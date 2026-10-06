"""Zapy 1.4.0 (en desarrollo) - Tutor académico multiusuario en Discord (Gemini + Notion).

Paso 1 del modo multiusuario: capa de datos (SQLite) con tokens cifrados (Fernet)
y migración automática del perfil del propietario. El comportamiento del bot
todavía no cambia: los pasos siguientes harán que Notion, el prompt y Gemini
dependan del usuario.
Dependencias: discord.py>=2.3, aiohttp (viene con discord.py), python-dotenv,
google-genai, cryptography y, en Windows, tzdata (para zoneinfo).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord.ext import commands, tasks
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("zapy")

# ----------------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
NOTION_TOKEN = (os.getenv("NOTION_TOKEN") or "").strip()
NOTION_DATABASE_ID = (os.getenv("NOTION_DATABASE_ID") or "").strip()


def _a_int(valor: str | None) -> int | None:
    try:
        return int(valor) if valor else None
    except ValueError:
        return None


CANAL_NOTIFICACIONES_ID = _a_int(os.getenv("CANAL_NOTIFICACIONES_ID"))

# Opcional: IDs de Discord (separados por comas) que pueden usar el bot.
# Si se deja vacío, puede usarlo cualquiera que lo vea.
USUARIOS_PERMITIDOS = {
    int(x) for x in os.getenv("USUARIOS_PERMITIDOS", "").replace(" ", "").split(",") if x.isdigit()
}

# Propietario: su configuración actual del .env se migra a la base de datos
# la primera vez. Si no se define, se usa USUARIOS_PERMITIDOS si solo hay uno.
OWNER_ID = _a_int(os.getenv("OWNER_ID")) or (
    next(iter(USUARIOS_PERMITIDOS)) if len(USUARIOS_PERMITIDOS) == 1 else None
)

# Clave maestra (Fernet) con la que se cifran los tokens de cada usuario.
# Generar una vez:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SECRET_KEY = (os.getenv("ZAPY_SECRET_KEY") or "").strip()
DB_FILE = Path(os.getenv("ZAPY_DB", "zapy.db"))

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Nombres de las propiedades en tus bases de datos de Notion.
PROP_TITULO = os.getenv("NOTION_PROP_TITULO", "Nombre")
PROP_FECHA = os.getenv("NOTION_PROP_FECHA", "Fecha")

try:
    TZ = ZoneInfo(os.getenv("BOT_TZ", "Europe/Madrid"))
except ZoneInfoNotFoundError:  # Windows sin tzdata
    log.warning("Zona horaria no encontrada (pip install tzdata). Uso UTC.")
    TZ = dt.timezone.utc

NOTION_IDS_FILE = Path("notion_ids.json")
CACHE_EVENTOS_TTL = 60  # s
CACHE_ALGORITMOS_TTL = 3600  # s
CACHE_ESQUEMA_TTL = 3600  # s
INTERVALO_AVISOS = 300  # s entre revisiones de eventos nuevos (se recorre a cada usuario)
MAX_ALGORITMO_CHARS = 20_000
HISTORIAL_MENSAJES = 10
COOLDOWN_IA = 5  # s entre peticiones de IA por usuario
MAX_TOKENS_CHAT = 1500  # los modelos con "thinking" gastan parte de este límite
MAX_TOKENS_MASTERCLASS = 8192
NOTION_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Alias de asignatura -> variable de entorno con el ID de su base de datos.
_ALIAS_ASIGNATURAS = {
    "NOTION_MATES_ID": ["mates", "matematicas"],
    "NOTION_FYQ_ID": ["fyq", "fisica y quimica", "fisica", "quimica"],
    "NOTION_TECNO_ID": ["tecno", "tecnologia"],
    "NOTION_DIGI_ID": ["digi", "digitalizacion"],
    "NOTION_ROBOTICA_ID": ["robotica"],
    "NOTION_EUSKERA_ID": ["euskera"],
    "NOTION_LENGUA_ID": ["lengua"],
    "NOTION_INGLES_ID": ["ingles"],
    "NOTION_GEOHIST_ID": ["geo-hist", "geografia e historia", "historia", "geografia"],
}


def _norm(texto: str) -> str:
    """minúsculas y sin tildes: 'Matemáticas' -> 'matematicas'."""
    sin_tildes = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sin_tildes if not unicodedata.combining(c)).lower().strip()


ASIGNATURAS: dict[str, str] = {
    alias: os.environ[var].strip()
    for var, alias_lista in _ALIAS_ASIGNATURAS.items()
    if os.getenv(var)
    for alias in alias_lista
}

DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

# Gemini y Notion ya no son globales: cada usuario usa su propia clave y su propio token.
# Las variables NOTION_*/GEMINI_API_KEY del .env solo sirven para migrar al propietario.

# ----------------------------------------------------------------------------
# PROMPTS
# ----------------------------------------------------------------------------
SYSTEM_PROMPT_BASE = """Zapy, a partir de ahora vas a ser un tutor academico experto en todas las areas academicas con los mejores metodos de estudio basados en la ciencia y en opiniones de expertos en el tema. Tambien vas a ser un experto en la organizacion de bloques de estudio y rutinas en general, tambien los metodos que usaras seran basadas en la ciencia y en opiniones de expertos. No hagas muy largas las respuestas

Actualmente, estoy en cuarto del eso cientifico con la siguientes asignaturas: Euskera, Lengua Castellana, Ingles, Geografia e Historia, Educacion Fisica, Tutoria, Matematicas academicas, Fisica y Quimica, Tecnologia, Digitalizacion y Robotica. Todas las asignaturas se explican, se hacen los deberes, proyectos y examenes en Euskera menos Ingles y Lengua Castellana.

Mi objetivos son tener una rutina muy bien estructurada para un estudio muy bueno y con la menor cantidad de horas de estudio gracias a los mejores metodos de estudio. Asi que necesito una rutina para cada dia o semana o periodo acorde a mis necesidades. Todo esto para sacar la maxima nota en cada asignatura.

Esta es mi rutina semanal con todos los horarios exactos, mis impedimentos, mis preferencias, mis huecos libres…:


Lunes

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:45 - 20:30 entrenar, 20:30 - 21:30 volver a casa y cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Martes

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 20:00 - 21:30 Estar con Familia y Cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Miercoles

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:45 - 20:30 entrenar, 20:30 - 21:30 volver a casa y cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Jueves

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 19:15 - 21:45 entrenar, 22:00 - 22:30 volver a casa y cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Viernes

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): Las tardes del viernes no estudio
- Hora de cierre / descanso nocturno: Nunca se sabe, pero tarde


Sabado: Los sabados a la mañana/mediodia hay partido y no suelo estar hasta las 16:00

Domingo: Entre las 13:00 y 16:00 no puedo.

Quiero que me respondas diciendo en que momento estudio, con que metodo, que asignatura… Ejemplo:  A las 3:15 Tienes que estudiar mates con este metodo “x” hasta las 5:00
"""

# El primer párrafo es la persona de Zapy (válida para todos); el resto es el perfil
# y la rutina del propietario, que se migra a la base de datos como su rutina.
PERSONA_BASE, _, RUTINA_PROPIETARIO = SYSTEM_PROMPT_BASE.partition("\n\n")
RUTINA_PROPIETARIO = RUTINA_PROPIETARIO.strip()


SYSTEM_PROMPT_MASTERCLASS = """Zapy, actúa como un catedrático y tutor académico de excelencia, especialista en pedagogía de alto rendimiento y preparación para exámenes de ESO y Bachillerato. Tu habilidad principal es transformar temarios complejos en "Masterclasses" hiperdetalladas, rigurosas e imborrables para la memoria.
El objetivo principal es elaborar una "Masterclass Completa" y exhaustiva sobre el tema que te pida, diseñada para un estudiante que busca sacar un 10 en su examen. Cada tema tiene que ser explicado de la mejor manera posible siendo claro. En el apartado siguiente te incorporo la estructura y reglas de formato.

ESTRUCTURA:
INTRODUCCION DEL TEMA, RAPIDO Y CLARO. EJEMPLO: “ESTA MASTERCLASS TRATA DE LAS ECUACIONES DE SEGUNDO GRADO, AQUI APRENDERAS A HACERLAS PASO A PASO Y LUEGO TENDRAS UN EJERCICIOS DE PRUEBA”

FASE DE APRENDIZAJE: EN ESTA FASE VAS A ENSEÑARME PASO A PASO A HACER “X” EJERCICIO O EL TEMA. EJEMPLO: PARA CONVERTIR UNA ORACION NOMINAL EN UNA VERBAL, PRIMERO DEBES CAMBIAR ESTO… LUEGO HAY UN EJEMPLO DE LA EXPLICACION ABAJO Y ASI CONSTANTEMENTE. PD: SI ES UNA MASTERCLASS DE IDIOMA PON EL EJEMPLO EN LA LENGUA QUE SE QUIERE APRENDER.

FASE DE EJERCICIOS DE PRACTICA: EN LA PENULTIMA FASE CREA EJERCICIOS DE PRUEBA PARA PRACTICAR LO APRENDIDO. HAZLOS DE MAS FACILES A MAS DIFICILES.

ERRORES TIPICOS Y CORRECCION DE EJERCICIOS: ENSEÑA LOS ERRORES TIPICOS CON SU EXPLICACIÓN. DA LA CORRECION DE EJERCICIOS CON SU RESPECTIVA EXPLICACION.

REGLAS DE FORMATO (Markdown, se publicará en Notion):
- Jerarquía visual clara: usa encabezados (#, ##, ###) para dividir el contenido en módulos lógicos y progresivos.
- Glosario de conceptos clave: al inicio de cada sección, destaca en **negrita** las definiciones exactas necesarias para las preguntas teóricas de examen.
- Fórmulas (si el tema involucra ciencias, matemáticas o lógica): inclúyelas todas en LaTeX, con $formula$ en línea y, para ecuaciones centradas, $$ en una línea, la fórmula en la siguiente y $$ en otra. Explica el significado y las unidades de cada variable.
- Desglose de conceptos: usa listas con viñetas para explicar reglas, criterios de signos, excepciones o clasificaciones.
- No uses tablas Markdown (usa listas). No añadas saludos ni comentarios fuera de la masterclass.

Tono y enfoque: directo, riguroso, didáctico y sin omitir ningún apartado del tema por extenso que sea.
"""


# ----------------------------------------------------------------------------
# CLIENTE NOTION (asíncrono, con reintentos, límite de concurrencia y paginación)
# ----------------------------------------------------------------------------
class NotionError(Exception):
    pass


class Notion:
    BASE = "https://api.notion.com/v1"

    def __init__(self, token: str, session: aiohttp.ClientSession):
        self._session = session
        self._sem = asyncio.Semaphore(3)  # Notion permite ~3 peticiones/s
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    async def request(self, method: str, endpoint: str, payload: dict | None = None) -> dict:
        url = f"{self.BASE}/{endpoint.lstrip('/')}"
        ultimo_error = "sin respuesta"
        for intento in range(3):
            espera = 2**intento
            try:
                async with self._sem, self._session.request(
                    method, url, headers=self._headers, json=payload, timeout=NOTION_TIMEOUT
                ) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        ultimo_error = f"HTTP {resp.status}"
                        espera = float(resp.headers.get("Retry-After", espera))
                    elif resp.status >= 400:
                        raise NotionError(f"HTTP {resp.status}: {(await resp.text())[:300]}")
                    else:
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                ultimo_error = repr(e)
            if intento < 2:
                await asyncio.sleep(espera)
        raise NotionError(f"{method} {endpoint} falló tras 3 intentos ({ultimo_error})")

    async def query_database(self, db_id: str) -> list[dict]:
        paginas: list[dict] = []
        cursor = None
        while True:
            body: dict = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = await self.request("POST", f"databases/{db_id}/query", body)
            paginas.extend(data.get("results", []))
            if not data.get("has_more"):
                return paginas
            cursor = data.get("next_cursor")

    async def children(self, block_id: str) -> list[dict]:
        bloques: list[dict] = []
        cursor = None
        while True:
            qs = f"?page_size=100&start_cursor={cursor}" if cursor else "?page_size=100"
            data = await self.request("GET", f"blocks/{block_id}/children{qs}")
            bloques.extend(data.get("results", []))
            if not data.get("has_more"):
                return bloques
            cursor = data.get("next_cursor")


# ----------------------------------------------------------------------------
# DATOS DE USUARIO (SQLite local + tokens cifrados con Fernet)
# ----------------------------------------------------------------------------
class Boveda:
    """Cifra/descifra secretos (tokens de Notion, claves de Gemini) con la clave maestra."""

    def __init__(self, clave: str):
        self._fernet = Fernet(clave.encode("ascii"))

    def cifrar(self, texto: str) -> str:
        return self._fernet.encrypt(texto.encode("utf-8")).decode("ascii")

    def descifrar(self, token: str) -> str | None:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return None  # clave maestra distinta o dato corrupto


@dataclass(frozen=True)
class Perfil:
    """Configuración personal de un usuario (secretos ya descifrados, solo en memoria)."""
    user_id: int
    curso: str
    rutina: str
    notion_token: str | None
    notion_calendario_id: str | None
    gemini_key: str | None
    asignaturas: dict[str, str]  # alias normalizado -> ID de su base de datos en Notion

    @property
    def tiene_notion(self) -> bool:
        return bool(self.notion_token)

    @property
    def tiene_calendario(self) -> bool:
        return bool(self.notion_token and self.notion_calendario_id)

    @property
    def tiene_gemini(self) -> bool:
        return bool(self.gemini_key)


_CAMPOS_TEXTO = frozenset({"curso", "rutina", "notion_calendario_id"})
_CAMPOS_SECRETOS = frozenset({"notion_token", "gemini_key"})
MAX_RUTINA_CHARS = 4000


def _ahora_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Almacen:
    """Persistencia por usuario. Las operaciones públicas son async (SQLite va en un hilo)."""

    _ESQUEMA = """
    CREATE TABLE IF NOT EXISTS usuarios (
        user_id INTEGER PRIMARY KEY,
        curso TEXT NOT NULL DEFAULT '',
        rutina TEXT NOT NULL DEFAULT '',
        notion_token TEXT,
        notion_calendario_id TEXT,
        gemini_key TEXT,
        linea_base INTEGER NOT NULL DEFAULT 0,
        creado TEXT NOT NULL,
        actualizado TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS asignaturas (
        user_id INTEGER NOT NULL REFERENCES usuarios(user_id) ON DELETE CASCADE,
        alias TEXT NOT NULL,
        db_id TEXT NOT NULL,
        PRIMARY KEY (user_id, alias)
    );
    CREATE TABLE IF NOT EXISTS eventos_vistos (
        user_id INTEGER NOT NULL REFERENCES usuarios(user_id) ON DELETE CASCADE,
        evento_id TEXT NOT NULL,
        PRIMARY KEY (user_id, evento_id)
    );
    """

    def __init__(self, ruta: Path, boveda: Boveda):
        self._boveda = boveda
        self._lock = threading.Lock()
        self._con = sqlite3.connect(ruta, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        with self._lock:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute("PRAGMA foreign_keys=ON")
            self._con.executescript(self._ESQUEMA)

    def cerrar(self) -> None:
        with self._lock:
            self._con.close()

    # --- API async ---------------------------------------------------------
    async def obtener_perfil(self, user_id: int) -> Perfil | None:
        return await asyncio.to_thread(self._obtener_perfil, user_id)

    async def guardar_perfil(self, user_id: int, **campos: str | None) -> None:
        """Crea o actualiza solo los campos indicados (curso, rutina, notion_token, ...)."""
        await asyncio.to_thread(self._guardar_perfil, user_id, campos)

    async def reemplazar_asignaturas(self, user_id: int, asignaturas: dict[str, str]) -> None:
        await asyncio.to_thread(self._reemplazar_asignaturas, user_id, asignaturas)

    async def borrar_usuario(self, user_id: int) -> bool:
        return await asyncio.to_thread(self._borrar_usuario, user_id)

    async def ids_con_calendario(self) -> list[int]:
        return await asyncio.to_thread(self._ids_con_calendario)

    async def ids_vistos(self, user_id: int) -> set[str] | None:
        """None = aún no hay línea base de eventos para este usuario."""
        return await asyncio.to_thread(self._ids_vistos, user_id)

    async def fijar_linea_base(self, user_id: int, ids: set[str]) -> None:
        await asyncio.to_thread(self._fijar_linea_base, user_id, ids)

    async def marcar_vistos(self, user_id: int, ids: set[str]) -> None:
        await asyncio.to_thread(self._marcar_vistos, user_id, ids)

    # --- Implementación síncrona ------------------------------------------
    def _descifrar_col(self, fila: sqlite3.Row, col: str) -> str | None:
        if not fila[col]:
            return None
        valor = self._boveda.descifrar(fila[col])
        if valor is None:
            log.warning("No pude descifrar %s del usuario %s (¿cambió ZAPY_SECRET_KEY?)",
                        col, fila["user_id"])
        return valor

    def _obtener_perfil(self, user_id: int) -> Perfil | None:
        with self._lock:
            fila = self._con.execute("SELECT * FROM usuarios WHERE user_id = ?", (user_id,)).fetchone()
            if fila is None:
                return None
            asignaturas = {
                r["alias"]: r["db_id"]
                for r in self._con.execute("SELECT alias, db_id FROM asignaturas WHERE user_id = ?", (user_id,))
            }
        return Perfil(
            user_id=user_id,
            curso=fila["curso"],
            rutina=fila["rutina"],
            notion_token=self._descifrar_col(fila, "notion_token"),
            notion_calendario_id=fila["notion_calendario_id"],
            gemini_key=self._descifrar_col(fila, "gemini_key"),
            asignaturas=asignaturas,
        )

    def _guardar_perfil(self, user_id: int, campos: dict[str, str | None]) -> None:
        invalidos = set(campos) - _CAMPOS_TEXTO - _CAMPOS_SECRETOS
        if invalidos:
            raise ValueError(f"Campos de perfil no válidos: {sorted(invalidos)}")
        valores: dict[str, str | None] = {}
        for campo, valor in campos.items():
            valor = (valor or "").strip()
            if campo in _CAMPOS_SECRETOS:
                valores[campo] = self._boveda.cifrar(valor) if valor else None
            elif campo == "notion_calendario_id":
                valores[campo] = valor or None
            elif campo == "rutina":
                valores[campo] = _limpiar_texto(valor)[:MAX_RUTINA_CHARS]
            else:  # curso
                valores[campo] = _limpiar_texto(valor)[:100]
        ahora = _ahora_iso()
        with self._lock, self._con:
            self._con.execute(
                "INSERT OR IGNORE INTO usuarios (user_id, creado, actualizado) VALUES (?, ?, ?)",
                (user_id, ahora, ahora),
            )
            if valores:
                sets = ", ".join(f"{c} = ?" for c in valores)  # nombres validados arriba
                self._con.execute(
                    f"UPDATE usuarios SET {sets}, actualizado = ? WHERE user_id = ?",
                    (*valores.values(), ahora, user_id),
                )

    def _reemplazar_asignaturas(self, user_id: int, asignaturas: dict[str, str]) -> None:
        filas = [(user_id, _norm(alias), db_id.strip()) for alias, db_id in asignaturas.items()
                 if _norm(alias) and db_id.strip()]
        with self._lock, self._con:
            self._con.execute("DELETE FROM asignaturas WHERE user_id = ?", (user_id,))
            self._con.executemany("INSERT OR REPLACE INTO asignaturas VALUES (?, ?, ?)", filas)

    def _borrar_usuario(self, user_id: int) -> bool:
        with self._lock, self._con:  # ON DELETE CASCADE limpia asignaturas y eventos_vistos
            return self._con.execute("DELETE FROM usuarios WHERE user_id = ?", (user_id,)).rowcount > 0

    def _ids_con_calendario(self) -> list[int]:
        with self._lock:
            filas = self._con.execute(
                "SELECT user_id FROM usuarios WHERE notion_token IS NOT NULL "
                "AND notion_calendario_id IS NOT NULL AND notion_calendario_id != ''"
            ).fetchall()
        return [f["user_id"] for f in filas]

    def _ids_vistos(self, user_id: int) -> set[str] | None:
        with self._lock:
            fila = self._con.execute("SELECT linea_base FROM usuarios WHERE user_id = ?", (user_id,)).fetchone()
            if fila is None or not fila["linea_base"]:
                return None
            return {r["evento_id"] for r in
                    self._con.execute("SELECT evento_id FROM eventos_vistos WHERE user_id = ?", (user_id,))}

    def _fijar_linea_base(self, user_id: int, ids: set[str]) -> None:
        with self._lock, self._con:
            self._con.execute("DELETE FROM eventos_vistos WHERE user_id = ?", (user_id,))
            self._con.executemany("INSERT OR IGNORE INTO eventos_vistos VALUES (?, ?)",
                                  [(user_id, i) for i in ids])
            self._con.execute("UPDATE usuarios SET linea_base = 1 WHERE user_id = ?", (user_id,))

    def _marcar_vistos(self, user_id: int, ids: set[str]) -> None:
        with self._lock, self._con:
            self._con.executemany("INSERT OR IGNORE INTO eventos_vistos VALUES (?, ?)",
                                  [(user_id, i) for i in ids])


async def migrar_propietario(almacen: Almacen, ids_vistos: set[str] | None) -> None:
    """Primer arranque: vuelca la configuración del .env del propietario a la base de datos.

    Es idempotente: si el propietario ya existe no se toca nada.
    """
    if OWNER_ID is None:
        log.info("Migración omitida: define OWNER_ID en el .env para migrar tu configuración.")
        return
    if await almacen.obtener_perfil(OWNER_ID) is not None:
        return
    await almacen.guardar_perfil(
        OWNER_ID,
        curso="4º ESO científico",
        rutina=RUTINA_PROPIETARIO,
        notion_token=NOTION_TOKEN or None,
        notion_calendario_id=NOTION_DATABASE_ID or None,
        gemini_key=GEMINI_KEY or None,
    )
    await almacen.reemplazar_asignaturas(OWNER_ID, ASIGNATURAS)
    if ids_vistos is not None:
        await almacen.fijar_linea_base(OWNER_ID, ids_vistos)
    log.info("✅ Configuración del propietario (%s) migrada a %s", OWNER_ID, DB_FILE)


# ----------------------------------------------------------------------------
# PROMPT PERSONALIZADO (persona común + datos de cada usuario)
# ----------------------------------------------------------------------------
INSTRUCCION_FORMATO = (
    "Cuando te pida planificar, responde indicando en qué momento estudiar, qué asignatura y con qué "
    'método, respetando sus horarios fijos. Ejemplo: "A las 15:15 estudia Mates con el método X hasta '
    'las 17:00". Tu usuario es estudiante de ESO o Bachillerato: mantén siempre un tono y un contenido '
    "apropiados para su edad."
)


def construir_system_prompt(perfil: Perfil, eventos: str, ahora: dt.datetime) -> str:
    rutina = perfil.rutina or (
        "(Todavía no ha escrito su rutina. Si te pide un plan, pregúntale por sus horarios "
        "o recuérdale que puede guardarla con `!configurar`.)"
    )
    curso = f"CURSO DEL ESTUDIANTE: {perfil.curso}\n\n" if perfil.curso else ""
    return (
        f"{PERSONA_BASE}\n\n{INSTRUCCION_FORMATO}\n\n{curso}"
        f"PERFIL Y RUTINA DEL ESTUDIANTE:\n{rutina}\n\n"
        f"HOY ES: {DIAS_SEMANA[ahora.weekday()]}, {ahora:%d/%m/%Y}, {ahora:%H:%M}\n\n"
        f"EXÁMENES Y EVENTOS PRÓXIMOS EN NOTION:\n{eventos}"
    )


# ----------------------------------------------------------------------------
# BOT
# ----------------------------------------------------------------------------
class ZapyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.session: aiohttp.ClientSession | None = None
        self.almacen: Almacen | None = None
        # Un cliente de Notion por usuario: cada token tiene su propio límite de peticiones.
        self._notion_por_usuario: dict[int, tuple[str, Notion]] = {}

    def notion_de(self, perfil: Perfil) -> Notion | None:
        """Cliente de Notion del usuario, o None si no ha conectado su token."""
        if not perfil.notion_token or self.session is None:
            return None
        guardado = self._notion_por_usuario.get(perfil.user_id)
        if guardado and guardado[0] == perfil.notion_token:
            return guardado[1]
        notion = Notion(perfil.notion_token, self.session)
        self._notion_por_usuario[perfil.user_id] = (perfil.notion_token, notion)
        return notion

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.almacen = await asyncio.to_thread(Almacen, DB_FILE, Boveda(SECRET_KEY))
        # Los IDs de eventos del modo antiguo (un solo usuario) solo sirven para migrar.
        ids_legado = await asyncio.to_thread(_cargar_ids_disco)
        await migrar_propietario(self.almacen, ids_legado)
        comprobar_nuevos_eventos.start()

    async def close(self):
        if self.session:
            await self.session.close()
        if self.almacen:
            await asyncio.to_thread(self.almacen.cerrar)
        await super().close()


bot = ZapyBot()


def autorizado(user: discord.abc.User) -> bool:
    return not USUARIOS_PERMITIDOS or user.id in USUARIOS_PERMITIDOS


@bot.check
async def _solo_autorizados(ctx: commands.Context) -> bool:
    return autorizado(ctx.author)


MSG_SIN_CONFIGURAR = (
    "👋 Todavía no tienes tu Zapy configurado. Usa `!configurar` para añadir tu clave gratuita "
    "de Gemini, tu rutina y, si quieres, tu Notion."
)
MSG_FALTA_GEMINI = "🔑 Te falta añadir tu clave de Gemini. Hazlo con `!configurar`."
MSG_FALTA_NOTION = "📓 Aún no has conectado tu Notion. Hazlo con `!configurar`."
MSG_FALTA_CALENDARIO = "📅 Aún no has indicado tu calendario de Notion. Hazlo con `!configurar`."
MSG_CLAVE_GEMINI = "🔑 Tu clave de Gemini no es válida o ha dejado de funcionar. Actualízala con `!configurar`."
MSG_CUOTA_GEMINI = (
    "⏳ Tu clave de Gemini ha llegado al límite gratuito por ahora. "
    "Espera unos minutos (o al día siguiente) e inténtalo de nuevo."
)


async def requerir_perfil(user_id: int, enviar, *, notion: bool = False, calendario: bool = False,
                          gemini: bool = False) -> Perfil | None:
    """Perfil del usuario si cumple los requisitos; si no, le avisa con `enviar` y devuelve None."""
    perfil = await bot.almacen.obtener_perfil(user_id) if bot.almacen else None
    if perfil is None:
        aviso = MSG_SIN_CONFIGURAR
    elif gemini and not perfil.tiene_gemini:
        aviso = MSG_FALTA_GEMINI
    elif (notion or calendario) and not perfil.tiene_notion:
        aviso = MSG_FALTA_NOTION
    elif calendario and not perfil.tiene_calendario:
        aviso = MSG_FALTA_CALENDARIO
    else:
        return perfil
    await enviar(aviso)
    return None


# ----------------------------------------------------------------------------
# EVENTOS (calendario de exámenes en Notion)
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Evento:
    id: str
    nombre: str
    inicio: str | None
    fin: str | None

    @staticmethod
    def _parse(valor: str | None) -> dt.date | None:
        try:
            return dt.date.fromisoformat(valor[:10]) if valor else None
        except ValueError:
            return None

    @property
    def fecha_inicio(self) -> dt.date | None:
        return self._parse(self.inicio)

    @property
    def ultimo_dia(self) -> dt.date | None:
        return self._parse(self.fin) or self.fecha_inicio

    @property
    def fecha_texto(self) -> str:
        if not self.inicio:
            return "Sin fecha"
        return f"{self.inicio} -> {self.fin}" if self.fin else self.inicio


def _extraer_titulo(props: dict) -> str:
    for val in props.values():
        if isinstance(val, dict) and val.get("type") == "title":
            texto = "".join(t.get("plain_text", "") for t in val.get("title", []))
            if texto:
                return texto
    return "Sin título"


def _extraer_fechas(props: dict) -> tuple[str | None, str | None]:
    candidatas = [props.get(PROP_FECHA)] + list(props.values())
    for val in candidatas:
        if isinstance(val, dict) and val.get("type") == "date" and val.get("date"):
            return val["date"].get("start"), val["date"].get("end")
    return None, None


def _pagina_a_evento(page: dict) -> Evento:
    props = page.get("properties", {})
    inicio, fin = _extraer_fechas(props)
    return Evento(page["id"], _extraer_titulo(props), inicio, fin)


_cache_eventos: dict[int, tuple[float, list[Evento]]] = {}


async def obtener_eventos(perfil: Perfil, forzar: bool = False) -> list[Evento]:
    """Eventos del calendario de Notion del usuario (caché de 60 s). Lanza NotionError si falla."""
    ahora = time.monotonic()
    cacheado = _cache_eventos.get(perfil.user_id)
    if not forzar and cacheado and ahora - cacheado[0] < CACHE_EVENTOS_TTL:
        return cacheado[1]
    notion = bot.notion_de(perfil)
    if notion is None or not perfil.notion_calendario_id:
        raise NotionError("Notion no está conectado. Configúralo con `!configurar`.")
    paginas = await notion.query_database(perfil.notion_calendario_id)
    eventos = [_pagina_a_evento(pg) for pg in paginas]
    _cache_eventos[perfil.user_id] = (ahora, eventos)
    return eventos


def formatear_eventos(eventos: list[Evento], solo_futuros: bool = True) -> str:
    hoy = dt.datetime.now(TZ).date()
    visibles = [
        e for e in eventos
        if not solo_futuros or e.ultimo_dia is None or e.ultimo_dia >= hoy
    ]
    if not visibles:
        return "📅 No hay eventos ni exámenes próximos registrados en Notion."
    visibles.sort(key=lambda e: (e.fecha_inicio is None, e.fecha_inicio or dt.date.max))
    return "\n".join(f"• **{e.nombre}** — `{e.fecha_texto}`" for e in visibles)


def _cargar_ids_disco() -> set[str] | None:
    if not NOTION_IDS_FILE.exists():
        return None
    try:
        return set(json.loads(NOTION_IDS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        log.exception("No pude leer %s; se reiniciará la línea base", NOTION_IDS_FILE)
        return None


async def _destino_avisos(user_id: int):
    """Canal fijo para el propietario (si lo configuró) y mensaje privado para el resto."""
    if user_id == OWNER_ID and CANAL_NOTIFICACIONES_ID:
        canal = bot.get_channel(CANAL_NOTIFICACIONES_ID)
        if canal is not None:
            return canal
    try:
        usuario = bot.get_user(user_id) or await bot.fetch_user(user_id)
        return await usuario.create_dm()
    except discord.HTTPException:
        return None


async def _avisar_eventos_nuevos(user_id: int) -> None:
    perfil = await bot.almacen.obtener_perfil(user_id)
    if perfil is None or not perfil.tiene_calendario:
        return
    eventos = await obtener_eventos(perfil, forzar=True)
    actuales = {e.id for e in eventos}

    vistos = await bot.almacen.ids_vistos(user_id)
    if vistos is None:  # primera vez de este usuario: línea base sin avisar
        await bot.almacen.fijar_linea_base(user_id, actuales)
        return

    nuevos = [e for e in eventos if e.id not in vistos]
    if not nuevos:
        return
    destino = await _destino_avisos(user_id)
    if destino is None:
        return  # se reintentará en la próxima vuelta

    avisados: set[str] = set()
    for ev in nuevos:
        embed = discord.Embed(
            title="🆕 Nuevo evento en Notion",
            description="Se ha detectado una nueva entrada en tu calendario.",
            color=discord.Color.green(),
        )
        embed.add_field(name="📌 Evento", value=ev.nombre[:1024], inline=False)
        embed.add_field(name="📅 Fecha", value=ev.fecha_texto, inline=False)
        try:
            await destino.send(embed=embed)
            avisados.add(ev.id)
        except discord.Forbidden:  # mensajes privados cerrados: no insistir
            log.info("No puedo avisar al usuario %s (mensajes privados cerrados)", user_id)
            avisados.update(e.id for e in nuevos)
            break
        except discord.HTTPException:
            log.exception("No pude avisar del evento %s (se reintentará)", ev.id)
    if avisados:
        await bot.almacen.marcar_vistos(user_id, avisados)


@tasks.loop(seconds=INTERVALO_AVISOS)
async def comprobar_nuevos_eventos():
    if bot.almacen is None:
        return
    for user_id in await bot.almacen.ids_con_calendario():
        try:
            await _avisar_eventos_nuevos(user_id)
        except NotionError as e:
            log.warning("Notion no disponible para el usuario %s: %s", user_id, e)
        except Exception:
            log.exception("Fallo inesperado revisando eventos del usuario %s", user_id)
        await asyncio.sleep(1)  # escalonado: no lanzar todas las consultas a la vez


@comprobar_nuevos_eventos.before_loop
async def _antes_de_comprobar():
    await bot.wait_until_ready()


# ----------------------------------------------------------------------------
# APUNTES: lectura de la metodología y escritura de masterclasses en Notion
# ----------------------------------------------------------------------------
def _bloques_a_texto(bloques: list[dict]) -> str:
    lineas = []
    prefijos = {
        "heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
        "bulleted_list_item": "- ", "numbered_list_item": "1. ", "quote": "> ",
        "paragraph": "", "code": "",
    }
    for b in bloques:
        tipo = b.get("type")
        if tipo in prefijos:
            rich = b.get(tipo, {}).get("rich_text", [])
            texto = "".join(t.get("plain_text", "") for t in rich)
            if texto:
                lineas.append(prefijos[tipo] + texto)
        elif tipo == "equation":
            expr = b.get("equation", {}).get("expression", "")
            if expr:
                lineas.append(f"$${expr}$$")
    return "\n".join(lineas)


def resolver_asignatura(perfil: Perfil, nombre: str) -> str | None:
    return perfil.asignaturas.get(_norm(nombre))


_cache_algoritmos: dict[tuple[int, str], tuple[float, str]] = {}


async def obtener_algoritmo_asignatura(perfil: Perfil, db_id: str) -> str:
    """Metodología de la asignatura guardada en su base de datos de Notion."""
    ahora = time.monotonic()
    clave = (perfil.user_id, db_id)
    cacheado = _cache_algoritmos.get(clave)
    if cacheado and ahora - cacheado[0] < CACHE_ALGORITMOS_TTL:
        return cacheado[1]
    notion = bot.notion_de(perfil)
    if notion is None:
        return ""
    try:
        paginas = await notion.query_database(db_id)
        # Las masterclasses generadas viven en la misma base de datos: no se
        # reinyectan como "metodología" o el prompt crecería en cada uso.
        paginas = [
            pg for pg in paginas
            if not _extraer_titulo(pg.get("properties", {})).startswith("Masterclass:")
        ]
        textos = await asyncio.gather(*(notion.children(pg["id"]) for pg in paginas))
        contenido = "\n\n---\n\n".join(t for t in map(_bloques_a_texto, textos) if t)
        contenido = contenido[:MAX_ALGORITMO_CHARS]
    except NotionError as e:
        log.warning("No pude leer la metodología (%s): %s", db_id, e)
        return ""  # no se cachea el fallo
    _cache_algoritmos[clave] = (ahora, contenido)
    return contenido


# --- Markdown -> bloques de Notion -----------------------------------------
RICH_MAX = 2000  # límite de Notion por trozo de texto
EQ_MAX = 1000  # límite de Notion por ecuación
_LENGUAJES_CODIGO = {"python", "java", "javascript", "json", "bash", "c", "c++", "sql", "html", "css"}
_RE_INLINE = re.compile(r"(\$\$[^$\n]+\$\$|\$(?!\s)[^$\n]+?(?<!\s)\$|\*\*[^*\n]+\*\*)")


def _limpiar_texto(texto: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", texto.replace("\r\n", "\n")).strip()


def _trozos_texto(texto: str, negrita: bool = False) -> list[dict]:
    extra = {"annotations": {"bold": True}} if negrita else {}
    return [
        {"type": "text", "text": {"content": texto[i : i + RICH_MAX]}, **extra}
        for i in range(0, len(texto), RICH_MAX)
    ]


def _rich_text(texto: str) -> list[dict]:
    """Convierte **negrita**, $fórmulas$ y $$fórmulas$$ en línea a rich_text."""
    rt: list[dict] = []
    for parte in _RE_INLINE.split(texto):
        if not parte:
            continue
        expr = None
        if parte.startswith("$$") and parte.endswith("$$") and len(parte) > 4:
            expr = parte[2:-2].strip()
        elif parte.startswith("$") and parte.endswith("$") and len(parte) > 2:
            expr = parte[1:-1].strip()
        if expr and len(expr) <= EQ_MAX:
            rt.append({"type": "equation", "equation": {"expression": expr}})
        elif parte.startswith("**") and parte.endswith("**") and len(parte) > 4:
            rt.extend(_trozos_texto(parte[2:-2], negrita=True))
        else:
            rt.extend(_trozos_texto(parte))
    return rt[:100]  # máximo de Notion por bloque


def _bloque(tipo: str, texto: str) -> dict | None:
    rt = _rich_text(texto)
    return {"object": "block", "type": tipo, tipo: {"rich_text": rt}} if rt else None


def markdown_a_bloques(markdown: str) -> list[dict]:
    lineas = _limpiar_texto(markdown).split("\n")
    bloques: list[dict | None] = []
    i = 0
    while i < len(lineas):
        linea = lineas[i].strip()
        i += 1
        if not linea:
            continue

        # Bloque de código ```lang ... ```
        if linea.startswith("```"):
            lang = linea[3:].strip().lower()
            codigo = []
            while i < len(lineas) and not lineas[i].strip().startswith("```"):
                codigo.append(lineas[i])
                i += 1
            i += 1
            texto = "\n".join(codigo)[:RICH_MAX]
            if texto:
                bloques.append({
                    "object": "block", "type": "code",
                    "code": {
                        "rich_text": [{"type": "text", "text": {"content": texto}}],
                        "language": lang if lang in _LENGUAJES_CODIGO else "plain text",
                    },
                })
            continue

        # Ecuación en bloque: $$x$$ en una línea, o $$ / fórmula / $$ en varias
        if linea.startswith("$$"):
            expr = None
            if linea.endswith("$$") and len(linea) > 4:
                expr = linea[2:-2].strip()
            elif "$$" not in linea[2:]:
                partes = [linea[2:]]
                while i < len(lineas):
                    siguiente = lineas[i].strip()
                    i += 1
                    if siguiente.endswith("$$"):
                        partes.append(siguiente[:-2])
                        break
                    partes.append(siguiente)
                expr = " ".join(p for p in partes if p).strip()
            if expr is not None:
                if expr and len(expr) <= EQ_MAX:
                    bloques.append({"object": "block", "type": "equation",
                                    "equation": {"expression": expr}})
                elif expr:
                    bloques.append(_bloque("paragraph", expr))
                continue

        if re.fullmatch(r"[-*_]{3,}", linea):
            bloques.append({"object": "block", "type": "divider", "divider": {}})
        elif m := re.match(r"^(#{1,6})\s+(.*)", linea):
            bloques.append(_bloque(f"heading_{min(len(m.group(1)), 3)}", m.group(2)))
        elif m := re.match(r"^[-*]\s+(.*)", linea):
            bloques.append(_bloque("bulleted_list_item", m.group(1)))
        elif m := re.match(r"^\d+[.)]\s+(.*)", linea):
            bloques.append(_bloque("numbered_list_item", m.group(1)))
        elif linea.startswith(">"):
            bloques.append(_bloque("quote", linea.lstrip("> ")))
        else:
            bloques.append(_bloque("paragraph", linea))
    return [b for b in bloques if b]


_cache_esquemas: dict[str, tuple[float, tuple[str, str | None]]] = {}


async def propiedades_db(notion: Notion, db_id: str) -> tuple[str, str | None]:
    """Nombre de la propiedad de título y de fecha de una base de datos (cada usuario puede
    llamarlas distinto: "Nombre"/"Name", "Fecha"/"Date"...)."""
    ahora = time.monotonic()
    cacheado = _cache_esquemas.get(db_id)
    if cacheado and ahora - cacheado[0] < CACHE_ESQUEMA_TTL:
        return cacheado[1]
    props = (await notion.request("GET", f"databases/{db_id}")).get("properties", {})
    titulo = PROP_TITULO if props.get(PROP_TITULO, {}).get("type") == "title" else next(
        (n for n, v in props.items() if v.get("type") == "title"), PROP_TITULO
    )
    fechas = [n for n, v in props.items() if v.get("type") == "date"]
    fecha = PROP_FECHA if PROP_FECHA in fechas else (fechas[0] if fechas else None)
    _cache_esquemas[db_id] = (ahora, (titulo, fecha))
    return titulo, fecha


async def crear_pagina_notion(notion: Notion, db_id: str, titulo: str, fecha: str | None = None,
                              bloques: list[dict] | None = None) -> dict:
    """Crea una página; los primeros 100 bloques van en la misma petición."""
    prop_titulo, prop_fecha = await propiedades_db(notion, db_id)
    props: dict = {prop_titulo: {"title": [{"text": {"content": titulo[:RICH_MAX]}}]}}
    if fecha:
        if not prop_fecha:
            raise NotionError("Tu base de datos no tiene ninguna propiedad de tipo fecha.")
        props[prop_fecha] = {"date": {"start": fecha}}
    cuerpo: dict = {"parent": {"database_id": db_id}, "properties": props}
    bloques = bloques or []
    if bloques:
        cuerpo["children"] = bloques[:100]
    pagina = await notion.request("POST", "pages", cuerpo)
    for i in range(100, len(bloques), 100):
        await notion.request("PATCH", f"blocks/{pagina['id']}/children", {"children": bloques[i : i + 100]})
    return pagina


# ----------------------------------------------------------------------------
# GEMINI
# ----------------------------------------------------------------------------
class ClaveGeminiError(Exception):
    """La clave de Gemini del usuario falta, no es válida o no tiene permiso."""


class CuotaGeminiError(Exception):
    """La clave de Gemini del usuario ha agotado su cuota gratuita."""


_clientes_gemini: dict[int, tuple[str, genai.Client]] = {}


def cliente_gemini(perfil: Perfil) -> genai.Client | None:
    """Cliente de Gemini con la clave del propio usuario (cada uno gasta su cuota gratuita)."""
    if not perfil.gemini_key:
        return None
    guardado = _clientes_gemini.get(perfil.user_id)
    if guardado and guardado[0] == perfil.gemini_key:
        return guardado[1]
    cliente = genai.Client(api_key=perfil.gemini_key)
    _clientes_gemini[perfil.user_id] = (perfil.gemini_key, cliente)
    return cliente


def invalidar_cache_usuario(user_id: int) -> None:
    """Olvida todo lo guardado en memoria de un usuario (al cambiar su configuración o borrarlo)."""
    _cache_eventos.pop(user_id, None)
    _clientes_gemini.pop(user_id, None)
    bot._notion_por_usuario.pop(user_id, None)
    for clave in [k for k in _cache_algoritmos if k[0] == user_id]:
        del _cache_algoritmos[clave]


def _clave_rechazada(e: genai_errors.APIError) -> bool:
    return e.code in {401, 403} or (e.code == 400 and "api key" in str(e).lower())


async def generar_texto(cliente: genai.Client | None, contents, system: str, max_tokens: int,
                        temperature: float = 0.3) -> str:
    if cliente is None:
        raise ClaveGeminiError()
    config = types.GenerateContentConfig(
        system_instruction=system, temperature=temperature, max_output_tokens=max_tokens
    )
    for intento in range(3):
        try:
            resp = await cliente.aio.models.generate_content(
                model=GEMINI_MODEL, contents=contents, config=config
            )
        except genai_errors.APIError as e:
            if _clave_rechazada(e):
                raise ClaveGeminiError() from None  # sin el texto original: podría citar la clave
            if e.code in {429, 500, 503, 504} and intento < 2:
                await asyncio.sleep(2 ** (intento + 1))
                continue
            if e.code == 429:
                raise CuotaGeminiError() from e
            raise
        texto = (resp.text or "").strip()
        if not texto:
            raise RuntimeError("Gemini devolvió una respuesta vacía (¿bloqueo de seguridad o límite de tokens?)")
        return texto
    raise RuntimeError("Gemini no respondió")  # inalcanzable, por claridad


# ----------------------------------------------------------------------------
# DISCORD: utilidades de mensajes
# ----------------------------------------------------------------------------
SIN_MENCIONES = discord.AllowedMentions.none()


def dividir_mensaje(texto: str, limite: int = 1900) -> list[str]:
    """Parte el texto por líneas (no a mitad de palabra/fórmula)."""
    partes: list[str] = []
    actual = ""
    for linea in texto.split("\n"):
        while len(linea) > limite:  # línea gigante: corte duro
            if actual:
                partes.append(actual)
                actual = ""
            partes.append(linea[:limite])
            linea = linea[limite:]
        if actual and len(actual) + 1 + len(linea) > limite:
            partes.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        partes.append(actual)
    return partes


async def enviar_mensaje_largo(destino, texto: str) -> None:
    for parte in dividir_mensaje(texto):
        await destino.send(parte, allowed_mentions=SIN_MENCIONES)


def limpiar_mencion(texto: str) -> str:
    return re.sub(rf"<@!?{bot.user.id}>", "", texto).strip()


_ultima_peticion: dict[int, float] = {}


def _en_cooldown(user_id: int) -> bool:
    ahora = time.monotonic()
    if ahora - _ultima_peticion.get(user_id, 0.0) < COOLDOWN_IA:
        return True
    _ultima_peticion[user_id] = ahora
    return False


async def construir_conversacion(message: discord.Message, texto: str) -> list[types.Content]:
    """Historial reciente del hilo (si lo hay) + el mensaje actual."""
    turnos: list[types.Content] = []
    canal = message.channel
    if isinstance(canal, discord.Thread):
        previos = [m async for m in canal.history(limit=HISTORIAL_MENSAJES, before=message)]
        previos.reverse()
        if len(previos) < HISTORIAL_MENSAJES and canal.parent is not None:
            try:  # el mensaje que originó el hilo no está en su historial
                previos.insert(0, await canal.parent.fetch_message(canal.id))
            except discord.HTTPException:
                pass
        for m in previos:
            contenido = limpiar_mencion(m.content)
            if not contenido or contenido.startswith("!"):
                continue
            rol = "model" if m.author.id == bot.user.id else "user"
            turnos.append(types.Content(role=rol, parts=[types.Part(text=contenido)]))
    turnos.append(types.Content(role="user", parts=[types.Part(text=texto)]))
    return turnos


async def responder_con_ia(message: discord.Message) -> None:
    texto = limpiar_mencion(message.content)
    if not texto:
        await message.reply("¿En qué te ayudo? Cuéntame qué quieres planificar o estudiar.",
                            mention_author=False)
        return
    perfil = await requerir_perfil(
        message.author.id, lambda t: message.reply(t, mention_author=False), gemini=True
    )
    if perfil is None:
        return
    if _en_cooldown(message.author.id):
        await message.add_reaction("⏳")
        return

    destino = message.channel
    if isinstance(destino, discord.TextChannel):
        try:
            destino = await message.create_thread(
                name=f"Planificación - {message.author.display_name}"[:100]
            )
        except discord.HTTPException:
            log.warning("No pude crear el hilo; respondo en el canal")

    try:
        async with destino.typing():
            if perfil.tiene_calendario:
                try:
                    eventos = formatear_eventos(await obtener_eventos(perfil))
                except NotionError as e:
                    log.warning("Eventos no disponibles para la IA (usuario %s): %s", perfil.user_id, e)
                    eventos = "(no disponibles en este momento)"
            else:
                eventos = "(el usuario no ha conectado su calendario de Notion)"

            system = construir_system_prompt(perfil, eventos, dt.datetime.now(TZ))
            conversacion = await construir_conversacion(message, texto)
            respuesta = await generar_texto(cliente_gemini(perfil), conversacion, system, MAX_TOKENS_CHAT)
    except ClaveGeminiError:
        await destino.send(MSG_CLAVE_GEMINI)
        return
    except CuotaGeminiError:
        await destino.send(MSG_CUOTA_GEMINI)
        return
    except Exception:
        log.exception("Error al responder con IA")
        await destino.send("❌ No he podido generar la respuesta ahora mismo. Inténtalo de nuevo en un momento.")
        return
    await enviar_mensaje_largo(destino, respuesta)


def _debe_responder(message: discord.Message) -> bool:
    canal = message.channel
    if isinstance(canal, discord.DMChannel):
        return True
    if bot.user in message.mentions:  # (mentioned_in también salta con @everyone)
        return True
    # Hilos creados por Zapy: responde sin necesidad de mención.
    return isinstance(canal, discord.Thread) and canal.owner_id == bot.user.id


# ----------------------------------------------------------------------------
# EVENTOS DE DISCORD
# ----------------------------------------------------------------------------
@bot.event
async def on_ready():
    log.info("✅ Zapy activo como %s", bot.user)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return
    if autorizado(message.author) and _debe_responder(message):
        await responder_con_ia(message)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    error = getattr(error, "original", error)
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("⚠️ Faltan argumentos. Mira `!comandos` para ver el formato.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("⚠️ Argumento no válido. Mira `!comandos`.")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Espera {error.retry_after:.0f} s antes de repetirlo.", delete_after=5)
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send("⚠️ Este comando solo funciona en servidores.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("🚫 No tienes permisos para usar este comando.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("🚫 Me faltan permisos en este canal (gestionar mensajes / leer historial).")
    elif isinstance(error, commands.CheckFailure):
        return  # usuario no autorizado: silencio
    else:
        log.error("Error en el comando %s", ctx.command, exc_info=error)
        await ctx.send("❌ Ha ocurrido un error inesperado.")


# ----------------------------------------------------------------------------
# COMANDOS
# ----------------------------------------------------------------------------
@bot.command(name="comandos")
async def mostrar_comandos(ctx: commands.Context):
    embed = discord.Embed(title="🤖 Comandos de Zapy", color=discord.Color.blue())
    embed.add_field(name="📅 Notion", value="`!eventos` - Lista los próximos exámenes/tareas.", inline=False)
    embed.add_field(name="➕ Añadir", value="`!añadir Nombre | AAAA-MM-DD` - Guarda un evento.", inline=False)
    embed.add_field(name="🚀 Masterclass", value="`!apuntes Asignatura | Tema` - Genera apuntes en Notion.", inline=False)
    embed.add_field(name="🧹 Limpiar", value="`!clear [n]` - Borra mensajes (requiere permiso).", inline=False)
    embed.add_field(name="💬 Planificar", value="Menciona a Zapy o escríbele por DM.", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="clear")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def limpiar_mensajes(ctx: commands.Context, cantidad: int = 100):
    cantidad = max(1, min(cantidad, 500))
    borrados = await ctx.channel.purge(limit=cantidad + 1)  # +1: el propio comando
    await ctx.send(f"🧹 {max(len(borrados) - 1, 0)} mensajes borrados.", delete_after=3)


@bot.command(name="eventos")
@commands.cooldown(1, 5, commands.BucketType.user)
async def ver_eventos(ctx: commands.Context):
    perfil = await requerir_perfil(ctx.author.id, ctx.send, calendario=True)
    if perfil is None:
        return
    try:
        eventos = await obtener_eventos(perfil, forzar=True)
    except NotionError as e:
        await ctx.send(f"❌ No pude consultar Notion: {e}")
        return
    await enviar_mensaje_largo(ctx, f"📅 **Próximos eventos en tu Notion:**\n{formatear_eventos(eventos)}")


@bot.command(name="añadir", aliases=["anadir"])
@commands.cooldown(3, 30, commands.BucketType.user)
async def anadir_evento(ctx: commands.Context, *, args: str):
    nombre, _, fecha = args.partition("|")
    nombre, fecha = nombre.strip(), fecha.strip() or None
    if not nombre:
        await ctx.send("⚠️ Usa el formato: `!añadir Nombre | AAAA-MM-DD`")
        return
    if fecha:
        try:
            dt.date.fromisoformat(fecha)
        except ValueError:
            await ctx.send("⚠️ Fecha no válida. Usa el formato `AAAA-MM-DD` (por ejemplo `2026-11-20`).")
            return
    perfil = await requerir_perfil(ctx.author.id, ctx.send, calendario=True)
    if perfil is None:
        return

    async with ctx.typing():
        try:
            await crear_pagina_notion(bot.notion_de(perfil), perfil.notion_calendario_id, nombre, fecha)
            await obtener_eventos(perfil, forzar=True)
        except NotionError as e:
            log.warning("Error creando evento (usuario %s): %s", perfil.user_id, e)
            await ctx.send("❌ Error al guardar en Notion.")
            return
    await ctx.send(f"✅ Evento **{nombre}** creado en Notion.", allowed_mentions=SIN_MENCIONES)


@bot.command(name="apuntes")
@commands.cooldown(1, 60, commands.BucketType.user)
async def generar_apuntes(ctx: commands.Context, *, args: str):
    asignatura, sep, tema = (p.strip() for p in args.partition("|"))
    if not sep or not asignatura or not tema:
        await ctx.send("⚠️ Usa el formato: `!apuntes Asignatura | Tema`")
        ctx.command.reset_cooldown(ctx)
        return

    perfil = await requerir_perfil(ctx.author.id, ctx.send, notion=True, gemini=True)
    if perfil is None:
        ctx.command.reset_cooldown(ctx)
        return
    db_id = resolver_asignatura(perfil, asignatura)
    if not db_id:
        disponibles = ", ".join(f"`{a}`" for a in sorted(perfil.asignaturas)) or "ninguna configurada (usa `!configurar`)"
        await ctx.send(f"⚠️ No conozco la asignatura **{asignatura}**. Disponibles: {disponibles}",
                       allowed_mentions=SIN_MENCIONES)
        ctx.command.reset_cooldown(ctx)
        return

    async with ctx.typing():
        try:
            algoritmo = await obtener_algoritmo_asignatura(perfil, db_id)
            prompt = f"Elabora la Masterclass sobre '{tema}' de '{asignatura}'."
            if perfil.curso:
                prompt += f" Curso del estudiante: {perfil.curso}."
            if algoritmo:
                prompt += f"\n\n--- METODOLOGÍA Y ALGORITMO ---\n{algoritmo}"
            markdown = await generar_texto(
                cliente_gemini(perfil), prompt, SYSTEM_PROMPT_MASTERCLASS, MAX_TOKENS_MASTERCLASS
            )
            pagina = await crear_pagina_notion(
                bot.notion_de(perfil), db_id, f"Masterclass: {tema}", bloques=markdown_a_bloques(markdown)
            )
        except ClaveGeminiError:
            await ctx.send(MSG_CLAVE_GEMINI)
            return
        except CuotaGeminiError:
            await ctx.send(MSG_CUOTA_GEMINI)
            return
        except NotionError as e:
            log.warning("Error exportando masterclass (usuario %s): %s", perfil.user_id, e)
            await ctx.send("❌ Error al exportar a Notion.")
            return
        except Exception:
            log.exception("Error generando masterclass")
            await ctx.send("❌ No pude generar la masterclass ahora mismo. Inténtalo de nuevo en un momento.")
            return

    enlace = pagina.get("url", "")
    await ctx.send(
        f"🚀 **Masterclass generada:** **{tema}** ({asignatura.capitalize()}) publicada en Notion.\n{enlace}",
        allowed_mentions=SIN_MENCIONES,
    )


def main() -> None:
    if not TOKEN:
        log.critical("❌ ERROR CRÍTICO: FALTA DISCORD_TOKEN EN .ENV")
        sys.exit(1)
    try:
        Boveda(SECRET_KEY)
    except (ValueError, TypeError):
        log.critical(
            "❌ Falta ZAPY_SECRET_KEY (o no es válida) en el .env. Genera una con:\n"
            '   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
            "   Guárdala y haz copia: sin ella no se pueden descifrar los datos de los usuarios."
        )
        sys.exit(1)
    bot.run(TOKEN, log_handler=None)  # el logging ya está configurado arriba


if __name__ == "__main__":
    main()

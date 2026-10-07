"""Zapy 1.4.0 (en desarrollo) - Tutor académico multiusuario en Discord (Gemini + Notion).

Modo multiusuario: datos por usuario en SQLite con tokens cifrados (Fernet), Notion,
prompt y clave de Gemini propios de cada persona, y `!configurar` (panel con botones y
formularios) para que cada usuario se configure sin tocar el servidor.
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
# CONFIGURACIÓN POR USUARIO (!configurar: panel con botones y formularios)
# ----------------------------------------------------------------------------
MAX_ASIGNATURAS = 15
URL_CLAVE_GEMINI = "https://aistudio.google.com/apikey"
URL_INTEGRACIONES_NOTION = "https://www.notion.so/my-integrations"

AYUDA_CONFIG = (
    "**🔑 Clave de Gemini (gratis)**\n"
    f"1. Entra en {URL_CLAVE_GEMINI} con tu cuenta de Google.\n"
    "2. Pulsa *Crear clave de API* y cópiala.\n"
    "3. Dale a **Clave de Gemini** en el panel y pégala. Cada persona usa su propia clave.\n\n"
    "**📓 Notion (opcional)**\n"
    f"1. Entra en {URL_INTEGRACIONES_NOTION} y crea una integración interna. Copia su *token*.\n"
    "2. En Notion, abre la página o base de datos que quieres usar → menú **⋯** → **Conexiones** "
    "y añade tu integración. Sin este paso Zapy no puede verla.\n"
    "3. Elige **Conectar Notion** (pegas los enlaces de tus bases) o **Crear mi Notion** "
    "(Zapy crea el calendario y una base por asignatura dentro de una página tuya).\n\n"
    "Zapy lee las páginas de cada base de asignatura como tu metodología de estudio y guarda "
    "ahí las masterclasses de `!apuntes`."
)

_RE_ID_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_RE_ID_HEX = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", re.IGNORECASE)
_RE_LINEA_ASIG = re.compile(r"^\s*(?P<alias>[^=|:]+?)\s*[=|:]\s*(?P<resto>.+)$")


def extraer_id_notion(texto: str) -> str | None:
    """ID de una página/base de Notion a partir de un enlace o del propio ID."""
    ruta = texto.strip().split("?")[0].split("#")[0]  # fuera ?v=<vista> y anclas
    for patron in (_RE_ID_UUID, _RE_ID_HEX):
        hallados = patron.findall(ruta)
        if hallados:
            return hallados[-1].replace("-", "").lower()
    return None


def parsear_asignaturas(texto: str) -> tuple[dict[str, str], list[str]]:
    """Líneas 'Nombre = enlace' -> ({nombre: id}, líneas que no se entienden)."""
    encontradas: dict[str, str] = {}
    errores: list[str] = []
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        m = _RE_LINEA_ASIG.match(linea)
        db_id = extraer_id_notion(m["resto"]) if m else None
        alias = m["alias"].strip() if m else ""
        if not db_id or not alias or alias.lower() in {"http", "https"}:
            errores.append(linea[:40])
        else:
            encontradas[alias] = db_id
    return encontradas, errores


_GRUPOS_ALIAS = [[_norm(a) for a in lista] for lista in _ALIAS_ASIGNATURAS.values()]


def alias_equivalentes(nombre: str) -> list[str]:
    """Sinónimos de una asignatura conocida ('Mates' -> mates, matematicas); si no, solo ella."""
    n = _norm(nombre)
    for grupo in _GRUPOS_ALIAS:
        if n in grupo:
            return list(grupo)
    return [n] if n else []


def construir_alias(asignaturas: dict[str, str]) -> dict[str, str]:
    """{nombre: id} -> {alias normalizado: id}. Lo que escribe el usuario manda sobre los sinónimos."""
    resultado = {_norm(n): i for n, i in asignaturas.items() if _norm(n)}
    for nombre, db_id in asignaturas.items():
        for alias in alias_equivalentes(nombre):
            resultado.setdefault(alias, db_id)
    return resultado


def asignaturas_unicas(perfil: Perfil | None) -> list[tuple[str, str]]:
    """Una entrada por base de datos (con su alias más corto), ordenadas por nombre."""
    por_id: dict[str, str] = {}
    for alias, db_id in (perfil.asignaturas.items() if perfil else []):
        actual = por_id.get(db_id)
        if actual is None or (len(alias), alias) < (len(actual), actual):
            por_id[db_id] = alias
    return sorted((alias, db_id) for db_id, alias in por_id.items())


async def comprobar_token_notion(notion: Notion) -> str | None:
    """None si el token funciona; si no, el motivo en lenguaje claro."""
    try:
        await notion.request("GET", "users/me")
    except NotionError as e:
        if "HTTP 401" in str(e):
            return "ese token de Notion no es válido"
        return "no he podido comprobar el token ahora mismo"
    return None


async def comprobar_acceso_notion(notion: Notion, endpoint: str) -> str | None:
    """None si la integración puede leer `endpoint` (databases/<id> o pages/<id>)."""
    try:
        await notion.request("GET", endpoint)
    except NotionError as e:
        msg = str(e)
        if "HTTP 404" in msg:
            return "no existe o no la has compartido con tu integración (menú ⋯ → Conexiones)"
        if "HTTP 400" in msg:
            return "ese enlace no corresponde a lo que esperaba"
        if "HTTP 401" in msg:
            return "el token de Notion no es válido"
        return "no he podido comprobarla ahora mismo"
    return None


async def comprobar_clave_gemini(clave: str) -> tuple[bool, str]:
    """Prueba la clave con una llamada mínima. Devuelve (se_guarda, mensaje)."""
    try:
        await genai.Client(api_key=clave).aio.models.generate_content(
            model=GEMINI_MODEL, contents="Di solo: ok",
            config=types.GenerateContentConfig(max_output_tokens=16),
        )
    except genai_errors.APIError as e:
        if _clave_rechazada(e):
            return False, f"Esa clave no es válida. Genera otra en {URL_CLAVE_GEMINI}."
        if e.code == 429:
            return True, "Clave válida guardada, aunque ahora mismo ha agotado su límite gratuito."
        return True, "Clave guardada, pero no he podido comprobarla ahora mismo (Gemini no respondió)."
    except Exception:
        return True, "Clave guardada, pero no he podido comprobarla ahora mismo."
    return True, "Clave de Gemini comprobada y guardada."


def embed_panel(perfil: Perfil | None) -> discord.Embed:
    def marca(ok: bool) -> str:
        return "✅" if ok else "❌"

    p = perfil
    embed = discord.Embed(
        title="⚙️ Configura tu Zapy",
        description=(
            "Pulsa un botón para rellenar cada parte. Solo es obligatoria la **clave de Gemini**; "
            "lo demás hace tus planes más precisos. Tus claves se guardan cifradas y nunca se muestran."
        ),
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="🔑 Gemini (obligatoria)",
        value=f"{marca(bool(p and p.tiene_gemini))} " + ("Conectada" if p and p.tiene_gemini else "Falta tu clave"),
        inline=False,
    )
    detalle = f"{p.curso or 'sin curso'} · {len(p.rutina)} caracteres de rutina" if p and p.rutina else "Falta tu rutina"
    embed.add_field(name="👤 Perfil y rutina", value=f"{marca(bool(p and p.rutina))} {detalle}", inline=False)
    unicas = asignaturas_unicas(p)
    embed.add_field(
        name="📓 Notion (opcional)",
        value=(
            f"{marca(bool(p and p.tiene_notion))} Token\n"
            f"{marca(bool(p and p.tiene_calendario))} Calendario de exámenes\n"
            f"{marca(bool(unicas))} Asignaturas"
            + (f": {', '.join(a for a, _ in unicas)[:600]}" if unicas else "")
        ),
        inline=False,
    )
    return embed


async def _avisar(interaction: discord.Interaction, texto: str) -> None:
    """Respuesta privada (solo la ve quien pulsó), antes o después de un defer."""
    if interaction.response.is_done():
        await interaction.followup.send(texto, ephemeral=True)
    else:
        await interaction.response.send_message(texto, ephemeral=True)


class _ModalBase(discord.ui.Modal):
    def __init__(self, vista: "PanelConfig", titulo: str):
        super().__init__(title=titulo)
        self.vista = vista

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Error en un formulario de configuración", exc_info=error)
        await _avisar(interaction, "❌ Algo ha fallado al guardar. Inténtalo de nuevo en un momento.")


class ModalGemini(_ModalBase):
    def __init__(self, vista: "PanelConfig"):
        super().__init__(vista, "Clave de Gemini")
        self.clave = discord.ui.TextInput(
            label="Clave de API (Google AI Studio)", placeholder="AIza...", min_length=20, max_length=200
        )
        self.add_item(self.clave)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        clave = self.clave.value.strip().strip("\"'` ")
        if len(clave) < 20 or any(c.isspace() for c in clave):
            await _avisar(interaction, f"❌ Esa clave no tiene el formato correcto. Cópiala entera desde {URL_CLAVE_GEMINI}.")
            return
        guardar, mensaje = await comprobar_clave_gemini(clave)
        if not guardar:
            await _avisar(interaction, f"❌ {mensaje}")
            return
        await bot.almacen.guardar_perfil(interaction.user.id, gemini_key=clave)
        invalidar_cache_usuario(interaction.user.id)
        await _avisar(interaction, f"✅ {mensaje}")
        await self.vista.refrescar()


class ModalPerfil(_ModalBase):
    def __init__(self, vista: "PanelConfig", perfil: Perfil | None):
        super().__init__(vista, "Tu perfil y rutina")
        self.curso = discord.ui.TextInput(
            label="Curso", placeholder="Ej.: 1º Bachillerato científico",
            default=(perfil.curso if perfil else "") or None, max_length=100,
        )
        self.rutina = discord.ui.TextInput(
            label="Tu rutina y horarios", style=discord.TextStyle.paragraph,
            placeholder="Clases, extraescolares, cuándo prefieres estudiar, qué se te da mal...",
            default=(perfil.rutina if perfil else "") or None, max_length=MAX_RUTINA_CHARS,
        )
        self.add_item(self.curso)
        self.add_item(self.rutina)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.almacen.guardar_perfil(
            interaction.user.id, curso=self.curso.value, rutina=self.rutina.value
        )
        await _avisar(interaction, "✅ Perfil y rutina guardados. Zapy ya los usa en tus planes.")
        await self.vista.refrescar()


class ModalNotion(_ModalBase):
    """Conectar Notion pegando los enlaces de bases de datos que el usuario ya tiene."""

    def __init__(self, vista: "PanelConfig", perfil: Perfil | None):
        super().__init__(vista, "Conectar tu Notion")
        self.tiene_token = bool(perfil and perfil.notion_token)
        self.token = discord.ui.TextInput(
            label="Token de tu integración de Notion",
            placeholder="Vacío = mantener el actual" if self.tiene_token else "ntn_... (o secret_...)",
            required=not self.tiene_token, max_length=300,
        )
        self.calendario = discord.ui.TextInput(
            label="Enlace de tu calendario de exámenes",
            placeholder="https://www.notion.so/...", required=False, max_length=300,
            default=(perfil.notion_calendario_id if perfil else "") or None,
        )
        previo = "\n".join(f"{a} = {i}" for a, i in asignaturas_unicas(perfil)) or None
        self.asignaturas = discord.ui.TextInput(
            label="Asignaturas (Nombre = enlace, una por línea)", style=discord.TextStyle.paragraph,
            placeholder="Mates = https://www.notion.so/...\nInglés = https://www.notion.so/...",
            required=False, max_length=3000, default=previo,
        )
        for campo in (self.token, self.calendario, self.asignaturas):
            self.add_item(campo)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = interaction.user.id
        perfil = await bot.almacen.obtener_perfil(uid)
        token = self.token.value.strip() or (perfil.notion_token if perfil else None)
        if not token:
            await _avisar(interaction, "❌ Necesito el token de tu integración de Notion.")
            return
        notion = Notion(token, bot.session)
        motivo = await comprobar_token_notion(notion)
        if motivo:
            await _avisar(interaction, f"❌ {motivo[0].upper() + motivo[1:]}. Mira el botón ❓ Ayuda.")
            return

        cambios: dict[str, str] = {"notion_token": token}
        informe = ["✅ Token de Notion comprobado."]

        texto_cal = self.calendario.value.strip()
        if texto_cal:
            cal_id = extraer_id_notion(texto_cal)
            motivo = await comprobar_acceso_notion(notion, f"databases/{cal_id}") if cal_id else "no encuentro un enlace válido"
            if motivo:
                informe.append(f"⚠️ Calendario: {motivo}.")
            else:
                cambios["notion_calendario_id"] = cal_id
                informe.append("✅ Calendario conectado.")

        nuevas, ilegibles = parsear_asignaturas(self.asignaturas.value)
        asignaturas_ok: dict[str, str] | None = None
        if nuevas or ilegibles:
            problemas = [f"«{linea}» no tiene el formato Nombre = enlace" for linea in ilegibles]
            if len(nuevas) > MAX_ASIGNATURAS:
                problemas.append(f"máximo {MAX_ASIGNATURAS} asignaturas")
            else:
                motivos = await asyncio.gather(
                    *(comprobar_acceso_notion(notion, f"databases/{i}") for i in nuevas.values())
                )
                problemas += [f"{nombre}: {m}" for (nombre, _), m in zip(nuevas.items(), motivos) if m]
            if problemas:
                informe.append("⚠️ No he cambiado tus asignaturas:\n" + "\n".join(f"  • {x}" for x in problemas))
            else:
                asignaturas_ok = nuevas
                informe.append(f"✅ {len(nuevas)} asignaturas conectadas.")

        await bot.almacen.guardar_perfil(uid, **cambios)
        if asignaturas_ok is not None:
            await bot.almacen.reemplazar_asignaturas(uid, construir_alias(asignaturas_ok))
        invalidar_cache_usuario(uid)
        await _avisar(interaction, "\n".join(informe)[:1900])
        await self.vista.refrescar()


def _cuerpo_base_datos(padre_id: str, titulo: str, con_fecha: bool) -> dict:
    props: dict = {"Nombre": {"title": {}}}
    if con_fecha:
        props["Fecha"] = {"date": {}}
    return {
        "parent": {"type": "page_id", "page_id": padre_id},
        "title": [{"type": "text", "text": {"content": titulo[:100]}}],
        "properties": props,
    }


class ModalNotionAuto(_ModalBase):
    """Crea el calendario y una base de datos por asignatura dentro de una página del usuario."""

    def __init__(self, vista: "PanelConfig", perfil: Perfil | None):
        super().__init__(vista, "Crear mi Notion automáticamente")
        self.tiene_token = bool(perfil and perfil.notion_token)
        self.token = discord.ui.TextInput(
            label="Token de tu integración de Notion",
            placeholder="Vacío = mantener el actual" if self.tiene_token else "ntn_... (o secret_...)",
            required=not self.tiene_token, max_length=300,
        )
        self.pagina = discord.ui.TextInput(
            label="Enlace de una página tuya (compartida)",
            placeholder="https://www.notion.so/... (la integración debe estar conectada)", max_length=300,
        )
        self.asignaturas = discord.ui.TextInput(
            label="Tus asignaturas (una por línea)", style=discord.TextStyle.paragraph,
            placeholder="Mates\nFísica y Química\nInglés", max_length=1000,
        )
        for campo in (self.token, self.pagina, self.asignaturas):
            self.add_item(campo)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = interaction.user.id
        perfil = await bot.almacen.obtener_perfil(uid)
        token = self.token.value.strip() or (perfil.notion_token if perfil else None)
        if not token:
            await _avisar(interaction, "❌ Necesito el token de tu integración de Notion.")
            return

        nombres: dict[str, str] = {}  # alias normalizado -> nombre tal como lo escribió
        for linea in self.asignaturas.value.splitlines():
            nombre = linea.strip()[:60]
            if nombre and _norm(nombre) not in nombres:
                nombres[_norm(nombre)] = nombre
        if not nombres or len(nombres) > MAX_ASIGNATURAS:
            await _avisar(interaction, f"❌ Escribe entre 1 y {MAX_ASIGNATURAS} asignaturas, una por línea.")
            return
        padre = extraer_id_notion(self.pagina.value)
        if not padre:
            await _avisar(interaction, "❌ No encuentro un enlace de página de Notion válido.")
            return

        notion = Notion(token, bot.session)
        motivo = await comprobar_token_notion(notion)
        if motivo:
            await _avisar(interaction, f"❌ {motivo[0].upper() + motivo[1:]}. Mira el botón ❓ Ayuda.")
            return
        motivo = await comprobar_acceso_notion(notion, f"pages/{padre}")
        if motivo:
            await _avisar(interaction, f"❌ Esa página: {motivo}.")
            return

        try:
            calendario = await notion.request("POST", "databases", _cuerpo_base_datos(padre, "Zapy · Calendario", True))
        except NotionError as e:
            log.warning("No pude crear el calendario (usuario %s): %s", uid, e)
            permisos = " Comprueba que tu integración tiene permiso para insertar contenido." if "HTTP 403" in str(e) else ""
            await _avisar(interaction, f"❌ No he podido crear el calendario en Notion.{permisos}")
            return

        resultados = await asyncio.gather(
            *(notion.request("POST", "databases", _cuerpo_base_datos(padre, f"Zapy · {n}", False)) for n in nombres.values()),
            return_exceptions=True,
        )
        creadas: dict[str, str] = {}
        fallidas: list[str] = []
        for nombre, res in zip(nombres.values(), resultados):
            if isinstance(res, dict) and res.get("id"):
                creadas[nombre] = res["id"]
            else:
                fallidas.append(nombre)
                log.warning("No pude crear la base de %s (usuario %s): %s", nombre, uid, res)

        await bot.almacen.guardar_perfil(uid, notion_token=token, notion_calendario_id=calendario["id"])
        if creadas:
            await bot.almacen.reemplazar_asignaturas(uid, construir_alias(creadas))
        invalidar_cache_usuario(uid)

        informe = [f"✅ He creado tu calendario y {len(creadas)} bases de asignaturas en Notion y ya están conectadas."]
        if fallidas:
            informe.append("⚠️ No pude crear: " + ", ".join(fallidas) + ". Vuelve a intentarlo solo con esas.")
        informe.append("Las bases anteriores, si las había, no se han borrado. Añade páginas con tu método de "
                       "estudio a una asignatura y Zapy lo usará en `!apuntes`.")
        await _avisar(interaction, "\n".join(informe)[:1900])
        await self.vista.refrescar()


class ConfirmarBorrado(discord.ui.View):
    def __init__(self, user_id: int, vista: "PanelConfig | None" = None):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.vista = vista

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Sí, borrar todo", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bot.almacen.borrar_usuario(self.user_id)
        invalidar_cache_usuario(self.user_id)
        self.stop()
        await interaction.response.edit_message(
            content="🗑️ Listo: he borrado tu perfil, tus claves y tus asignaturas. Lo que hay en tu "
                    "Notion no se ha tocado. Puedes volver a empezar con `!configurar`.",
            view=None,
        )
        if self.vista:
            await self.vista.refrescar()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Vale, no he borrado nada.", view=None)


class PanelConfig(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.mensaje: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Este panel es de otra persona. Escribe `!configurar` para abrir el tuyo.", ephemeral=True
            )
            return False
        return True

    async def refrescar(self) -> None:
        if self.mensaje is None:
            return
        perfil = await bot.almacen.obtener_perfil(self.user_id)
        try:
            await self.mensaje.edit(embed=embed_panel(perfil), view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.mensaje is not None:
            try:
                await self.mensaje.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log.error("Error en el panel de configuración", exc_info=error)
        await _avisar(interaction, "❌ Algo ha fallado. Vuelve a abrir el panel con `!configurar`.")

    @discord.ui.button(label="Clave de Gemini", emoji="🔑", style=discord.ButtonStyle.primary, row=0)
    async def boton_gemini(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalGemini(self))

    @discord.ui.button(label="Perfil y rutina", emoji="👤", style=discord.ButtonStyle.primary, row=0)
    async def boton_perfil(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalPerfil(self, await bot.almacen.obtener_perfil(self.user_id)))

    @discord.ui.button(label="Conectar Notion", emoji="📓", style=discord.ButtonStyle.secondary, row=0)
    async def boton_notion(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalNotion(self, await bot.almacen.obtener_perfil(self.user_id)))

    @discord.ui.button(label="Crear mi Notion", emoji="✨", style=discord.ButtonStyle.secondary, row=0)
    async def boton_notion_auto(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalNotionAuto(self, await bot.almacen.obtener_perfil(self.user_id)))

    @discord.ui.button(label="Ayuda", emoji="❓", style=discord.ButtonStyle.secondary, row=1)
    async def boton_ayuda(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(AYUDA_CONFIG, ephemeral=True)

    @discord.ui.button(label="Borrar mis datos", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def boton_borrar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "¿Seguro? Se borrarán tu perfil, tu rutina, tus claves y tus asignaturas de Zapy.",
            view=ConfirmarBorrado(self.user_id, self), ephemeral=True,
        )


# ----------------------------------------------------------------------------
# COMANDOS
# ----------------------------------------------------------------------------
@bot.command(name="comandos")
async def mostrar_comandos(ctx: commands.Context):
    embed = discord.Embed(title="🤖 Comandos de Zapy", color=discord.Color.blue())
    embed.add_field(name="⚙️ Configurar", value="`!configurar` - Tu clave de Gemini, tu rutina y tu Notion.", inline=False)
    embed.add_field(name="📅 Notion", value="`!eventos` - Lista los próximos exámenes/tareas.", inline=False)
    embed.add_field(name="➕ Añadir", value="`!añadir Nombre | AAAA-MM-DD` - Guarda un evento.", inline=False)
    embed.add_field(name="🚀 Masterclass", value="`!apuntes Asignatura | Tema` - Genera apuntes en Notion.", inline=False)
    embed.add_field(name="🧹 Limpiar", value="`!clear [n]` - Borra mensajes (requiere permiso).", inline=False)
    embed.add_field(name="💬 Planificar", value="Menciona a Zapy o escríbele por DM.", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="configurar", aliases=["config"])
@commands.cooldown(1, 10, commands.BucketType.user)
async def configurar(ctx: commands.Context):
    perfil = await bot.almacen.obtener_perfil(ctx.author.id)
    vista = PanelConfig(ctx.author.id)
    embed = embed_panel(perfil)
    try:  # por privado: así el panel no queda a la vista de todo el servidor
        vista.mensaje = await ctx.author.send(embed=embed, view=vista)
    except discord.Forbidden:
        vista.mensaje = await ctx.send(
            "⚠️ Tienes los mensajes privados cerrados, así que te muestro el panel aquí. "
            "Tus claves solo las ve Zapy: se escriben en formularios privados.",
            embed=embed, view=vista,
        )
        return
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("📩 Te he enviado el panel de configuración por mensaje privado.", delete_after=20)


@bot.command(name="borrar_mis_datos")
@commands.cooldown(1, 10, commands.BucketType.user)
async def borrar_mis_datos(ctx: commands.Context):
    await ctx.send(
        "¿Seguro? Se borrarán tu perfil, tu rutina, tus claves y tus asignaturas de Zapy.",
        view=ConfirmarBorrado(ctx.author.id),
    )


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

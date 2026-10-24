"""Zapy 1.4.0 (en desarrollo) - Tutor académico multiusuario en Discord (Gemini + Notion).

Modo multiusuario: datos por usuario en SQLite con tokens cifrados (Fernet), Notion,
prompt y clave de Gemini propios de cada persona, y `!configurar` (panel con botones y
formularios) para que cada usuario se configure sin tocar el servidor, y un planificador
con temporizador (empezar/pausar/reanudar/terminar) que aprende cuánto tardas de verdad.
Dependencias: discord.py>=2.3, aiohttp (viene con discord.py), python-dotenv,
google-genai, cryptography y, en Windows, tzdata (para zoneinfo).
"""
from __future__ import annotations

import ast
import asyncio
import datetime as dt
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape
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
MAX_TOKENS_MASTERCLASS = 16384  # incluye los tokens de razonamiento del modelo
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


SYSTEM_PROMPT_MASTERCLASS = r"""Zapy, actúa como un catedrático y tutor académico de excelencia, especialista en pedagogía de alto rendimiento y en la preparación de exámenes de ESO y Bachillerato. Transformas temarios complejos en "Masterclasses" rigurosas, claras e imborrables para la memoria.

OBJETIVO
Elabora una Masterclass completa sobre el tema que te pidan, para un estudiante que busca sacar un 10 en su examen. Debe poder entenderse a la primera aunque no sepa nada del tema y, a la vez, cubrir todo lo que puede caer en el examen. La claridad va por delante del lucimiento: cada concepto se explica con palabras sencillas, se ilustra con un ejemplo concreto y solo después se formaliza.

ESTRUCTURA (respétala; un encabezado ## por fase)
## Introducción
Rápida y clara: de qué trata la masterclass, qué vas a aprender a hacer y qué conviene saber antes. Ejemplo de tono: "Esta masterclass trata de las ecuaciones de segundo grado: aquí aprenderás a resolverlas paso a paso y al final tendrás ejercicios de prueba."
## Fase de aprendizaje
Enséñame paso a paso a hacer el ejercicio o a dominar el tema, con subapartados ###. Para cada concepto: (1) la idea intuitiva, con una analogía o una situación cotidiana; (2) la definición o regla exacta, en **negrita**; (3) un ejemplo resuelto en el que se vean TODOS los pasos intermedios y se explique el porqué de cada uno. Ve de lo simple a lo complejo y encadena cada apartado con el anterior. Si es una masterclass de idiomas, pon los ejemplos en la lengua que se quiere aprender (con su traducción al castellano). Cierra esta fase con una "Chuleta" en un aviso [!IMPORTANTE] con lo imprescindible (definiciones, fórmulas y reglas clave) para repasar el día antes del examen.
## Ejercicios de práctica
Penúltima fase: crea ejercicios para practicar lo aprendido, numerados y ordenados de más fáciles a más difíciles (el último, de nivel examen). Sin soluciones en esta fase.
## Errores típicos y corrección
Última fase: enseña los errores más habituales con su explicación y da la corrección de TODOS los ejercicios de práctica con su razonamiento paso a paso. Pon cada corrección dentro de un desplegable, para que el estudiante lo intente antes de mirar.

CALIDAD
- Define cada término técnico la primera vez que aparezca. Frases cortas y directas; nada de relleno ni de repetir lo mismo con otras palabras.
- En cálculos y razonamientos muestra todos los pasos intermedios y comprueba mentalmente cada resultado numérico antes de escribirlo.
- Rigor: no inventes datos, fechas, fórmulas, nombres ni citas. Si no estás seguro de algo, no lo incluyas.
- Cubre el tema completo, sin omitir apartados por extensos que sean, pero sin salirte del temario ni del nivel del curso del estudiante.
- Tono cercano, motivador y serio. No añadas saludos ni comentarios fuera de la masterclass (nada de "Aquí tienes...") y no la envuelvas en un bloque de código: responde solo con el Markdown.

HERRAMIENTAS VISUALES (úsalas cuando ayuden a entender; mejor pocas y muy claras que muchas)
1. FÓRMULAS (LaTeX compatible con KaTeX): en línea con $...$ y centradas con $$ en una línea, la fórmula en la siguiente y $$ en otra. Una sola fórmula por bloque y de menos de 400 caracteres. Reglas: para varias líneas alineadas usa \begin{aligned} ... \end{aligned} (nunca align ni equation); sin \label, \ref, \ce, siunitx ni paquetes; unidades con \text{...} (por ejemplo $9{,}8\ \text{m/s}^2$); fórmulas químicas con \mathrm{H_2O}; nunca un $ dentro de una fórmula. Tras cada fórmula importante explica qué significa cada variable y sus unidades.
2. DIAGRAMAS (Mermaid), en un bloque ```mermaid. Úsalos para procesos, algoritmos, causa y efecto, clasificaciones, ciclos y secuencias (máximo 3 por masterclass). Permitidos: flowchart TD o LR, sequenceDiagram, stateDiagram-v2, classDiagram, erDiagram, pie y gantt. Reglas estrictas: ids de nodo simples (A, B, C1...); TODAS las etiquetas entre comillas dobles; flechas con texto como -->|"Sí"|; máximo 12 nodos y etiquetas de menos de 40 caracteres; sin HTML, sin style/classDef/click y sin paréntesis fuera de las comillas. Ejemplo:
```mermaid
flowchart TD
    A["Inicio"] --> B{"¿El discriminante es positivo?"}
    B -->|"Sí"| C["Dos soluciones reales"]
    B -->|"No"| D["Sin soluciones reales"]
```
3. GRÁFICAS (las dibuja Zapy), en un bloque ```grafica. Úsalas para funciones, cinemática, estadística... (máximo 3 por masterclass). Una clave por línea. Ejemplo:
```grafica
titulo: Parábola y = x² − 4
x: -4, 4
funcion: x^2 - 4 | y = x² − 4
marcar: (2,0) raíz
marcar: (-2,0) raíz
marcar: (0,-4) vértice
ejex: x
ejey: y
```
Reglas: las expresiones usan x con + - * / ^, paréntesis, las funciones sin cos tan asin acos atan sqrt exp abs ln (neperiano) y log (base 10) y las constantes pi y e. Hasta 3 líneas funcion: o series de datos con puntos: (0,0) (1,3) (2,5) | Nombre. El rango x: es obligatorio si hay funciones; y: es opcional. Con marcar: (x,y) etiqueta señalas puntos importantes (raíces, vértice, cortes): comprueba que de verdad están sobre la gráfica.
4. TABLAS Markdown, para comparar, clasificar, conjugar o listar vocabulario (máximo 6 columnas).
5. AVISOS: citas cuya primera línea lleva una etiqueta: > [!IDEA] (intuición o analogía), > [!DEFINICION] (definición o regla exacta), > [!EJEMPLO], > [!TRUCO] (regla mnemotécnica o atajo), > [!ERROR] (error típico) o > [!IMPORTANTE] (lo que más cae en el examen). Tras la etiqueta puedes poner un título breve; el contenido va en las líneas siguientes, también con >.
6. DESPLEGABLES: <details><summary>Título</summary> y </details>, cada uno en su línea y con el contenido entre medias.
7. CÓDIGO: bloques con su lenguaje (python, arduino, bash, c++...) en tecnología, robótica o digitalización; comenta las líneas importantes y no pases de 40 líneas.
No uses imágenes, enlaces a imágenes, arte ASCII ni más HTML que details y summary.
"""


PISTAS_ASIGNATURA = {
    "mates": "Prioriza el razonamiento paso a paso con fórmulas LaTeX y ejemplos numéricos sencillos antes de los de nivel examen. Si el tema es de funciones, geometría analítica, trigonometría o estadística, incluye gráficas con los puntos clave marcados.",
    "fyq": "Escribe siempre las magnitudes con sus unidades y despeja las fórmulas en los ejemplos. En cinemática y dinámica usa gráficas (posición-tiempo, velocidad-tiempo). En química, ajusta las reacciones paso a paso y usa tablas para propiedades y clasificaciones. Los procesos, ciclos y clasificaciones, como diagramas.",
    "tecno": "Usa diagramas de flujo para procesos, algoritmos y sistemas de control, y tablas para comparar materiales, componentes o tecnologías. Si hay código, que sea breve y comentado.",
    "digi": "Usa diagramas para explicar procesos, redes y flujos de información, y tablas para comparar herramientas o conceptos. Si hay código, que sea breve y comentado.",
    "robotica": "Usa diagramas de flujo para la lógica del programa y para los sistemas sensor-controlador-actuador. El código (arduino o python) breve y comentado, y las fórmulas de movimiento o electricidad en LaTeX.",
    "euskera": "Explica en castellano, pero pon todos los ejemplos en euskera con su traducción. Usa tablas para declinaciones, conjugaciones y vocabulario.",
    "lengua": "Usa tablas para clasificaciones (tipos de oraciones, categorías gramaticales...) y ejemplos analizados paso a paso. Diagramas solo si ayudan de verdad.",
    "ingles": "Explica en castellano y pon todos los ejemplos en inglés con su traducción. Usa tablas para tiempos verbales, vocabulario y estructuras.",
    "geo-hist": "Presenta las causas y consecuencias con diagramas, las cronologías como tabla (fecha | hecho) y los conceptos con definiciones exactas. Incluye solo fechas, nombres y datos de los que estés totalmente seguro.",
}
PISTA_GENERICA = ("Adapta los recursos visuales a la materia: diagramas para procesos y relaciones, tablas para "
                  "comparar y gráficas para funciones o datos.")


def pistas_asignatura(asignatura: str) -> str:
    clave = (alias_equivalentes(asignatura) or [""])[0]
    return PISTAS_ASIGNATURA.get(clave, PISTA_GENERICA)


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

    async def request(self, method: str, endpoint: str, payload: dict | None = None, *,
                      archivo: tuple[bytes, str, str] | None = None) -> dict:
        url = f"{self.BASE}/{endpoint.lstrip('/')}"
        ultimo_error = "sin respuesta"
        for intento in range(3):
            espera = 2**intento
            try:
                if archivo is None:
                    opciones = {"headers": self._headers, "json": payload}
                else:  # multipart: un FormData no se puede reutilizar, se rehace en cada intento
                    contenido, nombre, mime = archivo
                    formulario = aiohttp.FormData()
                    formulario.add_field("file", contenido, filename=nombre, content_type=mime)
                    opciones = {"headers": {k: v for k, v in self._headers.items() if k != "Content-Type"},
                                "data": formulario}
                async with self._sem, self._session.request(method, url, timeout=NOTION_TIMEOUT, **opciones) as resp:
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

    async def subir_archivo(self, contenido: bytes, nombre: str, mime: str) -> str:
        """Sube un archivo pequeño con la API de subida directa de Notion y devuelve su ID para adjuntarlo."""
        if len(contenido) > MAX_ARCHIVO_BYTES:
            raise NotionError("el archivo supera el límite de Notion")
        creado = await self.request("POST", "file_uploads", {"filename": nombre, "content_type": mime})
        await self.request("POST", f"file_uploads/{creado['id']}/send", archivo=(contenido, nombre, mime))
        return creado["id"]

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


@dataclass(frozen=True)
class Tarea:
    """Una tarea con temporizador. Las marcas de tiempo son epoch (segundos) para sobrevivir a reinicios."""
    id: int
    user_id: int
    asignatura: str          # tal como la escribió el usuario
    clave: str               # asignatura normalizada (Mates y Matemáticas comparten clave)
    descripcion: str
    tipo: str                # practica | teoria | memoria | redaccion | otros
    estimado_min: int | None
    estado: str              # en_curso | pausada | terminada | abandonada
    inicio_ts: int
    reanudado_ts: int | None  # inicio del tramo actual; None si está en pausa o cerrada
    acumulado_s: int         # segundos contados en tramos ya cerrados (la pausa no cuenta)
    pausas: int
    fin_ts: int | None
    real_s: int | None
    cansancio: int | None
    dificultad: int | None
    manual: bool

    def transcurrido_s(self, ahora: int) -> int:
        if self.estado in {"terminada", "abandonada"} and self.real_s is not None:
            return self.real_s
        extra = max(0, ahora - self.reanudado_ts) if self.reanudado_ts is not None else 0
        return self.acumulado_s + extra


_COLS_TAREA = ("id, user_id, asignatura, clave, descripcion, tipo, estimado_min, estado, inicio_ts, "
               "reanudado_ts, acumulado_s, pausas, fin_ts, real_s, cansancio, dificultad, manual")


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
    CREATE TABLE IF NOT EXISTS tareas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES usuarios(user_id) ON DELETE CASCADE,
        asignatura TEXT NOT NULL,
        clave TEXT NOT NULL,
        descripcion TEXT NOT NULL DEFAULT '',
        tipo TEXT NOT NULL DEFAULT 'otros',
        estimado_min INTEGER,
        estado TEXT NOT NULL CHECK (estado IN ('en_curso', 'pausada', 'terminada', 'abandonada')),
        inicio_ts INTEGER NOT NULL,
        reanudado_ts INTEGER,
        acumulado_s INTEGER NOT NULL DEFAULT 0,
        pausas INTEGER NOT NULL DEFAULT 0,
        fin_ts INTEGER,
        real_s INTEGER,
        cansancio INTEGER CHECK (cansancio BETWEEN 1 AND 5),
        dificultad INTEGER CHECK (dificultad BETWEEN 1 AND 5),
        manual INTEGER NOT NULL DEFAULT 0
    );
    CREATE UNIQUE INDEX IF NOT EXISTS una_tarea_activa ON tareas(user_id)
        WHERE estado IN ('en_curso', 'pausada');
    CREATE INDEX IF NOT EXISTS tareas_por_usuario ON tareas(user_id, estado, fin_ts);
    """

    def __init__(self, ruta: Path, boveda: Boveda):
        self._boveda = boveda
        self._lock = threading.Lock()
        self._con = sqlite3.connect(ruta, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        with self._lock:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute("PRAGMA synchronous=NORMAL")  # seguro con WAL; menos esperas en la SD
            self._con.execute("PRAGMA busy_timeout=5000")
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

    async def iniciar_tarea(self, user_id: int, asignatura: str, clave: str, descripcion: str, tipo: str,
                            estimado_min: int | None, ahora: int) -> Tarea | None:
        """None si el usuario ya tiene una tarea en curso o en pausa."""
        return await asyncio.to_thread(self._iniciar_tarea, user_id, asignatura, clave, descripcion,
                                       tipo, estimado_min, ahora)

    async def tarea_activa(self, user_id: int) -> Tarea | None:
        return await asyncio.to_thread(self._tarea_activa, user_id)

    async def pausar_tarea(self, user_id: int, ahora: int) -> Tarea | None:
        return await asyncio.to_thread(self._pausar_tarea, user_id, ahora)

    async def reanudar_tarea(self, user_id: int, ahora: int) -> Tarea | None:
        return await asyncio.to_thread(self._reanudar_tarea, user_id, ahora)

    async def cerrar_tarea(self, user_id: int, ahora: int, estado: str,
                           minutos_manual: int | None = None) -> Tarea | None:
        """Cierra la tarea activa como 'terminada' o 'abandonada'."""
        return await asyncio.to_thread(self._cerrar_tarea, user_id, ahora, estado, minutos_manual)

    async def valorar_tarea(self, tarea_id: int, user_id: int, cansancio: int | None = None,
                            dificultad: int | None = None) -> bool:
        return await asyncio.to_thread(self._valorar_tarea, tarea_id, user_id, cansancio, dificultad)

    async def tareas_terminadas(self, user_id: int, limite: int = 1000) -> list[Tarea]:
        return await asyncio.to_thread(self._tareas_terminadas, user_id, limite)

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

    @staticmethod
    def _a_tarea(f: sqlite3.Row) -> Tarea:
        return Tarea(
            id=f["id"], user_id=f["user_id"], asignatura=f["asignatura"], clave=f["clave"],
            descripcion=f["descripcion"], tipo=f["tipo"], estimado_min=f["estimado_min"], estado=f["estado"],
            inicio_ts=f["inicio_ts"], reanudado_ts=f["reanudado_ts"], acumulado_s=f["acumulado_s"],
            pausas=f["pausas"], fin_ts=f["fin_ts"], real_s=f["real_s"], cansancio=f["cansancio"],
            dificultad=f["dificultad"], manual=bool(f["manual"]),
        )

    def _activa_sin_lock(self, user_id: int) -> Tarea | None:
        fila = self._con.execute(
            f"SELECT {_COLS_TAREA} FROM tareas WHERE user_id = ? AND estado IN ('en_curso', 'pausada')",
            (user_id,),
        ).fetchone()
        return self._a_tarea(fila) if fila else None

    def _tarea_activa(self, user_id: int) -> Tarea | None:
        with self._lock:
            return self._activa_sin_lock(user_id)

    def _iniciar_tarea(self, user_id, asignatura, clave, descripcion, tipo, estimado_min, ahora):
        iso = _ahora_iso()
        with self._lock:
            try:
                with self._con:
                    self._con.execute(
                        "INSERT OR IGNORE INTO usuarios (user_id, creado, actualizado) VALUES (?, ?, ?)",
                        (user_id, iso, iso),
                    )
                    self._con.execute(
                        "INSERT INTO tareas (user_id, asignatura, clave, descripcion, tipo, estimado_min, "
                        "estado, inicio_ts, reanudado_ts) VALUES (?, ?, ?, ?, ?, ?, 'en_curso', ?, ?)",
                        (user_id, asignatura, clave, descripcion, tipo, estimado_min, ahora, ahora),
                    )
            except sqlite3.IntegrityError:  # el índice único: ya hay una tarea activa
                return None
            return self._activa_sin_lock(user_id)

    def _pausar_tarea(self, user_id: int, ahora: int) -> Tarea | None:
        with self._lock, self._con:
            cur = self._con.execute(
                "UPDATE tareas SET acumulado_s = acumulado_s + MAX(0, ? - reanudado_ts), reanudado_ts = NULL, "
                "pausas = pausas + 1, estado = 'pausada' WHERE user_id = ? AND estado = 'en_curso'",
                (ahora, user_id),
            )
            return self._activa_sin_lock(user_id) if cur.rowcount else None

    def _reanudar_tarea(self, user_id: int, ahora: int) -> Tarea | None:
        with self._lock, self._con:
            cur = self._con.execute(
                "UPDATE tareas SET estado = 'en_curso', reanudado_ts = ? WHERE user_id = ? AND estado = 'pausada'",
                (ahora, user_id),
            )
            return self._activa_sin_lock(user_id) if cur.rowcount else None

    def _cerrar_tarea(self, user_id: int, ahora: int, estado: str, minutos_manual: int | None) -> Tarea | None:
        if estado not in {"terminada", "abandonada"}:
            raise ValueError(f"Estado de cierre no válido: {estado}")
        with self._lock, self._con:
            tarea = self._activa_sin_lock(user_id)
            if tarea is None:
                return None
            real = tarea.transcurrido_s(ahora)
            manual = 0
            if minutos_manual is not None:
                real, manual = minutos_manual * 60, 1
            self._con.execute(
                "UPDATE tareas SET estado = ?, fin_ts = ?, real_s = ?, acumulado_s = ?, manual = ?, "
                "reanudado_ts = NULL WHERE id = ?",
                (estado, ahora, real, real, manual, tarea.id),
            )
            fila = self._con.execute(f"SELECT {_COLS_TAREA} FROM tareas WHERE id = ?", (tarea.id,)).fetchone()
            return self._a_tarea(fila)

    def _valorar_tarea(self, tarea_id: int, user_id: int, cansancio: int | None, dificultad: int | None) -> bool:
        with self._lock, self._con:
            cur = self._con.execute(
                "UPDATE tareas SET cansancio = COALESCE(?, cansancio), dificultad = COALESCE(?, dificultad) "
                "WHERE id = ? AND user_id = ? AND estado = 'terminada'",
                (cansancio, dificultad, tarea_id, user_id),
            )
            return cur.rowcount > 0

    def _tareas_terminadas(self, user_id: int, limite: int) -> list[Tarea]:
        with self._lock:
            filas = self._con.execute(
                f"SELECT {_COLS_TAREA} FROM tareas WHERE user_id = ? AND estado = 'terminada' "
                "ORDER BY fin_ts DESC LIMIT ?",
                (user_id, limite),
            ).fetchall()
        return [self._a_tarea(f) for f in filas]


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


def construir_system_prompt(perfil: Perfil, eventos: str, ahora: dt.datetime, habitos: str = "") -> str:
    rutina = perfil.rutina or (
        "(Todavía no ha escrito su rutina. Si te pide un plan, pregúntale por sus horarios "
        "o recuérdale que puede guardarla con `!configurar`.)"
    )
    curso = f"CURSO DEL ESTUDIANTE: {perfil.curso}\n\n" if perfil.curso else ""
    bloque_habitos = (
        "\n\nHÁBITOS MEDIDOS DEL ESTUDIANTE (calculados por Zapy con sus tareas reales; úsalos para estimar "
        f"duraciones y no inventes otros datos):\n{habitos}" if habitos else ""
    )
    return (
        f"{PERSONA_BASE}\n\n{INSTRUCCION_FORMATO}\n\n{curso}"
        f"PERFIL Y RUTINA DEL ESTUDIANTE:\n{rutina}\n\n"
        f"HOY ES: {DIAS_SEMANA[ahora.weekday()]}, {ahora:%d/%m/%Y}, {ahora:%H:%M}\n\n"
        f"EXÁMENES Y EVENTOS PRÓXIMOS EN NOTION:\n{eventos}{bloque_habitos}"
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


# --- Markdown -> bloques de Notion (fórmulas, diagramas, gráficas, tablas, avisos y desplegables) ---
RICH_MAX = 2000  # límite de Notion por trozo de texto
EQ_MAX = 1000  # límite de Notion por ecuación
MAX_GRAFICAS = 4
MAX_ARCHIVO_BYTES = 5 * 1024 * 1024  # tope de Notion por archivo en espacios gratuitos
_LENGUAJES_CODIGO = {"python", "java", "javascript", "typescript", "json", "bash", "shell", "c", "c++", "c#",
                     "sql", "html", "css", "arduino", "markdown", "latex", "mermaid"}
_ALIAS_LENGUAJE = {"py": "python", "js": "javascript", "ts": "typescript", "cpp": "c++", "csharp": "c#",
                   "sh": "shell", "ino": "arduino", "md": "markdown", "tex": "latex"}
_RE_INLINE = re.compile(
    r"(\$\$[^$\n]+\$\$|\$(?!\s)[^$\n]+?(?<!\s)\$|\*\*[^*\n]+\*\*|`[^`\n]+`"
    r"|(?<![\w*])\*(?![\s*])[^*\n]+?(?<![\s*])\*(?![\w*]))"
)
_CALLOUTS = {  # etiqueta -> (emoji, color de Notion, título por defecto)
    "IDEA": ("🧠", "purple_background", "Idea clave"),
    "DEFINICION": ("📖", "blue_background", "Definición"),
    "DEF": ("📖", "blue_background", "Definición"),
    "EJEMPLO": ("✏️", "green_background", "Ejemplo"),
    "TRUCO": ("💡", "yellow_background", "Truco"),
    "ERROR": ("⚠️", "red_background", "Error típico"),
    "OJO": ("👀", "orange_background", "Ojo"),
    "IMPORTANTE": ("⭐", "orange_background", "Importante"),
    "NOTE": ("📝", "gray_background", "Nota"),
    "TIP": ("💡", "yellow_background", "Consejo"),
    "WARNING": ("⚠️", "red_background", "Atención"),
    "CAUTION": ("⛔", "red_background", "Cuidado"),
}
_RE_CALLOUT = re.compile(r"^\[!\s*([^\]\s]+)\s*\]\s*(.*)$")


def _limpiar_texto(texto: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", texto.replace("\r\n", "\n")).strip()


# --- Fórmulas: LaTeX compatible con KaTeX (el que usa Notion) ----------------------
_LATEX_PROHIBIDO = re.compile(
    r"\\(?:usepackage|newcommand|renewcommand|def|input|include|write|href|url|includegraphics|"
    r"eqref|ref|cite|SI|si|qty|num|unit|cancelto|mathbbm|require|ce)(?![A-Za-z])"
)


# Comandos \xxx que reconoce KaTeX 0.16 (el motor de fórmulas de Notion), extraídos del propio KaTeX.
_COMANDOS_KATEX = frozenset((
    "AA Alpha And Bbb Bbbk Beta Big Bigg Biggl Biggm Biggr Bigl Bigm Bigr Box Bra Braket Bumpeq Cap "
    "Chi Colonapprox Coloneq Coloneqq Colonsim Complex Cup DOTSB DOTSI DOTSX Dagger Darr Delta "
    "Diamond Doteq Downarrow Epsilon Eqcolon Eqqcolon Eta Finv Game Gamma H Harr Huge Im Iota Join "
    "KaTeX Kappa Ket LARGE LaTeX Lambda Large Larr Leftarrow Leftrightarrow Lleftarrow Longleftarrow "
    "Longleftrightarrow Longrightarrow Lrarr Lsh Mu N Nu Omega Omicron Overrightarrow P Phi Pi Pr Psi "
    "R Rarr Re Reals Rho Rightarrow Rrightarrow Rsh S Set Sigma Subset Supset Tau TeX TextOrMath "
    "Theta Uarr Uparrow Updownarrow Upsilon Vdash Vert Vvdash Xi Z Zeta aa above acute alef alefsym "
    "aleph allowbreak alpha amalg angl angle angln approx approxcolon approxcoloncolon approxeq "
    "arccos arcctg arcsin arctan arctg arg argmax argmin ast asymp atop backepsilon backprime backsim "
    "backsimeq backslash bar barwedge bcancel because begin begingroup beta beth between bf bgroup "
    "big bigcap bigcirc bigcup bigg biggl biggm biggr bigl bigm bigodot bigoplus bigotimes bigr "
    "bigsqcup bigstar bigtriangledown bigtriangleup biguplus bigvee bigwedge binom blacklozenge "
    "blacksquare blacktriangle blacktriangledown blacktriangleleft blacktriangleright blue blueA "
    "blueB blueC blueD blueE bm bmod bold boldsymbol bot bowtie boxdot boxed boxminus boxplus "
    "boxtimes bra brace brack braket breve bull bullet bumpeq c cal cancel cap cdot cdotp cdots "
    "centerdot cfrac ch char check checkmark chi choose circ circeq circlearrowleft circlearrowright "
    "circledR circledS circledast circledcirc circleddash clap clubs clubsuit cnums colon colonapprox "
    "coloncolon coloncolonapprox coloncolonequals coloncolonminus coloncolonsim coloneq coloneqq "
    "colonequals colonminus colonsim color colorbox complement cong coprod copyright cos cosec cosh "
    "cot cotg coth csc ctg cth cup curlyeqprec curlyeqsucc curlyvee curlywedge curvearrowleft "
    "curvearrowright dArr dag dagger daleth darr dashleftarrow dashrightarrow dashv dbinom dblcolon "
    "ddag ddagger ddddot dddot ddot ddots def deg degree delta det dfrac diagdown diagup diamond "
    "diamonds diamondsuit digamma dim displaystyle div divideontimes dot doteq doteqdot dotplus dots "
    "dotsb dotsc dotsi dotsm dotso dotsx doublebarwedge doublecap doublecup downarrow downdownarrows "
    "downharpoonleft downharpoonright edef egroup ell emph empty emptyset end endgroup enskip enspace "
    "epsilon eqcirc eqcolon eqqcolon eqsim eqslantgtr eqslantless equalscolon equalscoloncolon equiv "
    "errmessage eta eth exist exists exp expandafter fallingdotseq fbox fcolorbox flat footnotesize "
    "forall frac frak frown futurelet gamma gcd gdef ge genfrac geq geqq geqslant gets gg ggg gggtr "
    "gimel global gnapprox gneq gneqq gnsim goldA goldB goldC goldD goldE grave gray grayA grayB "
    "grayC grayD grayE grayF grayG grayH grayI green greenA greenB greenC greenD greenE gt gtrapprox "
    "gtrdot gtreqless gtreqqless gtrless gtrsim gvertneqq hArr harr hat hbar hbox hdashline hearts "
    "heartsuit hline hom hookleftarrow hookrightarrow hphantom href hskip hslash hspace htmlClass "
    "htmlData htmlId htmlStyle huge iff iiint iint image imageof imath impliedby implies in "
    "includegraphics inf infin infty injlim int intercal intop iota isin it jmath kaBlue kaGreen "
    "kappa ker kern ket lArr lBrace lVert lambda land lang langle large larr lbrace lbrack lceil "
    "ldotp ldots le leadsto left leftarrow leftarrowtail leftharpoondown leftharpoonup leftleftarrows "
    "leftrightarrow leftrightarrows leftrightharpoons leftrightsquigarrow leftthreetimes leq leqq "
    "leqslant lessapprox lessdot lesseqgtr lesseqqgtr lessgtr lesssim let lfloor lg lgroup lhd lim "
    "liminf limits limsup ll llap llbracket llcorner lll llless lmoustache ln lnapprox lneq lneqq "
    "lnot lnsim log long longleftarrow longleftrightarrow longmapsto longrightarrow looparrowleft "
    "looparrowright lor lozenge lparen lq lrArr lrarr lrcorner lt ltimes lvert lvertneqq maltese "
    "mapsto maroonA maroonB maroonC maroonD maroonE mathbb mathbf mathbin mathcal mathchoice mathclap "
    "mathclose mathellipsis mathfrak mathinner mathit mathllap mathnormal mathop mathopen mathord "
    "mathpunct mathrel mathring mathrlap mathrm mathscr mathsf mathsfit mathsterling mathstrut mathtt "
    "max measuredangle medspace message mho mid middle min mintA mintB mintC minuscolon "
    "minuscoloncolon minuso mkern mod models mp mskip mu multimap nLeftarrow nLeftrightarrow "
    "nRightarrow nVDash nVdash nabla natnums natural ncong ne nearrow neg negmedspace negthickspace "
    "negthinspace neq newcommand newline nexists ngeq ngeqq ngeqslant ngtr ni nleftarrow "
    "nleftrightarrow nleq nleqq nleqslant nless nmid nobreak nobreakspace noexpand nolimits nonumber "
    "normalsize not notag notin notni nparallel nprec npreceq nrightarrow nshortmid nshortparallel "
    "nsim nsubseteq nsubseteqq nsucc nsucceq nsupseteq nsupseteqq ntriangleleft ntrianglelefteq "
    "ntriangleright ntrianglerighteq nu nvDash nvdash nwarrow odot oiiint oiint oint omega omicron "
    "ominus operatorname operatornamewithlimits oplus orange ordinarycolon origof oslash otimes over "
    "overbrace overbracket overgroup overleftarrow overleftharpoon overleftrightarrow overline "
    "overlinesegment overrightarrow overrightharpoon overset owns parallel partial perp phantom phase "
    "phi pi pink pitchfork plim plusmn pm pmb pmod pod pounds prec precapprox preccurlyeq preceq "
    "precnapprox precneqq precnsim precsim prime prod projlim propto providecommand psi purple "
    "purpleA purpleB purpleC purpleD purpleE qquad quad r rArr rBrace rVert raisebox rang rangle rarr "
    "ratio rbrace rbrack rceil real reals red redA redB redC redD redE relax renewcommand restriction "
    "rfloor rgroup rhd rho right rightarrow rightarrowtail rightharpoondown rightharpoonup "
    "rightleftarrows rightleftharpoons rightrightarrows rightsquigarrow rightthreetimes risingdotseq "
    "rlap rm rmoustache rparen rq rrbracket rtimes rule rvert scriptscriptstyle scriptsize "
    "scriptstyle sdot searrow sec sect set setminus sf sh sharp shortmid shortparallel show sigma sim "
    "simcolon simcoloncolon simeq sin sinh sixptsize small smallfrown smallint smallsetminus "
    "smallsmile smash smile sout space spades spadesuit sphericalangle sqcap sqcup sqrt sqsubset "
    "sqsubseteq sqsupset sqsupseteq square stackrel star sub sube subset subseteq subseteqq subsetneq "
    "subsetneqq substack succ succapprox succcurlyeq succeq succnapprox succneqq succnsim succsim sum "
    "sup supe supset supseteq supseteqq supsetneq supsetneqq surd swarrow tag tan tanh tau tbinom "
    "tealA tealB tealC tealD tealE text textbf textcircled textcolor textcopyright textit textmd "
    "textnormal textregistered textrm textsf textstyle texttt textup tfrac tg th therefore theta "
    "thetasym thickapprox thicksim thickspace thinspace tilde times tiny tmspace to top triangle "
    "triangledown triangleleft trianglelefteq triangleq triangleright trianglerighteq tt "
    "twoheadleftarrow twoheadrightarrow u uArr uarr ulcorner underbar underbrace underbracket "
    "undergroup underleftarrow underleftrightarrow underline underlinesegment underrightarrow "
    "underset unlhd unrhd uparrow updownarrow upharpoonleft upharpoonright uplus upsilon upuparrows "
    "urcorner url utilde v vDash varDelta varGamma varLambda varOmega varPhi varPi varPsi varSigma "
    "varTheta varUpsilon varXi varepsilon varinjlim varkappa varliminf varlimsup varnothing varphi "
    "varpi varprojlim varpropto varrho varsigma varsubsetneq varsubsetneqq varsupsetneq varsupsetneqq "
    "vartheta vartriangle vartriangleleft vartriangleright varvdots vcentcolon vcenter vdash vdots "
    "vec vee veebar verb vert vphantom wedge weierp widecheck widehat widetilde wp wr xLeftarrow "
    "xLeftrightarrow xRightarrow xcancel xdef xhookleftarrow xhookrightarrow xi xleftarrow "
    "xleftequilibrium xleftharpoondown xleftharpoonup xleftrightarrow xleftrightharpoons xlongequal "
    "xmapsto xrightarrow xrightequilibrium xrightharpoondown xrightharpoonup xrightleftarrows "
    "xrightleftharpoons xtofrom xtwoheadleftarrow xtwoheadrightarrow yen zeta "
).split())
_ENTORNOS_KATEX = {"matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "smallmatrix", "array",
                   "cases", "rcases", "aligned", "alignedat", "gathered", "subarray"}


def latex_seguro(expr: str) -> str | None:
    """Corrige los fallos típicos de LaTeX generado por IA. None si no se puede garantizar que Notion lo acepte."""
    e = re.sub(r"\\label\{[^}]*\}", "", expr)
    e = re.sub(r"\\begin\{(?:align|eqnarray|split|gather|multline)\*?\}", lambda _: r"\begin{aligned}", e)
    e = re.sub(r"\\end\{(?:align|eqnarray|split|gather|multline)\*?\}", lambda _: r"\end{aligned}", e)
    e = re.sub(r"\\(?:begin|end)\{equation\*?\}", "", e)
    e = e.replace("\\bm{", "\\boldsymbol{").replace("\\degree", "^\\circ").strip()
    e = re.sub(r"\\(sen|senh|arcsen|arccosec|tgh|arccotg)(?![A-Za-z])", r"\\operatorname{\1}", e)  # operadores en español
    if not e or len(e) > EQ_MAX or "$" in e or _LATEX_PROHIBIDO.search(e):
        return None
    llaves = 0
    for c in e.replace("\\{", "").replace("\\}", ""):
        llaves += (c == "{") - (c == "}")
        if llaves < 0:
            return None
    izquierdos, derechos = (len(re.findall(rf"\\{c}(?![A-Za-z])", e)) for c in ("left", "right"))  # sin confundir \rightarrow con \right
    if llaves != 0 or izquierdos != derechos or e.count("\\begin{") != e.count("\\end{"):
        return None
    if set(re.findall(r"\\([A-Za-z]+)", e)) - _COMANDOS_KATEX or set(re.findall(r"\\begin\{([^}]*)\}", e)) - _ENTORNOS_KATEX:
        return None  # un comando o entorno que KaTeX no conoce haría que Notion rechazase la fórmula
    return e


# --- Diagramas: Mermaid (Notion los dibuja solo) -----------------------------------------
_TIPOS_MERMAID = {"flowchart", "graph", "sequencediagram", "statediagram", "statediagram-v2",
                  "classdiagram", "erdiagram", "pie", "gantt", "journey"}


_CIERRE_FORMA = {"[": "]", "(": ")", "{": "}"}


def _citar_etiquetas_flowchart(texto: str) -> str:
    """Pone entre comillas las etiquetas de los nodos, que es lo que más rompe los diagramas (paréntesis,
    comas, corchetes anidados...). Respeta lo ya entrecomillado y cuenta los anidamientos."""
    salida, i, n = [], 0, len(texto)
    while i < n:
        c = texto[i]
        if c == '"':  # texto ya entrecomillado: se deja tal cual
            j = texto.find('"', i + 1)
            fin = n if j == -1 or "\n" in texto[i:j] else j + 1
            salida.append(texto[i:fin])
            i = fin
            continue
        if (c in _CIERRE_FORMA and i > 0 and (texto[i - 1].isalnum() or texto[i - 1] == "_")
                and i + 1 < n and texto[i + 1] not in '[({/\\"'):  # formas especiales ([[ ([ (( {{ [/ ...) se respetan
            cierre, hondo, j = _CIERRE_FORMA[c], 1, i + 1
            while j < n and texto[j] != "\n" and hondo:
                hondo += (texto[j] == c) - (texto[j] == cierre)
                j += 1
            etiqueta = texto[i + 1 : j - 1].strip()
            if hondo == 0 and etiqueta:
                salida.append(f'{c}"{etiqueta.replace(chr(34), chr(39))}"{cierre}')
                i = j
                continue
        salida.append(c)
        i += 1
    # etiquetas de las flechas: -->|texto|  (solo fuera de comillas)
    trozos = re.split(r'("[^"\n]*")', "".join(salida))
    for k in range(0, len(trozos), 2):
        trozos[k] = re.sub(r"((?:-->|---|-\.->|==>|--)\s*)\|([^|\n\"]+)\|",
                           lambda m: f'{m.group(1)}|"{m.group(2).strip()}"|', trozos[k])
    return "".join(trozos)


def _proteger_end(texto: str) -> str:
    """'end' en minúscula como nombre de nodo rompe los flowchart (cierra un subgraph): se escribe 'End'."""
    lineas = []
    for linea in texto.split("\n"):
        if linea.strip() != "end":  # la línea 'end' sola sí cierra un subgraph
            partes = re.split(r'("[^"\n]*")', linea)
            for k in range(0, len(partes), 2):
                partes[k] = re.sub(r"(?<![\w.-])end(?![\w-])", "End", partes[k])
            linea = "".join(partes)
        lineas.append(linea)
    return "\n".join(lineas)


def _sin_punto_y_coma(texto: str) -> str:
    """En secuencias y estados el ';' del texto de un mensaje se interpreta como fin de instrucción."""
    lineas = []
    for linea in texto.split("\n"):
        antes, dos_puntos, despues = linea.partition(":")
        if dos_puntos and not linea.strip().startswith("%%"):
            linea = antes + ":" + despues.replace(";", ",")
        lineas.append(linea)
    return "\n".join(lineas)


def mermaid_seguro(codigo: str) -> str | None:
    """Diagrama saneado, o None si no es de un tipo conocido o es sospechoso (se guardará como texto)."""
    lineas = [l.rstrip() for l in codigo.strip().split("\n") if not l.strip().startswith("%%{")]
    utiles = [l for l in lineas if l.strip() and not l.strip().startswith("%%")]
    if not utiles or len(utiles) > 80:
        return None
    tipo = utiles[0].split()[0].lower()
    texto = "\n".join(lineas).strip()
    if tipo not in _TIPOS_MERMAID or re.search(r"(?im)^\s*click\s|<script|javascript:|\bhref\b|onerror|onclick", texto):
        return None
    if tipo in {"flowchart", "graph"}:
        texto = _proteger_end(_citar_etiquetas_flowchart(texto))
    elif tipo in {"sequencediagram", "statediagram", "statediagram-v2"}:
        texto = _sin_punto_y_coma(texto)
    return texto if len(texto) <= RICH_MAX else None


# --- Gráficas: expresiones seguras + dibujo SVG propio ------------------------------------
_FUNCIONES = {"sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
              "atan": math.atan, "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh, "exp": math.exp,
              "ln": math.log, "log": math.log10, "log10": math.log10, "log2": math.log2, "sqrt": math.sqrt,
              "abs": abs}
_CONSTANTES = {"pi": math.pi, "e": math.e}
_SUSTITUCIONES = (("²", "**2"), ("³", "**3"), ("^", "**"), ("√", "sqrt"), ("π", "pi"), ("×", "*"),
                  ("·", "*"), ("÷", "/"), ("−", "-"), ("–", "-"))
_NODOS_OK = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Constant, ast.Load,
             ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.USub, ast.UAdd)
COLORES_GRAFICA = ("#1f77b4", "#d62728", "#2ca02c", "#ff7f0e")


def _normalizar_expresion(texto: str) -> str:
    t = re.sub(r"^\s*(?:y|f\s*\(\s*x\s*\))\s*=\s*", "", texto.strip(), flags=re.IGNORECASE)
    for viejo, nuevo in _SUSTITUCIONES:
        t = t.replace(viejo, nuevo)
    t = re.sub(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?=[a-zA-Z(])", r"\1*", t)  # 2x -> 2*x ; 3(x+1) -> 3*(x+1)
    t = re.sub(r"\)\s*(?=[\w(])", ")*", t)                              # (x+1)(x-1) -> (x+1)*(x-1)
    return re.sub(r"\bx\s*(?=\()", "x*", t)                             # x(x+1) -> x*(x+1)


def compilar_expresion(texto: str) -> ast.AST:
    """Valida una expresión matemática con lista blanca de nodos: no se ejecuta código del usuario ni de la IA."""
    if len(texto) > 160:
        raise ValueError("expresión demasiado larga")
    try:
        arbol = ast.parse(_normalizar_expresion(texto), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise ValueError(f"no entiendo la expresión «{texto[:40]}»") from None
    nodos = list(ast.walk(arbol))
    if len(nodos) > 90:
        raise ValueError("expresión demasiado compleja")
    nombres_de_funcion = {id(n.func) for n in nodos if isinstance(n, ast.Call)}
    for n in nodos:
        if not isinstance(n, _NODOS_OK):
            raise ValueError(f"operación no permitida en «{texto[:40]}»")
        if isinstance(n, ast.Constant) and type(n.value) not in (int, float):
            raise ValueError("solo se admiten números")
        if isinstance(n, ast.Call) and not (isinstance(n.func, ast.Name) and n.func.id in _FUNCIONES
                                            and len(n.args) == 1 and not n.keywords):
            raise ValueError(f"función no permitida en «{texto[:40]}»")
        if isinstance(n, ast.Name) and id(n) not in nombres_de_funcion and n.id != "x" and n.id not in _CONSTANTES:
            raise ValueError(f"no conozco «{n.id}»")
    return arbol.body


def _potencia(a: float, b: float) -> float:
    if abs(b) > 100 or abs(a) > 1e9:  # evita cálculos gigantes (p. ej. 9**9**9)
        raise OverflowError
    return math.pow(a, b)


def _ejecutar(n: ast.AST, x: float | None) -> float:
    if isinstance(n, ast.Constant):
        return float(n.value)
    if isinstance(n, ast.Name):
        if n.id == "x":
            if x is None:
                raise ValueError("aquí no puede aparecer x")
            return x
        return _CONSTANTES[n.id]
    if isinstance(n, ast.UnaryOp):
        v = _ejecutar(n.operand, x)
        return -v if isinstance(n.op, ast.USub) else v
    if isinstance(n, ast.BinOp):
        a, b = _ejecutar(n.left, x), _ejecutar(n.right, x)
        if isinstance(n.op, ast.Pow):
            return _potencia(a, b)
        if isinstance(n.op, ast.Add):
            return a + b
        if isinstance(n.op, ast.Sub):
            return a - b
        if isinstance(n.op, ast.Mult):
            return a * b
        if isinstance(n.op, ast.Div):
            return a / b
        return a % b
    if isinstance(n, ast.Call):
        return _FUNCIONES[n.func.id](_ejecutar(n.args[0], x))
    raise ValueError("expresión no válida")


def evaluar(expr: ast.AST, x: float | None = None) -> float | None:
    """Valor de la expresión en x, o None si no está definida ahí (división por cero, raíz de negativo...)."""
    try:
        v = _ejecutar(expr, x)
    except (ZeroDivisionError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _numero(texto: str) -> float:
    v = evaluar(compilar_expresion(texto))
    if v is None:
        raise ValueError(f"«{texto[:30]}» no es un número válido")
    return v


def _dividir_nivel0(texto: str, sep: str = ",") -> list[str]:
    partes, nivel, actual = [], 0, ""
    for c in texto:
        nivel += (c == "(") - (c == ")")
        if c == sep and nivel == 0:
            partes.append(actual)
            actual = ""
        else:
            actual += c
    return partes + [actual]


def _punto(texto: str) -> tuple[float, float]:
    partes = _dividir_nivel0(texto.strip().strip("()"))
    if len(partes) != 2:
        raise ValueError(f"punto no válido «{texto[:30]}»")
    return _numero(partes[0]), _numero(partes[1])


def parsear_grafica(texto: str) -> dict:
    """Bloque ```grafica -> especificación validada. Lanza ValueError con un motivo legible si algo falla."""
    spec: dict = {"titulo": "", "ejex": "x", "ejey": "y", "x": None, "y": None,
                  "funciones": [], "series": [], "marcas": []}
    for linea in texto.splitlines():
        linea = linea.strip()
        clave, dos_puntos, valor = linea.partition(":")
        if not linea or linea.startswith("#") or not dos_puntos:
            continue
        clave, valor = _norm(clave).replace(" ", ""), valor.strip()
        if clave == "titulo":
            spec["titulo"] = valor[:80]
        elif clave in {"ejex", "ejey"}:
            spec[clave] = valor[:30]
        elif clave in {"x", "y"}:
            ext = _dividir_nivel0(valor)
            if len(ext) != 2:
                raise ValueError(f"el rango de {clave} debe ser «mínimo, máximo»")
            lo, hi = _numero(ext[0]), _numero(ext[1])
            if not lo < hi or hi - lo > 1e6:
                raise ValueError(f"rango de {clave} no válido")
            spec[clave] = (lo, hi)
        elif clave in {"funcion", "funciones"}:
            cuerpo, _, etiqueta = valor.partition("|")
            if len(spec["funciones"]) < 3:
                spec["funciones"].append((compilar_expresion(cuerpo), (etiqueta.strip() or cuerpo.strip())[:40]))
        elif clave == "puntos":
            cuerpo, _, etiqueta = valor.partition("|")
            pts = [_punto(p) for p in re.findall(r"\(([^()]*)\)", cuerpo)][:200]
            if pts and len(spec["series"]) < 3:
                spec["series"].append((pts, etiqueta.strip()[:40] or "Datos"))
        elif clave == "marcar":
            m = re.match(r"\(([^()]*)\)\s*(.*)", valor)
            if m and len(spec["marcas"]) < 8:
                spec["marcas"].append((*_punto(m.group(1)), m.group(2).strip()[:30]))
    if not spec["funciones"] and not spec["series"]:
        raise ValueError("la gráfica no tiene ninguna función ni puntos")
    if spec["funciones"] and spec["x"] is None:
        raise ValueError("falta el rango de x (por ejemplo «x: -4, 4»)")
    return spec


def _marcas_ejes(lo: float, hi: float) -> list[float]:
    bruto = (hi - lo) / 7
    magnitud = 10 ** math.floor(math.log10(bruto))
    paso = next((k * magnitud for k in (1, 2, 2.5, 5, 10) if bruto <= k * magnitud), 10 * magnitud)
    marcas, v = [], math.ceil(lo / paso - 1e-9) * paso
    while v <= hi + paso * 1e-9 and len(marcas) < 40:
        marcas.append(round(v, 12))
        v += paso
    return marcas


def _fmt_num(v: float) -> str:
    if abs(v) < 1e-9:
        return "0"
    return f"{v:.3g}" if abs(v) >= 1e4 or abs(v) < 1e-3 else f"{v:.4g}"


def _percentil_lista(valores: list[float], p: float) -> float:
    v = sorted(valores)
    k = (len(v) - 1) * p / 100
    i = int(k)
    return v[i] + (v[min(i + 1, len(v) - 1)] - v[i]) * (k - i)


def svg_grafica(spec: dict) -> str:
    """Dibuja la gráfica como SVG (fondo blanco, legible en modo claro y oscuro). Solo texto escapado y números."""
    W, H, ML, MR, MT, MB = 720, 450, 64, 24, 56, 56
    pw, ph = W - ML - MR, H - MT - MB
    esc = lambda s: escape(str(s), {'"': "&quot;"})

    series = [(pts, et) for pts, et in spec["series"]]
    if spec["x"] is not None:
        x0, x1 = spec["x"]
    else:
        xs = [p[0] for pts, _ in series for p in pts] + [m[0] for m in spec["marcas"]]
        x0, x1 = min(xs), max(xs)
        x0, x1 = (x0 - 1, x1 + 1) if x0 == x1 else (x0 - (x1 - x0) * 0.05, x1 + (x1 - x0) * 0.05)

    curvas: list[tuple[str, list[tuple[float, float] | None]]] = []
    for expr, etiqueta in spec["funciones"]:
        puntos = []
        for i in range(301):
            x = x0 + (x1 - x0) * i / 300
            y = evaluar(expr, x)
            puntos.append(None if y is None else (x, y))
        curvas.append((etiqueta, puntos))

    if spec["y"] is not None:
        y0, y1 = spec["y"]
    else:
        ys = [p[1] for _, pts in curvas for p in pts if p] + [p[1] for pts, _ in series for p in pts] \
             + [m[1] for m in spec["marcas"]]
        if not ys:
            raise ValueError("la función no está definida en ese rango")
        lo, hi = min(ys), max(ys)
        if len(ys) > 20 and hi - lo > 8 * max(_percentil_lista(ys, 95) - _percentil_lista(ys, 5), 1e-9):
            lo, hi = _percentil_lista(ys, 2), _percentil_lista(ys, 98)  # hay asíntotas: ignora los extremos
        if lo == hi:
            lo, hi = lo - 1, hi + 1
        margen = (hi - lo) * 0.08
        y0, y1 = lo - margen, hi + margen

    def px(x: float) -> float:
        return ML + (x - x0) / (x1 - x0) * pw

    def py(y: float) -> float:
        return max(-1e4, min(1e4, MT + (1 - (y - y0) / (y1 - y0)) * ph))

    fuente = 'font-family="Arial, Helvetica, sans-serif"'
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
         f'aria-label="{esc(spec["titulo"] or "Gráfica")}">',
         f'<title>{esc(spec["titulo"] or "Gráfica")}</title>',
         f'<defs><clipPath id="area"><rect x="{ML}" y="{MT}" width="{pw}" height="{ph}"/></clipPath></defs>',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>']
    if spec["titulo"]:
        o.append(f'<text x="{W / 2}" y="30" text-anchor="middle" {fuente} font-size="18" font-weight="bold" '
                 f'fill="#222222">{esc(spec["titulo"])}</text>')
    for v in _marcas_ejes(x0, x1):
        o.append(f'<line x1="{px(v):.1f}" y1="{MT}" x2="{px(v):.1f}" y2="{MT + ph}" stroke="#e3e3e3"/>')
        o.append(f'<text x="{px(v):.1f}" y="{MT + ph + 18}" text-anchor="middle" {fuente} font-size="12" '
                 f'fill="#444444">{esc(_fmt_num(v))}</text>')
    for v in _marcas_ejes(y0, y1):
        o.append(f'<line x1="{ML}" y1="{py(v):.1f}" x2="{ML + pw}" y2="{py(v):.1f}" stroke="#e3e3e3"/>')
        o.append(f'<text x="{ML - 8}" y="{py(v) + 4:.1f}" text-anchor="end" {fuente} font-size="12" '
                 f'fill="#444444">{esc(_fmt_num(v))}</text>')
    o.append(f'<rect x="{ML}" y="{MT}" width="{pw}" height="{ph}" fill="none" stroke="#999999"/>')
    if x0 < 0 < x1:
        o.append(f'<line x1="{px(0):.1f}" y1="{MT}" x2="{px(0):.1f}" y2="{MT + ph}" stroke="#333333" stroke-width="1.6"/>')
    if y0 < 0 < y1:
        o.append(f'<line x1="{ML}" y1="{py(0):.1f}" x2="{ML + pw}" y2="{py(0):.1f}" stroke="#333333" stroke-width="1.6"/>')
    o.append(f'<text x="{ML + pw / 2}" y="{H - 12}" text-anchor="middle" {fuente} font-size="14" '
             f'fill="#222222">{esc(spec["ejex"])}</text>')
    o.append(f'<text x="18" y="{MT + ph / 2}" text-anchor="middle" {fuente} font-size="14" fill="#222222" '
             f'transform="rotate(-90 18 {MT + ph / 2})">{esc(spec["ejey"])}</text>')

    leyenda, o_clip, o_puntos = [], [], []
    for i, (etiqueta, puntos) in enumerate(curvas):
        color, trazo, abierto, previo = COLORES_GRAFICA[i % 4], [], False, None
        for p in puntos:
            if p is None:
                abierto, previo = False, None
                continue
            alto = y1 - y0
            # Asíntota: salto enorme, o cambio de signo con los dos lados lejos del eje (un cruce normal
            # por el cero, por empinado que sea, tiene valores pequeños a ambos lados)
            if previo is not None and (abs(p[1] - previo) > 2.5 * alto
                                       or (p[1] * previo < 0 and min(abs(p[1]), abs(previo)) > 0.2 * alto)):
                abierto = False
            trazo.append(("L" if abierto else "M") + f"{px(p[0]):.1f},{py(p[1]):.1f}")
            abierto, previo = True, p[1]
        o_clip.append(f'<path d="{" ".join(trazo)}" fill="none" stroke="{color}" stroke-width="2.4" '
                      'stroke-linejoin="round"/>')
        leyenda.append((color, etiqueta))
    for j, (pts, etiqueta) in enumerate(series):
        color = COLORES_GRAFICA[(len(curvas) + j) % 4]
        ordenados = sorted(pts)
        o_clip.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>' % (
            color, " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in ordenados)))
        o_puntos.extend(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="3.5" fill="{color}"/>'
                        for x, y in ordenados if x0 <= x <= x1 and y0 <= y <= y1)  # fuera del recorte: no se cortan a la mitad
        leyenda.append((color, etiqueta))
    o.append('<g clip-path="url(#area)">' + "".join(o_clip) + "</g>")
    o.extend(o_puntos)
    for x, y, etiqueta in spec["marcas"]:
        if x0 <= x <= x1 and y0 <= y <= y1:
            o.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="5" fill="#d62728" stroke="#ffffff" stroke-width="1.5"/>')
            if etiqueta:
                o.append(f'<text x="{px(x) + 8:.1f}" y="{py(y) - 8:.1f}" {fuente} font-size="12" fill="#222222" '
                         f'stroke="#ffffff" stroke-width="3" paint-order="stroke">{esc(etiqueta)}</text>')
    for i, (color, etiqueta) in enumerate(leyenda):
        yy = MT + 18 + i * 20
        o.append(f'<line x1="{ML + 12}" y1="{yy - 4}" x2="{ML + 36}" y2="{yy - 4}" stroke="{color}" stroke-width="3"/>')
        o.append(f'<text x="{ML + 42}" y="{yy}" {fuente} font-size="12" fill="#222222" stroke="#ffffff" '
                 f'stroke-width="3" paint-order="stroke">{esc(etiqueta)}</text>')
    o.append("</svg>")
    return "\n".join(o)


# --- Texto con formato -----------------------------------------------------------------------
def _trozos_texto(texto: str, negrita: bool = False, codigo: bool = False, cursiva: bool = False) -> list[dict]:
    anot = {k: True for k, activo in (("bold", negrita), ("code", codigo), ("italic", cursiva)) if activo}
    extra = {"annotations": anot} if anot else {}
    return [
        {"type": "text", "text": {"content": texto[i : i + RICH_MAX]}, **extra}
        for i in range(0, len(texto), RICH_MAX)
    ]


def _rich_text(texto: str) -> list[dict]:
    """Convierte **negrita**, *cursiva*, `código` y fórmulas $...$ en línea a rich_text de Notion."""
    rt: list[dict] = []
    for parte in _RE_INLINE.split(texto):
        if not parte:
            continue
        if parte.startswith("$") and parte.endswith("$") and len(parte) > 2:
            doble = parte.startswith("$$") and len(parte) > 4
            fuente = parte[2:-2].strip() if doble else parte[1:-1].strip()
            expr = latex_seguro(fuente)
            if expr:
                rt.append({"type": "equation", "equation": {"expression": expr}})
            else:  # LaTeX dudoso: se enseña tal cual, como código, en vez de arriesgar toda la página
                rt.extend(_trozos_texto(fuente, codigo=True))
        elif parte.startswith("**") and parte.endswith("**") and len(parte) > 4:
            rt.extend(_trozos_texto(parte[2:-2], negrita=True))
        elif parte.startswith("`") and parte.endswith("`") and len(parte) > 2:
            rt.extend(_trozos_texto(parte[1:-1], codigo=True))
        elif parte.startswith("*") and parte.endswith("*") and len(parte) > 2:
            rt.extend(_trozos_texto(parte[1:-1], cursiva=True))
        else:
            rt.extend(_trozos_texto(parte))
    return rt[:100]  # máximo de Notion por bloque


def _bloque(tipo: str, texto: str) -> dict | None:
    rt = _rich_text(texto)
    return {"object": "block", "type": tipo, tipo: {"rich_text": rt}} if rt else None


def _bloque_codigo(codigo: str, lenguaje: str) -> dict | None:
    return ({"object": "block", "type": "code",
             "code": {"rich_text": _trozos_texto(codigo), "language": lenguaje}} if codigo.strip() else None)


def _dividir_celdas(linea: str) -> list[str]:
    """Separa una fila de tabla por '|' sin romper las fórmulas con $|x|$."""
    celdas, actual, en_formula = [], "", False
    for c in linea.strip().strip("|"):
        if c == "$":
            en_formula = not en_formula
        if c == "|" and not en_formula:
            celdas.append(actual.strip())
            actual = ""
        else:
            actual += c
    return celdas + [actual.strip()]


_RE_SEPARADOR_TABLA = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _normalizar_delimitadores_latex(lineas: list[str]) -> list[str]:
    """Convierte los delimitadores LaTeX alternativos (corchetes y paréntesis con barra) a $$ y $, fuera del código."""
    salida, en_codigo = [], False
    for linea in lineas:
        if linea.strip().startswith("```"):
            en_codigo = not en_codigo
        elif not en_codigo:
            linea = linea.replace("\\[", "$$").replace("\\]", "$$").replace("\\(", "$").replace("\\)", "$")
        salida.append(linea)
    return salida


def _parsear_bloques(lineas: list[str], nivel: int = 0) -> list[dict | None]:
    """Líneas de Markdown -> bloques. `nivel` = contenedores (aviso, desplegable) que ya envuelven estas
    líneas: Notion admite como mucho dos niveles de anidación en una petición."""
    bloques: list[dict | None] = []
    pila: list[tuple[int, dict]] = []  # elementos de lista abiertos (sangría, bloque)
    max_hijos = max(0, 2 - nivel)
    i, n = 0, len(lineas)
    while i < n:
        bruta = lineas[i]
        linea = bruta.strip()
        i += 1
        if not linea:
            continue

        # Elementos de lista (con sublistas por sangría)
        m_vineta = re.match(r"^[-*]\s+(.*)", linea)
        m_numero = re.match(r"^\d+[.)]\s+(.*)", linea)
        if m_vineta or m_numero:
            tipo = "bulleted_list_item" if m_vineta else "numbered_list_item"
            item = _bloque(tipo, (m_vineta or m_numero).group(1))
            if item is None:
                continue
            sangria = len(bruta.replace("\t", "    ")) - len(bruta.replace("\t", "    ").lstrip())
            while pila and pila[-1][0] >= sangria:
                pila.pop()
            del pila[max_hijos:]
            if pila:
                pila[-1][1][pila[-1][1]["type"]].setdefault("children", []).append(item)
            else:
                bloques.append(item)
            pila.append((sangria, item))
            continue
        pila.clear()

        # Código ```lenguaje ... ``` (mermaid y grafica reciben tratamiento especial)
        if linea.startswith("```"):
            lenguaje = linea[3:].strip().lower().split(" ")[0]
            lenguaje = _ALIAS_LENGUAJE.get(lenguaje, lenguaje)
            codigo = []
            while i < n and not lineas[i].strip().startswith("```"):
                codigo.append(lineas[i])
                i += 1
            i += 1
            texto = "\n".join(codigo)
            if lenguaje == "grafica":
                bloques.append({"_grafica": texto})
            elif lenguaje == "mermaid":
                limpio = mermaid_seguro(texto)
                bloques.append(_bloque_codigo(limpio or texto, "mermaid" if limpio else "plain text"))
            else:
                bloques.append(_bloque_codigo(texto, lenguaje if lenguaje in _LENGUAJES_CODIGO else "plain text"))
            continue

        # Ecuación en bloque: $$x$$ en una línea, o $$ / fórmula / $$ en varias
        if linea.startswith("$$"):
            expr = None
            if linea.endswith("$$") and len(linea) > 4:
                expr = linea[2:-2].strip()
            elif "$$" not in linea[2:]:
                partes = [linea[2:]]
                while i < n:
                    siguiente = lineas[i].strip()
                    i += 1
                    if siguiente.endswith("$$"):
                        partes.append(siguiente[:-2])
                        break
                    partes.append(siguiente)
                expr = " ".join(p for p in partes if p).strip()
            if expr is not None:
                limpia = latex_seguro(expr) if expr else None
                if limpia:
                    bloques.append({"object": "block", "type": "equation", "equation": {"expression": limpia}})
                elif expr:
                    bloques.append(_bloque_codigo(expr, "latex"))
                continue

        # Desplegable <details><summary>…</summary> … </details>
        if linea.lower().startswith("<details"):
            cuerpo, abiertas = [], 1
            resto = re.sub(r"^<details[^>]*>", "", linea, flags=re.IGNORECASE).strip()
            if resto:
                cuerpo.append(resto)
            while i < n and abiertas:
                siguiente = lineas[i]
                i += 1
                bajo = siguiente.strip().lower()
                abiertas += bajo.startswith("<details")
                if "</details>" in bajo:
                    abiertas -= 1
                    if not abiertas:
                        antes = re.split(r"</details>", siguiente, flags=re.IGNORECASE)[0]
                        if antes.strip():
                            cuerpo.append(antes)
                        break
                cuerpo.append(siguiente)
            texto = "\n".join(cuerpo)
            resumen = re.search(r"<summary>(.*?)</summary>", texto, flags=re.IGNORECASE | re.DOTALL)
            titulo = resumen.group(1).strip() if resumen else "Ver más"
            if resumen:
                texto = texto[: resumen.start()] + texto[resumen.end():]
            hijos = [b for b in _parsear_bloques(texto.split("\n"), nivel + 1) if b][:100]
            if nivel >= 2:  # sin sitio para más anidación: se aplana
                bloques.append(_bloque("paragraph", f"**{titulo}**"))
                bloques.extend(hijos)
            else:
                rt = _rich_text(titulo) or _trozos_texto("Ver más")
                bloques.append({"object": "block", "type": "toggle", "toggle": {"rich_text": rt, "children": hijos}})
            continue

        # Citas y avisos  > [!IDEA] Título
        if linea.startswith(">"):
            cita = [linea.lstrip(">").strip()]
            while i < n and lineas[i].strip().startswith(">"):
                cita.append(lineas[i].strip().lstrip(">").strip())
                i += 1
            etiqueta = _RE_CALLOUT.match(cita[0])
            clave = _norm(etiqueta.group(1)).upper() if etiqueta else ""
            if clave in _CALLOUTS:
                emoji, color, titulo_defecto = _CALLOUTS[clave]
                titulo = etiqueta.group(2).strip() or titulo_defecto
                hijos = [b for b in _parsear_bloques(cita[1:], nivel + 1) if b][:100]
                bloques.append({"object": "block", "type": "callout", "callout": {
                    "rich_text": _trozos_texto(titulo[:200], negrita=True),
                    "icon": {"type": "emoji", "emoji": emoji}, "color": color,
                    **({"children": hijos} if hijos else {})}})
            else:
                bloques.append(_bloque("quote", "\n".join(c for c in cita if c)))
            continue

        # Tablas Markdown
        if linea.startswith("|") and i < n and _RE_SEPARADOR_TABLA.match(lineas[i].strip()):
            filas = [_dividir_celdas(linea)]
            i += 1
            while i < n and lineas[i].strip().startswith("|") and len(filas) < 40:
                filas.append(_dividir_celdas(lineas[i]))
                i += 1
            ancho = min(8, max(len(f) for f in filas))
            bloques.append({"object": "block", "type": "table", "table": {
                "table_width": ancho, "has_column_header": True, "has_row_header": False,
                "children": [{"object": "block", "type": "table_row", "table_row": {
                    "cells": [_rich_text(c) for c in (f + [""] * ancho)[:ancho]]}} for f in filas]}})
            continue

        if re.fullmatch(r"[-*_]{3,}", linea):
            bloques.append({"object": "block", "type": "divider", "divider": {}})
        elif m := re.match(r"^(#{1,6})\s+(.*)", linea):
            bloques.append(_bloque(f"heading_{min(len(m.group(1)), 3)}", m.group(2)))
        elif re.match(r"^!\[[^\]]*\]\([^)]*\)$", linea):
            continue  # no se enlazan imágenes externas
        else:
            bloques.append(_bloque("paragraph", linea))
    return bloques


def markdown_a_bloques(markdown: str) -> list[dict]:
    texto = _limpiar_texto(markdown)
    cerco = re.match(r"^```(?:markdown|md)\s*\n(.*)\n```$", texto, flags=re.DOTALL)  # la IA a veces lo envuelve todo
    if cerco:
        texto = cerco.group(1)
    return [b for b in _parsear_bloques(_normalizar_delimitadores_latex(texto.split("\n"))) if b]


# --- Degradación: si Notion rechaza algo, la masterclass se publica igualmente ----------------------
def degradar_bloques(bloques):
    """Copia sin los elementos delicados: fórmulas -> código, diagramas -> texto, imágenes -> aviso."""
    if isinstance(bloques, list):
        return [degradar_bloques(b) for b in bloques]
    if not isinstance(bloques, dict):
        return bloques
    if bloques.get("type") == "equation" and "object" not in bloques:  # fórmula en línea
        return {"type": "text", "text": {"content": bloques["equation"]["expression"]}, "annotations": {"code": True}}
    if bloques.get("object") == "block":
        tipo = bloques.get("type")
        if tipo == "equation":
            return _bloque_codigo(bloques["equation"]["expression"], "latex")
        if tipo == "image":
            return _bloque("paragraph", "(figura no disponible)")
        if tipo == "code" and bloques["code"].get("language") == "mermaid":
            return {**bloques, "code": {**bloques["code"], "language": "plain text"}}
    return {k: degradar_bloques(v) for k, v in bloques.items()}


def _recorrer(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _recorrer(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _recorrer(v)


def contar_visuales(bloques: list[dict]) -> dict[str, int]:
    c = {"fórmulas": 0, "diagramas": 0, "gráficas": 0, "tablas": 0, "avisos": 0, "desplegables": 0}
    for d in _recorrer(bloques):
        tipo = d.get("type")
        if tipo == "equation":
            c["fórmulas"] += 1
        elif d.get("object") == "block":
            if tipo == "code" and d["code"].get("language") == "mermaid":
                c["diagramas"] += 1
            elif tipo == "image":
                c["gráficas"] += 1
            elif tipo in {"table", "callout", "toggle"}:
                c[{"table": "tablas", "callout": "avisos", "toggle": "desplegables"}[tipo]] += 1
    return {k: v for k, v in c.items() if v}


def informe_visual(bloques: list[dict], fallidas: int = 0, degradada: bool = False) -> str:
    c = contar_visuales(bloques)
    lineas = []
    if c:
        lineas.append("📐 Incluye: " + " · ".join(f"{v} {k}" for k, v in c.items()) + ".")
    if fallidas:
        lineas.append(f"⚠️ {fallidas} gráfica{'s' if fallidas != 1 else ''} no se pudo dibujar y quedó como texto.")
    if degradada:
        lineas.append("⚠️ Notion rechazó algún elemento visual, así que lo he guardado como texto simple.")
    return "\n".join(lineas)


async def resolver_graficas(notion: "Notion", bloques: list[dict]) -> tuple[list[dict], int]:
    """Cambia cada marcador de gráfica por una imagen SVG dibujada aquí y subida a Notion.
    Devuelve (bloques, nº de gráficas que han fallado); las fallidas quedan como una línea de texto."""
    estado = {"hechas": 0, "fallidas": 0}

    async def una(texto: str) -> dict | None:
        titulo = ""
        try:
            if estado["hechas"] + estado["fallidas"] >= MAX_GRAFICAS:
                raise ValueError("demasiadas gráficas en una masterclass")
            spec = parsear_grafica(texto)
            titulo = spec["titulo"]
            svg = svg_grafica(spec).encode("utf-8")
            archivo = await notion.subir_archivo(svg, "grafica.svg", "image/svg+xml")
        except (ValueError, NotionError) as e:
            estado["fallidas"] += 1
            log.warning("Gráfica no dibujada: %s", e)
            return _bloque("paragraph", f"📈 {titulo or 'Gráfica'}: no he podido dibujarla.")
        estado["hechas"] += 1
        imagen = {"type": "file_upload", "file_upload": {"id": archivo}}
        if titulo:
            imagen["caption"] = _trozos_texto(titulo)
        return {"object": "block", "type": "image", "image": imagen}

    async def recorrer(lista: list[dict]) -> list[dict]:
        salida = []
        for b in lista:
            if "_grafica" in b:
                nuevo = await una(b["_grafica"])
                if nuevo:
                    salida.append(nuevo)
                continue
            cuerpo = b.get(b.get("type", ""))
            if isinstance(cuerpo, dict) and cuerpo.get("children"):
                cuerpo["children"] = await recorrer(cuerpo["children"])
            salida.append(b)
        return salida

    return await recorrer(bloques), estado["fallidas"]


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
    """Crea una página; los primeros 100 bloques van en la misma petición. Si Notion rechaza algún elemento
    delicado (fórmula, diagrama, imagen), reintenta con una versión simple: la página nunca se pierde."""
    prop_titulo, prop_fecha = await propiedades_db(notion, db_id)
    props: dict = {prop_titulo: {"title": [{"text": {"content": titulo[:RICH_MAX]}}]}}
    if fecha:
        if not prop_fecha:
            raise NotionError("Tu base de datos no tiene ninguna propiedad de tipo fecha.")
        props[prop_fecha] = {"date": {"start": fecha}}
    bloques = bloques or []
    degradada = False

    async def enviar(metodo: str, endpoint: str, base: dict, hijos: list[dict]) -> dict:
        nonlocal degradada
        try:
            return await notion.request(metodo, endpoint, {**base, **({"children": hijos} if hijos else {})})
        except NotionError as e:
            if "HTTP 400" not in str(e) or not hijos:
                raise
            log.warning("Notion rechazó parte del contenido; reintento con una versión simple: %s", str(e)[:200])
            degradada = True
            return await notion.request(metodo, endpoint, {**base, "children": degradar_bloques(hijos)})

    pagina = await enviar("POST", "pages", {"parent": {"database_id": db_id}, "properties": props}, bloques[:100])
    for i in range(100, len(bloques), 100):
        await enviar("PATCH", f"blocks/{pagina['id']}/children", {}, bloques[i : i + 100])
    pagina["_zapy_degradada"] = degradada
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

            habitos = resumen_habitos(await bot.almacen.tareas_terminadas(perfil.user_id, TAREAS_PARA_PROMPT),
                                      await bot.almacen.tarea_activa(perfil.user_id), _ahora_ts())
            system = construir_system_prompt(perfil, eventos, dt.datetime.now(TZ), habitos)
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
MSG_CONFIRMAR_BORRADO = ("¿Seguro? Se borrarán tu perfil, tu rutina, tus claves, tus asignaturas y tus "
                         "tareas y estadísticas de Zapy.")
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


async def _editar_seguro(mensaje: discord.Message | None, **cambios) -> None:
    """Edita un mensaje del bot; si ya no existe o Discord lo rechaza, se ignora."""
    if mensaje is None:
        return
    try:
        await mensaje.edit(**cambios)
    except discord.HTTPException:
        pass


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


def _campo_token(perfil: Perfil | None) -> discord.ui.TextInput:
    tiene = bool(perfil and perfil.notion_token)
    return discord.ui.TextInput(
        label="Token de tu integración de Notion",
        placeholder="Vacío = mantener el actual" if tiene else "ntn_... (o secret_...)",
        required=not tiene, max_length=300,
    )


async def _notion_validado(interaction: discord.Interaction, escrito: str, perfil: Perfil | None):
    """(token, cliente) si el token funciona (el escrito o, vacío, el ya guardado); si no, avisa y devuelve None."""
    token = escrito.strip() or (perfil.notion_token if perfil else None)
    if not token:
        await _avisar(interaction, "❌ Necesito el token de tu integración de Notion.")
        return None
    notion = Notion(token, bot.session)
    motivo = await comprobar_token_notion(notion)
    if motivo:
        await _avisar(interaction, f"❌ {motivo[0].upper() + motivo[1:]}. Mira el botón ❓ Ayuda.")
        return None
    return token, notion


class ModalNotion(_ModalBase):
    """Conectar Notion pegando los enlaces de bases de datos que el usuario ya tiene."""

    def __init__(self, vista: "PanelConfig", perfil: Perfil | None):
        super().__init__(vista, "Conectar tu Notion")
        self.token = _campo_token(perfil)
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
        validado = await _notion_validado(interaction, self.token.value, perfil)
        if validado is None:
            return
        token, notion = validado

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
        self.token = _campo_token(perfil)
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

        validado = await _notion_validado(interaction, self.token.value, perfil)  # tras las comprobaciones locales
        if validado is None:
            return
        token, notion = validado
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
            content="🗑️ Listo: he borrado tu perfil, tus claves, tus asignaturas y tus estadísticas. Lo que hay en tu "
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
        await _editar_seguro(self.mensaje, embed=embed_panel(perfil), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        await _editar_seguro(self.mensaje, view=self)

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
            MSG_CONFIRMAR_BORRADO,
            view=ConfirmarBorrado(self.user_id, self), ephemeral=True,
        )


# ----------------------------------------------------------------------------
# PLANIFICADOR: tareas con temporizador y estimaciones que aprenden de ti
# ----------------------------------------------------------------------------
TAREAS_PARA_PROMPT = 300      # las más recientes bastan para el resumen y acotan el coste de cada mensaje
MIN_TAREA_S = 120            # menos de 2 min: probablemente un toque accidental
MAX_TAREA_AUTO_MIN = 240     # más de 4 h contando: seguramente se olvidó parar el temporizador
MAX_MIN_ESTADISTICAS = 360   # lo que pase de 6 h no cuenta para las estadísticas
MAX_MIN_MANUAL = 600
MIN_MUESTRAS_GRUPO = 3       # tareas parecidas necesarias para corregir por asignatura / tipo
MIN_MUESTRAS_GLOBAL = 5      # tareas necesarias para corregir con todo el historial
PRIOR_FACTOR = 3             # "tirón" hacia 1: con pocos datos el factor se modera

_PALABRAS_TIPO = (  # el orden importa: la primera coincidencia gana
    ("practica", ("ejercicio", "problema", "practica", "resolver", "cuestion", "simulacro", "calcul")),
    ("redaccion", ("redact", "ensayo", "comentario", "escribir", "informe", "trabajo", "composicion")),
    ("memoria", ("memoriz", "aprender", "vocabulario", "repaso", "repasar", "flashcard", "ficha", "formula")),
    ("teoria", ("leer", "lectura", "teoria", "apuntes", "resumen", "esquema", "entender", "video")),
)

MSG_SIN_TAREA = "No tienes ninguna tarea en curso. Empieza una con `!empezar Asignatura | Tarea`."
MSG_USO_EMPEZAR = (
    "⚠️ Usa: `!empezar Asignatura | Tarea | minutos que estimas` (la tarea y los minutos son opcionales).\n"
    "Ejemplos: `!empezar Mates | ejercicios del tema 3 | 45` · `!empezar Inglés`"
)
MSG_VALORAR = "\n\nUn toque y Zapy aprende de ti: ¿cómo ha ido?"


def inferir_tipo(descripcion: str) -> str:
    texto = _norm(descripcion)
    for tipo, palabras in _PALABRAS_TIPO:
        if any(palabra in texto for palabra in palabras):
            return tipo
    return "otros"


def fmt_duracion(segundos: float) -> str:
    s = max(0, int(round(segundos)))
    if s < 60:
        return f"{s} s"
    minutos = s // 60
    if minutos < 60:
        return f"{minutos} min"
    horas, minutos = divmod(minutos, 60)
    return f"{horas} h {minutos:02d} min"


def _ahora_ts() -> int:
    return int(time.time())


# --- Estimaciones (código, no IA: Gemini solo recibe el resultado) ---------------
def cuenta_en_estadisticas(t: Tarea) -> bool:
    return (t.estado == "terminada" and t.real_s is not None
            and MIN_TAREA_S <= t.real_s <= MAX_MIN_ESTADISTICAS * 60)


def _ratios(tareas: list[Tarea]) -> list[float]:
    """real / estimado de cada tarea con estimación, acotado para que un caso raro no domine."""
    return [min(5.0, max(0.2, (t.real_s / 60) / t.estimado_min)) for t in tareas if t.estimado_min]


def factor_correccion(ratios: list[float]) -> float:
    """Media geométrica de real/estimado, moderada hacia 1 cuando hay pocos datos."""
    if not ratios:
        return 1.0
    return math.exp(sum(math.log(r) for r in ratios) / (len(ratios) + PRIOR_FACTOR))


def _percentil(valores: list[float], p: float) -> float:
    v = sorted(valores)
    k = (len(v) - 1) * p / 100
    i = int(k)
    j = min(i + 1, len(v) - 1)
    return v[i] + (v[j] - v[i]) * (k - i)


@dataclass(frozen=True)
class Estimacion:
    minutos: int
    bajo: int
    alto: int
    muestras: int   # tareas con estimación en las que se basa (0 = sin datos suficientes)
    base: str
    factor: float


def estimar(tareas: list[Tarea], clave: str, tipo: str, estimado_min: int) -> Estimacion:
    """Corrige la estimación del usuario con su historial: primero tareas de la misma asignatura y
    tipo, luego la asignatura y, por último, todo su historial."""
    validas = [t for t in tareas if cuenta_en_estadisticas(t) and t.estimado_min]
    niveles = (
        ("tu asignatura y tipo de tarea", [t for t in validas if t.clave == clave and t.tipo == tipo], MIN_MUESTRAS_GRUPO),
        ("tu asignatura", [t for t in validas if t.clave == clave], MIN_MUESTRAS_GRUPO),
        ("todas tus tareas", validas, MIN_MUESTRAS_GLOBAL),
    )
    for base, grupo, minimo in niveles:
        if len(grupo) < minimo:
            continue
        ratios = _ratios(grupo)
        factor = factor_correccion(ratios)
        centro = estimado_min * factor
        if len(ratios) >= 4:
            bajo, alto = estimado_min * _percentil(ratios, 25), estimado_min * _percentil(ratios, 75)
        else:
            bajo, alto = centro * 0.8, centro * 1.25
        bajo, alto = min(bajo, centro), max(alto, centro)
        return Estimacion(max(1, round(centro)), max(1, round(bajo)), max(1, round(alto)), len(ratios), base, factor)
    return Estimacion(estimado_min, estimado_min, estimado_min, 0, "sin datos suficientes", 1.0)


def duracion_tipica(tareas: list[Tarea], clave: str, tipo: str) -> tuple[int, int, str] | None:
    """Mediana de lo que tarda en tareas parecidas (minutos, nº de tareas, base), si hay datos."""
    validas = [t for t in tareas if cuenta_en_estadisticas(t)]
    for base, grupo in (("tu asignatura y tipo de tarea", [t for t in validas if t.clave == clave and t.tipo == tipo]),
                        ("tu asignatura", [t for t in validas if t.clave == clave])):
        if len(grupo) >= MIN_MUESTRAS_GRUPO:
            return round(_percentil([t.real_s / 60 for t in grupo], 50)), len(grupo), base
    return None


def _agrupar_por_clave(tareas: list[Tarea]) -> dict[str, list[Tarea]]:
    grupos: dict[str, list[Tarea]] = {}
    for t in tareas:  # llegan de más reciente a más antigua
        grupos.setdefault(t.clave, []).append(t)
    return grupos


def resumen_habitos(tareas: list[Tarea], activa: Tarea | None, ahora: int) -> str:
    """Texto breve con los hábitos MEDIDOS del usuario, para el prompt de Gemini."""
    lineas: list[str] = []
    if activa is not None:
        estado = "en pausa" if activa.estado == "pausada" else "contando"
        lineas.append(f"TAREA EN CURSO: {activa.asignatura}"
                      + (f" ({activa.descripcion})" if activa.descripcion else "")
                      + f", {estado}, {fmt_duracion(activa.transcurrido_s(ahora))} hechos.")
    validas = [t for t in tareas if cuenta_en_estadisticas(t)]
    if len(validas) < MIN_MUESTRAS_GLOBAL:
        lineas.append("Aún hay pocas tareas medidas: no prometas duraciones exactas y recuérdale que tus "
                      "estimaciones mejoran al registrar sus tareas con `!empezar` y `!terminar`.")
        return "\n".join(lineas)
    ratios = _ratios(validas)
    if len(ratios) >= MIN_MUESTRAS_GLOBAL:
        lineas.append(f"En conjunto tarda ×{factor_correccion(ratios):.2f} lo que estima ({len(ratios)} tareas con estimación).")
    grupos = sorted(_agrupar_por_clave(validas).values(), key=len, reverse=True)[:6]
    for grupo in grupos:
        mediana = round(_percentil([t.real_s / 60 for t in grupo], 50))
        linea = f"- {grupo[0].asignatura}: {len(grupo)} tareas, mediana {mediana} min"
        r = _ratios(grupo)
        if len(r) >= MIN_MUESTRAS_GRUPO:
            linea += f", suele tardar ×{factor_correccion(r):.2f} lo que estima"
        lineas.append(linea)
    return "\n".join(lineas)


def texto_estimacion(tareas: list[Tarea], t: Tarea) -> str:
    """Lo que Zapy le dice al empezar una tarea, según lo que ya sabe de él."""
    if t.estimado_min:
        est = estimar(tareas, t.clave, t.tipo, t.estimado_min)
        if est.muestras >= MIN_MUESTRAS_GRUPO:
            return (f"📊 Estimas {t.estimado_min} min; en tareas como esta sueles tardar ~{est.minutos} min "
                    f"(entre {est.bajo} y {est.alto}), según {est.muestras} tareas de {est.base}.")
        return "📊 Estimación anotada. Con unas cuantas tareas registradas te diré cuánto sueles tardar de verdad."
    tipica = duracion_tipica(tareas, t.clave, t.tipo)
    if tipica:
        minutos, n, base = tipica
        return f"⏳ En tareas como esta sueles tardar ~{minutos} min ({n} tareas de {base})."
    return ""


# --- Temporizador: lógica compartida por comandos y botones ----------------------------
_RE_MINUTOS = re.compile(r"^\s*(\d{1,3})\s*(?:min|mins|m|minutos)?\s*$", re.IGNORECASE)


def _parsear_minutos(texto: str) -> int | None:
    coincidencia = _RE_MINUTOS.match(texto or "")
    return int(coincidencia[1]) if coincidencia else None


def parsear_empezar(args: str) -> tuple[str, str, int | None] | None:
    """'Mates | ejercicios | 45' -> (asignatura, descripción, minutos estimados); None si no se entiende."""
    partes = [x.strip() for x in args.split("|")]
    asignatura = partes[0][:60]
    if not asignatura:
        return None
    descripcion, estimado = "", None
    if len(partes) == 2:
        solo_minutos = _parsear_minutos(partes[1])
        if solo_minutos is not None:
            estimado = solo_minutos
        else:
            descripcion = partes[1][:200]
    elif len(partes) >= 3:
        descripcion = partes[1][:200]
        if partes[2]:
            estimado = _parsear_minutos(partes[2])
            if estimado is None:
                return None
    if estimado is not None and not 1 <= estimado <= MAX_MIN_MANUAL:
        return None
    return asignatura, descripcion, estimado


async def tarea_empezar(user_id: int, args: str) -> tuple[Tarea | None, str]:
    datos = parsear_empezar(args)
    if datos is None:
        return None, MSG_USO_EMPEZAR
    asignatura, descripcion, estimado = datos
    clave = (alias_equivalentes(asignatura) or [asignatura.lower()])[0]
    tarea = await bot.almacen.iniciar_tarea(
        user_id, asignatura, clave, descripcion, inferir_tipo(descripcion), estimado, _ahora_ts()
    )
    if tarea is None:
        activa = await bot.almacen.tarea_activa(user_id)
        estado = "en pausa" if activa and activa.estado == "pausada" else "en curso"
        nombre = f" (**{activa.asignatura}**)" if activa else ""
        return None, (f"⚠️ Ya tienes una tarea {estado}{nombre}. Termínala con `!terminar`, "
                      "déjala con `!abandonar` o mira cómo va con `!tarea`.")
    return tarea, texto_estimacion(await bot.almacen.tareas_terminadas(user_id), tarea)


async def tarea_pausar(user_id: int) -> str:
    t = await bot.almacen.pausar_tarea(user_id, _ahora_ts())
    if t is not None:
        return f"⏸️ En pausa. Llevas {fmt_duracion(t.acumulado_s)} contados; el tiempo parado no cuenta. Sigue con `!reanudar`."
    activa = await bot.almacen.tarea_activa(user_id)
    return MSG_SIN_TAREA if activa is None else "⏸️ Ya estaba en pausa. Sigue con `!reanudar`."


async def tarea_reanudar(user_id: int) -> str:
    t = await bot.almacen.reanudar_tarea(user_id, _ahora_ts())
    if t is not None:
        return f"▶️ Reanudada. Llevas {fmt_duracion(t.acumulado_s)} contados hasta ahora."
    activa = await bot.almacen.tarea_activa(user_id)
    return MSG_SIN_TAREA if activa is None else "▶️ Ya estaba en marcha."


async def tarea_abandonar(user_id: int) -> str:
    t = await bot.almacen.cerrar_tarea(user_id, _ahora_ts(), "abandonada")
    if t is None:
        return MSG_SIN_TAREA
    return f"❌ Tarea **{t.asignatura}** abandonada. No cuenta para tus estadísticas."


async def tarea_terminar(user_id: int, minutos: int | None) -> tuple[Tarea | None, str]:
    activa = await bot.almacen.tarea_activa(user_id)
    if activa is None:
        return None, MSG_SIN_TAREA
    ahora = _ahora_ts()
    if minutos is None and activa.transcurrido_s(ahora) > MAX_TAREA_AUTO_MIN * 60:
        return None, (f"⚠️ Llevas {fmt_duracion(activa.transcurrido_s(ahora))} contando: parece que se te olvidó "
                      "parar el temporizador. Dime cuánto fue de verdad, por ejemplo `!terminar 90` (minutos), "
                      "o descártala con `!abandonar`.")
    t = await bot.almacen.cerrar_tarea(user_id, ahora, "terminada", minutos)
    if t is None:
        return None, MSG_SIN_TAREA
    partes = [f"✅ **{t.asignatura}** terminada: **{fmt_duracion(t.real_s)}**"
              + (" (tiempo puesto a mano)" if t.manual else "") + "."]
    if t.pausas:
        partes.append(f"Con {t.pausas} pausa{'s' if t.pausas != 1 else ''}, que no se han contado.")
    if t.estimado_min:
        ratio = (t.real_s / 60) / t.estimado_min
        if ratio > 1.15:
            partes.append(f"Estimabas {t.estimado_min} min: has tardado un {round((ratio - 1) * 100)}% más.")
        elif ratio < 0.87:
            partes.append(f"Estimabas {t.estimado_min} min: has tardado un {round((1 - ratio) * 100)}% menos.")
        else:
            partes.append(f"Estimabas {t.estimado_min} min: casi clavado.")
    if t.real_s < MIN_TAREA_S:
        partes.append("Ha sido muy corta, así que no cuenta para tus estadísticas.")
    elif t.real_s > MAX_MIN_ESTADISTICAS * 60:
        partes.append("Es demasiado larga para contar en tus estadísticas.")
    return t, " ".join(partes)


# --- Paneles y botones ------------------------------------------------------------------------
_paneles_tarea: dict[int, "TareaView"] = {}  # el último panel abierto de cada usuario, para mantenerlo al día


def embed_tarea(t: Tarea, ahora: int, nota: str = "") -> discord.Embed:
    en_curso = t.estado == "en_curso"
    descripcion = (nota + "\n\n" if nota else "") + "Botones o `!pausar` · `!reanudar` · `!terminar`."
    embed = discord.Embed(
        title="⏱️ Tarea en curso" if en_curso else "⏸️ Tarea en pausa",
        description=descripcion,
        color=discord.Color.green() if en_curso else discord.Color.blue(),
    )
    embed.add_field(name="📚 Tarea", value=(f"**{t.asignatura}**" + (f" · {t.descripcion}" if t.descripcion else ""))[:1000], inline=False)
    if en_curso:  # marca de Discord que se actualiza sola: "hace 12 minutos" sin tener que editar el mensaje
        embed.add_field(name="⏱️ Contando desde", value=f"<t:{ahora - t.transcurrido_s(ahora)}:R>", inline=True)
    else:
        embed.add_field(name="⏸️ Contado", value=fmt_duracion(t.acumulado_s), inline=True)
    if t.estimado_min:
        embed.add_field(name="🎯 Estimado", value=f"{t.estimado_min} min", inline=True)
    if t.pausas:
        embed.add_field(name="Pausas", value=str(t.pausas), inline=True)
    return embed


async def refrescar_panel_tarea(user_id: int, nota: str = "") -> None:
    """Pone al día el panel abierto del usuario (o lo cierra si ya no tiene tarea activa)."""
    vista = _paneles_tarea.get(user_id)
    if vista is None or vista.mensaje is None:
        return
    activa = await bot.almacen.tarea_activa(user_id)
    if activa is not None:
        await _editar_seguro(vista.mensaje, embed=embed_tarea(activa, _ahora_ts(), nota), view=vista)
    else:
        _paneles_tarea.pop(user_id, None)
        await _editar_seguro(vista.mensaje, content=nota or "Tarea cerrada.", embed=None, view=None)


class ValoracionView(discord.ui.View):
    """Dos selectores de un toque tras terminar: cansancio y dificultad. Cada respuesta se guarda al instante."""

    def __init__(self, tarea_id: int, user_id: int, contenido: str):
        super().__init__(timeout=900)
        self.tarea_id, self.user_id, self.contenido = tarea_id, user_id, contenido
        self.hecho: set[str] = set()
        self.mensaje: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _guardar(self, interaction: discord.Interaction, campo: str, valor: int) -> None:
        await bot.almacen.valorar_tarea(self.tarea_id, self.user_id, **{campo: valor})
        self.hecho.add(campo)
        if len(self.hecho) == 2:
            self.stop()
            await interaction.response.edit_message(content=self.contenido + "\n\n✅ Anotado, gracias.", view=None)
        else:
            await interaction.response.defer()

    async def on_timeout(self) -> None:
        await _editar_seguro(self.mensaje, content=self.contenido, view=None)

    @discord.ui.select(placeholder="¿Cómo de cansado/a estabas?", row=0, options=[
        discord.SelectOption(label="1 · Nada cansado/a", value="1"),
        discord.SelectOption(label="2 · Algo cansado/a", value="2"),
        discord.SelectOption(label="3 · Normal", value="3"),
        discord.SelectOption(label="4 · Bastante cansado/a", value="4"),
        discord.SelectOption(label="5 · Agotado/a", value="5"),
    ])
    async def selector_cansancio(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self._guardar(interaction, "cansancio", int(select.values[0]))

    @discord.ui.select(placeholder="¿Qué tal de difícil fue?", row=1, options=[
        discord.SelectOption(label="1 · Muy fácil", value="1"),
        discord.SelectOption(label="2 · Fácil", value="2"),
        discord.SelectOption(label="3 · Normal", value="3"),
        discord.SelectOption(label="4 · Difícil", value="4"),
        discord.SelectOption(label="5 · Muy difícil", value="5"),
    ])
    async def selector_dificultad(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self._guardar(interaction, "dificultad", int(select.values[0]))


class TareaView(discord.ui.View):
    """Botones de la tarea en curso. El estado vive en la base de datos: los comandos siempre funcionan."""

    def __init__(self, user_id: int):
        super().__init__(timeout=12 * 3600)
        self.user_id = user_id
        self.mensaje: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Esta tarea es de otra persona. Usa `!empezar` para la tuya.", ephemeral=True)
            return False
        return True

    async def _preparar(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if interaction.message is not None:
            self.mensaje = interaction.message
        _paneles_tarea[self.user_id] = self

    async def on_timeout(self) -> None:
        _paneles_tarea.pop(self.user_id, None)
        for item in self.children:
            item.disabled = True
        await _editar_seguro(self.mensaje, view=self)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log.error("Error en el panel de tarea", exc_info=error)
        await _avisar(interaction, "❌ Algo ha fallado. Prueba con `!tarea`.")

    @discord.ui.button(label="Pausar", emoji="⏸️", style=discord.ButtonStyle.secondary, row=0)
    async def boton_pausar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._preparar(interaction)
        await refrescar_panel_tarea(self.user_id, await tarea_pausar(self.user_id))

    @discord.ui.button(label="Reanudar", emoji="▶️", style=discord.ButtonStyle.primary, row=0)
    async def boton_reanudar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._preparar(interaction)
        await refrescar_panel_tarea(self.user_id, await tarea_reanudar(self.user_id))

    @discord.ui.button(label="Terminar", emoji="✅", style=discord.ButtonStyle.success, row=0)
    async def boton_terminar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._preparar(interaction)
        tarea, texto = await tarea_terminar(self.user_id, None)
        if tarea is None:
            await interaction.followup.send(texto, ephemeral=True)
            return
        _paneles_tarea.pop(self.user_id, None)
        vista = ValoracionView(tarea.id, self.user_id, texto)
        vista.mensaje = self.mensaje
        if self.mensaje is not None:
            await self.mensaje.edit(content=texto + MSG_VALORAR, embed=None, view=vista)

    @discord.ui.button(label="Abandonar", emoji="❌", style=discord.ButtonStyle.danger, row=0)
    async def boton_abandonar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._preparar(interaction)
        await refrescar_panel_tarea(self.user_id, await tarea_abandonar(self.user_id))


# --- Estadísticas ------------------------------------------------------------------------------
def embed_estadisticas(tareas: list[Tarea]) -> discord.Embed:
    validas = [t for t in tareas if cuenta_en_estadisticas(t)]
    if not validas:
        return discord.Embed(
            title="📊 Tus estadísticas de estudio", color=discord.Color.blue(),
            description="Aún no tienes tareas completadas. Empieza una con `!empezar Mates | ejercicios | 45`, "
                        "ciérrala con `!terminar` y Zapy irá aprendiendo cuánto tardas de verdad.",
        )
    ratios = _ratios(validas)
    descripcion = f"**{len(validas)}** tareas · **{fmt_duracion(sum(t.real_s for t in validas))}** en total."
    if len(validas) < MIN_MUESTRAS_GLOBAL:
        descripcion += "\nTodavía son pocos datos: las estimaciones serán más finas con el uso."
    embed = discord.Embed(title="📊 Tus estadísticas de estudio", description=descripcion, color=discord.Color.blue())
    if len(ratios) >= MIN_MUESTRAS_GLOBAL:
        f = factor_correccion(ratios)
        veredicto = ("sueles tardar más de lo que estimas" if f > 1.15
                     else "sueles terminar antes de lo estimado" if f < 0.87 else "tus estimaciones son bastante buenas")
        embed.add_field(name="🎯 Tus estimaciones", value=f"×{f:.2f}: {veredicto} ({len(ratios)} tareas con estimación).", inline=False)
    lineas = []
    for grupo in sorted(_agrupar_por_clave(validas).values(), key=len, reverse=True)[:8]:
        mediana = round(_percentil([t.real_s / 60 for t in grupo], 50))
        linea = f"**{grupo[0].asignatura}** · {len(grupo)} tareas · mediana {mediana} min"
        r = _ratios(grupo)
        if len(r) >= MIN_MUESTRAS_GRUPO:
            linea += f" · ×{factor_correccion(r):.2f}"
        lineas.append(linea)
    embed.add_field(name="📖 Por asignatura", value="\n".join(lineas)[:1000], inline=False)
    cansancios = [t.cansancio for t in validas if t.cansancio]
    dificultades = [t.dificultad for t in validas if t.dificultad]
    if len(cansancios) >= 3:
        embed.add_field(name="😴 Cansancio y dificultad",
                        value=f"Cansancio medio {sum(cansancios) / len(cansancios):.1f}/5"
                              + (f" · dificultad media {sum(dificultades) / len(dificultades):.1f}/5" if len(dificultades) >= 3 else ""),
                        inline=False)
    return embed


async def enviar_privado(ctx: commands.Context, **kwargs):
    """Tus hábitos de estudio son personales: van por mensaje privado y, si no se puede, al canal."""
    try:
        mensaje = await ctx.author.send(**kwargs)
    except discord.Forbidden:
        return await ctx.send(**kwargs)
    if not isinstance(ctx.channel, discord.DMChannel):
        origen = getattr(ctx, "message", None)
        if origen is not None:
            try:
                await origen.add_reaction("📩")
            except discord.HTTPException:
                pass
    return mensaje


# ----------------------------------------------------------------------------
# COMANDOS
# ----------------------------------------------------------------------------
@bot.command(name="comandos")
async def mostrar_comandos(ctx: commands.Context):
    embed = discord.Embed(title="🤖 Comandos de Zapy", color=discord.Color.blue())
    embed.add_field(name="⚙️ Configurar", value="`!configurar` - Tu clave de Gemini, tu rutina y tu Notion.", inline=False)
    embed.add_field(
        name="⏱️ Planificador",
        value=("`!empezar Mates | ejercicios | 45` - Empieza una tarea y el temporizador.\n"
               "`!pausar` / `!reanudar` - Si te interrumpen (la pausa no cuenta).\n"
               "`!terminar` (o `!terminar 45`) - Cierra la tarea. `!abandonar` la descarta.\n"
               "`!tarea` - Ver la tarea en curso. `!estadisticas` - Cuánto tardas de verdad."),
        inline=False,
    )
    embed.add_field(name="📅 Notion", value="`!eventos` - Lista los próximos exámenes/tareas.", inline=False)
    embed.add_field(name="➕ Añadir", value="`!añadir Nombre | AAAA-MM-DD` - Guarda un evento.", inline=False)
    embed.add_field(name="🚀 Masterclass", value="`!apuntes Asignatura | Tema` - Masterclass en Notion con fórmulas, diagramas y gráficas.", inline=False)
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
        MSG_CONFIRMAR_BORRADO,
        view=ConfirmarBorrado(ctx.author.id),
    )


@bot.command(name="empezar")
@commands.cooldown(2, 5, commands.BucketType.user)
async def empezar_tarea(ctx: commands.Context, *, args: str = ""):
    tarea, nota = await tarea_empezar(ctx.author.id, args)
    if tarea is None:
        await enviar_privado(ctx, content=nota)
        return
    vista = TareaView(ctx.author.id)
    vista.mensaje = await enviar_privado(ctx, embed=embed_tarea(tarea, _ahora_ts(), nota), view=vista)
    _paneles_tarea[ctx.author.id] = vista


@bot.command(name="tarea")
@commands.cooldown(2, 5, commands.BucketType.user)
async def ver_tarea(ctx: commands.Context):
    activa = await bot.almacen.tarea_activa(ctx.author.id)
    if activa is None:
        await enviar_privado(ctx, content=MSG_SIN_TAREA)
        return
    vista = TareaView(ctx.author.id)
    vista.mensaje = await enviar_privado(ctx, embed=embed_tarea(activa, _ahora_ts()), view=vista)
    _paneles_tarea[ctx.author.id] = vista


@bot.command(name="pausar", aliases=["pausa"])
@commands.cooldown(3, 5, commands.BucketType.user)
async def pausar_tarea(ctx: commands.Context):
    texto = await tarea_pausar(ctx.author.id)
    await enviar_privado(ctx, content=texto)
    await refrescar_panel_tarea(ctx.author.id, texto)


@bot.command(name="reanudar", aliases=["continuar", "seguir"])
@commands.cooldown(3, 5, commands.BucketType.user)
async def reanudar_tarea(ctx: commands.Context):
    texto = await tarea_reanudar(ctx.author.id)
    await enviar_privado(ctx, content=texto)
    await refrescar_panel_tarea(ctx.author.id, texto)


@bot.command(name="terminar", aliases=["fin", "acabar"])
@commands.cooldown(2, 5, commands.BucketType.user)
async def terminar_tarea(ctx: commands.Context, *, args: str = ""):
    minutos = None
    if args.strip():
        minutos = _parsear_minutos(args)
        if minutos is None or not 1 <= minutos <= MAX_MIN_MANUAL:
            await enviar_privado(ctx, content="⚠️ Usa `!terminar` o, para poner el tiempo a mano, `!terminar 45` (minutos).")
            return
    tarea, texto = await tarea_terminar(ctx.author.id, minutos)
    if tarea is None:
        await enviar_privado(ctx, content=texto)
        return
    vista = ValoracionView(tarea.id, ctx.author.id, texto)
    vista.mensaje = await enviar_privado(ctx, content=texto + MSG_VALORAR, view=vista)
    await refrescar_panel_tarea(ctx.author.id, "✅ Tarea terminada.")


@bot.command(name="abandonar")
@commands.cooldown(2, 5, commands.BucketType.user)
async def abandonar_tarea(ctx: commands.Context):
    texto = await tarea_abandonar(ctx.author.id)
    await enviar_privado(ctx, content=texto)
    await refrescar_panel_tarea(ctx.author.id, texto)


@bot.command(name="estadisticas", aliases=["stats", "estadísticas"])
@commands.cooldown(1, 10, commands.BucketType.user)
async def ver_estadisticas(ctx: commands.Context):
    await enviar_privado(ctx, embed=embed_estadisticas(await bot.almacen.tareas_terminadas(ctx.author.id)))


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
            prompt += f"\n\nPISTAS PARA ESTA MATERIA: {pistas_asignatura(asignatura)}"
            if algoritmo:
                prompt += f"\n\n--- METODOLOGÍA Y ALGORITMO ---\n{algoritmo}"
            markdown = await generar_texto(
                cliente_gemini(perfil), prompt, SYSTEM_PROMPT_MASTERCLASS, MAX_TOKENS_MASTERCLASS
            )
            notion = bot.notion_de(perfil)
            bloques, graficas_fallidas = await resolver_graficas(notion, markdown_a_bloques(markdown))
            pagina = await crear_pagina_notion(notion, db_id, f"Masterclass: {tema}", bloques=bloques)
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
    informe = informe_visual(bloques, graficas_fallidas, pagina.get("_zapy_degradada", False))
    await ctx.send(
        f"🚀 **Masterclass generada:** **{tema}** ({asignatura.capitalize()}) publicada en Notion.\n{enlace}"
        + (f"\n{informe}" if informe else ""),
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

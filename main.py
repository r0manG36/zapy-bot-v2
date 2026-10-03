"""Zapy 1.3.0 - Tutor académico en Discord (Gemini + Notion).

Cambios principales respecto a 1.2.2: ver el resumen de la revisión.
Dependencias: discord.py>=2.3, aiohttp (viene con discord.py), python-dotenv,
google-genai y, en Windows, tzdata (para zoneinfo).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord.ext import commands, tasks
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

client_gemini = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

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
# BOT
# ----------------------------------------------------------------------------
class ZapyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.session: aiohttp.ClientSession | None = None
        self.notion: Notion | None = None
        # None = aún no sabemos qué eventos existían (primer arranque).
        self.ids_vistos: set[str] | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        if NOTION_TOKEN:
            self.notion = Notion(NOTION_TOKEN, self.session)
        self.ids_vistos = await asyncio.to_thread(_cargar_ids_disco)
        comprobar_nuevos_eventos.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()


bot = ZapyBot()


def autorizado(user: discord.abc.User) -> bool:
    return not USUARIOS_PERMITIDOS or user.id in USUARIOS_PERMITIDOS


@bot.check
async def _solo_autorizados(ctx: commands.Context) -> bool:
    return autorizado(ctx.author)


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


_cache_eventos: tuple[float, list[Evento]] | None = None


async def obtener_eventos(forzar: bool = False) -> list[Evento]:
    """Devuelve los eventos de Notion (caché de 60 s). Lanza NotionError si falla."""
    global _cache_eventos
    ahora = time.monotonic()
    if not forzar and _cache_eventos and ahora - _cache_eventos[0] < CACHE_EVENTOS_TTL:
        return _cache_eventos[1]
    if not bot.notion or not NOTION_DATABASE_ID:
        raise NotionError("`NOTION_TOKEN` o `NOTION_DATABASE_ID` no están configurados en el `.env`.")
    paginas = await bot.notion.query_database(NOTION_DATABASE_ID)
    eventos = [_pagina_a_evento(p) for p in paginas]
    _cache_eventos = (ahora, eventos)
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


def _guardar_ids_disco(ids: set[str]) -> None:
    try:
        tmp = NOTION_IDS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")
        tmp.replace(NOTION_IDS_FILE)  # escritura atómica
    except OSError:
        log.exception("No pude guardar %s", NOTION_IDS_FILE)


@tasks.loop(seconds=60)
async def comprobar_nuevos_eventos():
    if not (bot.notion and NOTION_DATABASE_ID and CANAL_NOTIFICACIONES_ID):
        return
    try:
        eventos = await obtener_eventos(forzar=True)
        actuales = {e.id for e in eventos}

        if bot.ids_vistos is None:  # primer arranque: línea base sin avisar
            bot.ids_vistos = actuales
            await asyncio.to_thread(_guardar_ids_disco, actuales)
            return

        canal = bot.get_channel(CANAL_NOTIFICACIONES_ID)
        if canal is None:
            return

        avisados: set[str] = set()
        for ev in (e for e in eventos if e.id not in bot.ids_vistos):
            embed = discord.Embed(
                title="🆕 Nuevo evento en Notion",
                description="Se ha detectado una nueva entrada en tu calendario.",
                color=discord.Color.green(),
            )
            embed.add_field(name="📌 Evento", value=ev.nombre[:1024], inline=False)
            embed.add_field(name="📅 Fecha", value=ev.fecha_texto, inline=False)
            try:
                await canal.send(embed=embed)
                avisados.add(ev.id)
            except discord.HTTPException:
                log.exception("No pude avisar del evento %s (se reintentará)", ev.id)

        if avisados:
            bot.ids_vistos |= avisados
            await asyncio.to_thread(_guardar_ids_disco, bot.ids_vistos)
    except NotionError as e:
        log.warning("Notion no disponible: %s", e)
    except Exception:
        log.exception("Fallo inesperado en el bucle de eventos")


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


def resolver_asignatura(nombre: str) -> str | None:
    return ASIGNATURAS.get(_norm(nombre))


_cache_algoritmos: dict[str, tuple[float, str]] = {}


async def obtener_algoritmo_asignatura(db_id: str) -> str:
    """Metodología de la asignatura guardada en su base de datos de Notion."""
    ahora = time.monotonic()
    cacheado = _cache_algoritmos.get(db_id)
    if cacheado and ahora - cacheado[0] < CACHE_ALGORITMOS_TTL:
        return cacheado[1]
    if not bot.notion:
        return ""
    try:
        paginas = await bot.notion.query_database(db_id)
        # Las masterclasses generadas viven en la misma base de datos: no se
        # reinyectan como "metodología" o el prompt crecería en cada uso.
        paginas = [
            p for p in paginas
            if not _extraer_titulo(p.get("properties", {})).startswith("Masterclass:")
        ]
        textos = await asyncio.gather(
            *(bot.notion.children(p["id"]) for p in paginas)
        )
        contenido = "\n\n---\n\n".join(t for t in map(_bloques_a_texto, textos) if t)
        contenido = contenido[:MAX_ALGORITMO_CHARS]
    except NotionError as e:
        log.warning("No pude leer la metodología (%s): %s", db_id, e)
        return ""  # no se cachea el fallo
    _cache_algoritmos[db_id] = (ahora, contenido)
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


async def crear_pagina_notion(db_id: str, titulo: str, fecha: str | None = None,
                              bloques: list[dict] | None = None) -> dict:
    """Crea una página; los primeros 100 bloques van en la misma petición."""
    assert bot.notion is not None
    props: dict = {PROP_TITULO: {"title": [{"text": {"content": titulo[:RICH_MAX]}}]}}
    if fecha:
        props[PROP_FECHA] = {"date": {"start": fecha}}
    cuerpo: dict = {"parent": {"database_id": db_id}, "properties": props}
    bloques = bloques or []
    if bloques:
        cuerpo["children"] = bloques[:100]
    pagina = await bot.notion.request("POST", "pages", cuerpo)
    for i in range(100, len(bloques), 100):
        await bot.notion.request(
            "PATCH", f"blocks/{pagina['id']}/children", {"children": bloques[i : i + 100]}
        )
    return pagina


# ----------------------------------------------------------------------------
# GEMINI
# ----------------------------------------------------------------------------
async def generar_texto(contents, system: str, max_tokens: int, temperature: float = 0.3) -> str:
    if not client_gemini:
        raise RuntimeError("API de Gemini no configurada")
    config = types.GenerateContentConfig(
        system_instruction=system, temperature=temperature, max_output_tokens=max_tokens
    )
    for intento in range(3):
        try:
            resp = await client_gemini.aio.models.generate_content(
                model=GEMINI_MODEL, contents=contents, config=config
            )
        except genai_errors.APIError as e:
            if e.code in {429, 500, 503, 504} and intento < 2:
                await asyncio.sleep(2 ** (intento + 1))
                continue
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
    if not client_gemini:
        await message.channel.send("⚠️ API de Gemini no configurada.")
        return

    texto = limpiar_mencion(message.content)
    if not texto:
        await message.reply("¿En qué te ayudo? Cuéntame qué quieres planificar o estudiar.",
                            mention_author=False)
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
            try:
                eventos = formatear_eventos(await obtener_eventos())
            except NotionError as e:
                log.warning("Eventos no disponibles para la IA: %s", e)
                eventos = "(no disponibles en este momento)"

            ahora = dt.datetime.now(TZ)
            system = (
                f"{SYSTEM_PROMPT_BASE}\n\n"
                f"HOY ES: {DIAS_SEMANA[ahora.weekday()]}, {ahora:%d/%m/%Y}, {ahora:%H:%M}\n\n"
                f"EXÁMENES Y EVENTOS PRÓXIMOS EN NOTION:\n{eventos}"
            )
            conversacion = await construir_conversacion(message, texto)
            respuesta = await generar_texto(conversacion, system, MAX_TOKENS_CHAT)
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
    log.info("✅ Zapy activo como %s (eventos conocidos: %s)", bot.user,
             len(bot.ids_vistos) if bot.ids_vistos is not None else "línea base pendiente")


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
    try:
        eventos = await obtener_eventos(forzar=True)
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
    if not bot.notion or not NOTION_DATABASE_ID:
        await ctx.send("❌ Notion no está configurado en el `.env`.")
        return

    async with ctx.typing():
        try:
            await crear_pagina_notion(NOTION_DATABASE_ID, nombre, fecha)
            await obtener_eventos(forzar=True)
        except NotionError as e:
            log.warning("Error creando evento: %s", e)
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

    db_id = resolver_asignatura(asignatura)
    if not db_id:
        disponibles = ", ".join(f"`{a}`" for a in sorted(ASIGNATURAS)) or "ninguna configurada en el `.env`"
        await ctx.send(f"⚠️ No conozco la asignatura **{asignatura}**. Disponibles: {disponibles}",
                       allowed_mentions=SIN_MENCIONES)
        ctx.command.reset_cooldown(ctx)
        return
    if not client_gemini or not bot.notion:
        await ctx.send("❌ Faltan `GEMINI_API_KEY` o `NOTION_TOKEN` en el `.env`.")
        return

    async with ctx.typing():
        try:
            algoritmo = await obtener_algoritmo_asignatura(db_id)
            prompt = f"Elabora la Masterclass sobre '{tema}' de '{asignatura}'."
            if algoritmo:
                prompt += f"\n\n--- METODOLOGÍA Y ALGORITMO ---\n{algoritmo}"
            markdown = await generar_texto(prompt, SYSTEM_PROMPT_MASTERCLASS, MAX_TOKENS_MASTERCLASS)
            pagina = await crear_pagina_notion(
                db_id, f"Masterclass: {tema}", bloques=markdown_a_bloques(markdown)
            )
        except NotionError as e:
            log.warning("Error exportando masterclass: %s", e)
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
    bot.run(TOKEN, log_handler=None)  # el logging ya está configurado arriba


if __name__ == "__main__":
    main()

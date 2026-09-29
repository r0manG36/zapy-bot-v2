import asyncio
import datetime
import json
import os
import re
import urllib.request
import urllib.error
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
CANAL_NOTIFICACIONES_ID = os.getenv("CANAL_NOTIFICACIONES_ID")

GEMINI_MODEL = "gemini-3.5-flash-lite"

NOTION_ASIGNATURAS_MAP = {
    "mates": os.getenv("NOTION_MATES_ID"),
    "matematicas": os.getenv("NOTION_MATES_ID"),
    "fyq": os.getenv("NOTION_FYQ_ID"),
    "fisica y quimica": os.getenv("NOTION_FYQ_ID"),
    "fisica": os.getenv("NOTION_FYQ_ID"),
    "quimica": os.getenv("NOTION_FYQ_ID"),
    "tecno": os.getenv("NOTION_TECNO_ID"),
    "tecnologia": os.getenv("NOTION_TECNO_ID"),
    "digi": os.getenv("NOTION_DIGI_ID"),
    "digitalizacion": os.getenv("NOTION_DIGI_ID"),
    "robotica": os.getenv("NOTION_ROBOTICA_ID"),
    "euskera": os.getenv("NOTION_EUSKERA_ID"),
    "lengua": os.getenv("NOTION_LENGUA_ID"),
    "ingles": os.getenv("NOTION_INGLES_ID"),
    "geo-hist": os.getenv("NOTION_GEOHIST_ID"),
    "geografia e historia": os.getenv("NOTION_GEOHIST_ID"),
    "historia": os.getenv("NOTION_GEOHIST_ID"),
    "geografia": os.getenv("NOTION_GEOHIST_ID"),
}

client_gemini = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

NOTION_CACHE_FILE = "notion_ids.json"

IDS_MEMORIA_RAM = set()
NOTION_EVENTOS_CACHE = None
NOTION_CACHE_TIMESTAMP = None
CACHE_TTL_SEGUNDOS = 60

CACHE_ALGORITMOS_RAM = {}
TTL_ALGORITMOS_SEGUNDOS = 3600

SYSTEM_PROMPT_BASE = """Zapy, eres un tutor académico experto en métodos de estudio eficientes y optimización del tiempo para 4º ESO.
Responde de forma concisa y directa sin rodeos introductorios.

Rutina Semanal Base:
- Lunes a Viernes: Clases 8:15 - 14:15.
- Lunes/Miércoles: Entreno 17:30 - 20:30.
- Martes: Familia/Cena 20:00 - 21:30.
- Jueves: Entreno 19:30 - 21:45.
- Viernes tarde: Sin estudio.
- Sábado: Partido por la mañana/mediodía (libre desde las 16:00).
- Domingo: Ocupado 13:00 - 16:00.

Genera la planificación evaluando en qué día y fecha concreta te encuentras hoy respecto a las fechas de los exámenes en Notion. Organiza únicamente los días venideros."""

SYSTEM_PROMPT_MASTERCLASS = """Zapy, actúa como un catedrático y tutor académico de excelencia, especialista en pedagogía de alto rendimiento y preparación para exámenes de ESO y Bachillerato. Tu habilidad principal es transformar temarios complejos en "Masterclasses" hiperdetalladas, rigurosas e imborrables para la memoria.

REGLAS DE FORMATO MATEMÁTICO Y SÍMBOLOS:
1. Usa sintaxis LaTeX limpia. Ecuaciones en línea de texto: $expresion$. Ecuaciones centradas independientes: $$expresion$$.
2. NO uses caracteres Unicode extraños o símbolos raros de formato para superíndices o subíndices; utiliza siempre LaTeX estricto (ej. $x^2$, $H_2O$).
3. Evita saltos de línea dentro de bloques de ecuaciones LaTeX.

ESTRUCTURA:
- INTRODUCCIÓN: Clara y directa.
- FASE DE APRENDIZAJE: Explicación paso a paso con ejemplos.
- FASE DE EJERCICIOS DE PRÁCTICA: De fáciles a difíciles.
- ERRORES TÍPICOS Y SOLUCIONES: Explicación y resolución completa."""


# --- CLIENTE HTTP DIRECTO PARA NOTION ---
def _http_notion_request(endpoint, method="POST", payload=None):
    if not NOTION_TOKEN:
        return None
    
    url = f"https://api.notion.com/v1/{endpoint.strip('/')}"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN.strip()}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    data = json.dumps(payload).encode('utf-8') if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"❌ Error HTTP Notion ({endpoint}): {e}")
        return None
    return None


def _extraer_titulo_pagina(properties):
    for key, val in properties.items():
        if isinstance(val, dict) and val.get("type") == "title":
            title_list = val.get("title", [])
            if title_list:
                texto = "".join([t.get("plain_text", "") for t in title_list])
                if texto:
                    return texto
    return "Sin título"


def _extraer_fecha_pagina(properties):
    for key, val in properties.items():
        if isinstance(val, dict) and val.get("type") == "date":
            date_data = val.get("date")
            if date_data:
                inicio = date_data.get("start", "")
                fin = date_data.get("end", "")
                return f"{inicio} -> {fin}" if fin else inicio
    return "Sin fecha"


async def obtener_eventos_notion(forzar_refresco=False):
    global NOTION_EVENTOS_CACHE, NOTION_CACHE_TIMESTAMP
    ahora = datetime.datetime.now()
    if (
        not forzar_refresco
        and NOTION_EVENTOS_CACHE is not None
        and NOTION_CACHE_TIMESTAMP
    ):
        if (ahora - NOTION_CACHE_TIMESTAMP).total_seconds() < CACHE_TTL_SEGUNDOS:
            return NOTION_EVENTOS_CACHE

    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        return "❌ `NOTION_TOKEN` o `NOTION_DATABASE_ID` no están configurados en el `.env`."

    try:
        endpoint = f"databases/{NOTION_DATABASE_ID.strip()}/query"
        response = await asyncio.to_thread(_http_notion_request, endpoint, "POST")
        if not response:
            return "❌ No se pudo conectar con Notion."

        results = response.get("results", [])
        if not results:
            res_texto = "📅 No hay eventos ni exámenes registrados actualmente en Notion."
            NOTION_EVENTOS_CACHE = res_texto
            NOTION_CACHE_TIMESTAMP = ahora
            return res_texto

        eventos = []
        for page in results:
            props = page.get("properties", {})
            nombre = _extraer_titulo_pagina(props)
            fecha = _extraer_fecha_pagina(props)
            eventos.append(f"• **{nombre}** — `{fecha}`")

        res_texto = "\n".join(eventos)
        NOTION_EVENTOS_CACHE = res_texto
        NOTION_CACHE_TIMESTAMP = ahora
        return res_texto
    except Exception as e:
        return f"❌ Error al consultar Notion: {e}"


def _crear_tarea_notion_sync(nombre, fecha_str):
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        return False
    try:
        nueva_pagina = {
            "parent": {"database_id": NOTION_DATABASE_ID.strip()},
            "properties": {"Nombre": {"title": [{"text": {"content": nombre}}]}},
        }
        if fecha_str:
            nueva_pagina["properties"]["Fecha"] = {"date": {"start": fecha_str}}
            
        res = _http_notion_request("pages", "POST", nueva_pagina)
        return res is not None
    except Exception as e:
        print(f"Error creando tarea Notion: {e}")
        return False


def _extraer_texto_de_bloques(block_list):
    lineas = []
    for block in block_list:
        b_type = block.get("type")
        if b_type in ["paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item"]:
            rich = block.get(b_type, {}).get("rich_text", [])
            texto = "".join([t.get("plain_text", "") for t in rich])
            if texto:
                if b_type == "heading_1":
                    lineas.append(f"# {texto}")
                elif b_type == "heading_2":
                    lineas.append(f"## {texto}")
                elif b_type == "heading_3":
                    lineas.append(f"### {texto}")
                elif b_type == "bulleted_list_item":
                    lineas.append(f"- {texto}")
                else:
                    lineas.append(texto)
        elif b_type == "equation":
            expr = block.get("equation", {}).get("expression", "")
            if expr:
                lineas.append(f"$${expr}$$")
    return "\n".join(lineas)


def _obtener_algoritmo_notion_sync(asignatura):
    db_id = NOTION_ASIGNATURAS_MAP.get(asignatura.lower())
    if not db_id:
        return ""
    try:
        res = _http_notion_request(f"databases/{db_id.strip()}/query", "POST")
        if not res:
            return ""
        pages = res.get("results", [])
        contenido_total = []
        for p in pages:
            page_id = p["id"]
            b_res = _http_notion_request(f"blocks/{page_id}/children", "GET")
            blocks = b_res.get("results", []) if b_res else []
            texto_pagina = _extraer_texto_de_bloques(blocks)
            if texto_pagina:
                contenido_total.append(texto_pagina)
        return "\n\n---\n\n".join(contenido_total)
    except Exception as e:
        print(f"Error algoritmo Notion ({asignatura}): {e}")
        return ""


async def obtener_algoritmo_asignatura(asignatura):
    global CACHE_ALGORITMOS_RAM
    clave = asignatura.lower()
    ahora = datetime.datetime.now().timestamp()
    if clave in CACHE_ALGORITMOS_RAM:
        data, ts = CACHE_ALGORITMOS_RAM[clave]
        if ahora - ts < TTL_ALGORITMOS_SEGUNDOS:
            return data
    contenido = await asyncio.to_thread(_obtener_algoritmo_notion_sync, asignatura)
    CACHE_ALGORITMOS_RAM[clave] = (contenido, ahora)
    return contenido


def _cargar_ids_disco():
    if os.path.exists(NOTION_CACHE_FILE):
        try:
            with open(NOTION_CACHE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"Error leyendo caché disco: {e}")
    return set()


def _guardar_ids_disco(ids_set):
    try:
        with open(NOTION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(ids_set), f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error guardando caché disco: {e}")


# --- DEPURADOR DE SÍMBOLOS Y SINTAXIS LATEX ---
def _limpiar_y_sanear_latex(texto):
    texto = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", texto)
    texto = texto.replace("\r\n", "\n")
    # Limpia espacios innecesarios pegados a los delimitadores $$     texto = re.sub(r"\$\$\s+", "$$", texto)
    texto = re.sub(r"\s+\$\$", "$$", texto)     return texto   def _convertir_linea_a_bloque_notion(linea):     linea = _limpiar_y_sanear_latex(linea.strip())     if not linea:         return None      # Ecuaciones independientes centradas ($$)
    if linea.startswith("$$") and linea.endswith("$$") and len(linea) > 4:
        expr = linea[2:-2].strip()
        return {
            "object": "block",
            "type": "equation",
            "equation": {"expression": expr[:1000]},
        }

    b_type = "paragraph"
    prefijo_len = 0

    if linea.startswith("# "):
        b_type = "heading_1"
        prefijo_len = 2
    elif linea.startswith("## "):
        b_type = "heading_2"
        prefijo_len = 3
    elif linea.startswith("### "):
        b_type = "heading_3"
        prefijo_len = 4
    elif linea.startswith("- ") or linea.startswith("* "):
        b_type = "bulleted_list_item"
        prefijo_len = 2

    texto_contenido = linea[prefijo_len:].strip()

    rich_text = []
    # Parsea ecuaciones integradas en el texto ($...$)
    partes = re.split(r"(\$.*?\$)", texto_contenido)

    for parte in partes:
        if not parte:
            continue
        if parte.startswith("$") and parte.endswith("$") and len(parte) > 2:
            expr = parte[1:-1].strip()
            rich_text.append(
                {"type": "equation", "equation": {"expression": expr[:1000]}}
            )
        else:
            rich_text.append({"type": "text", "text": {"content": parte[:2000]}})

    if not rich_text:
        return None

    return {"object": "block", "type": b_type, b_type: {"rich_text": rich_text}}


def _crear_apunte_notion_completo(asignatura, tema, contenido_markdown):
    db_target = NOTION_ASIGNATURAS_MAP.get(asignatura.lower()) or NOTION_DATABASE_ID
    if not db_target:
        return False
    try:
        nueva_pagina = {
            "parent": {"database_id": db_target.strip()},
            "properties": {
                "Nombre": {
                    "title": [{"text": {"content": f"Masterclass: {tema}"}}]
                }
            },
        }
        res_pag = _http_notion_request("pages", "POST", nueva_pagina)
        if not res_pag:
            return False
            
        page_id = res_pag["id"]
        bloques = []
        for linea in contenido_markdown.split("\n"):
            b = _convertir_linea_a_bloque_notion(linea)
            if b:
                bloques.append(b)
        for i in range(0, len(bloques), 100):
            _http_notion_request(f"blocks/{page_id}/children", "PATCH", {"children": bloques[i : i + 100]})
        return True
    except Exception as e:
        print(f"Error creando apunte Notion: {e}")
        return False


# --- TAREAS AUTOMÁTICAS ---
@tasks.loop(seconds=30)
async def comprobar_nuevos_eventos():
    global IDS_MEMORIA_RAM
    if not NOTION_TOKEN or not NOTION_DATABASE_ID or not CANAL_NOTIFICACIONES_ID:
        return
    try:
        canal = bot.get_channel(int(CANAL_NOTIFICACIONES_ID))
        if not canal:
            return

        endpoint = f"databases/{NOTION_DATABASE_ID.strip()}/query"
        response = await asyncio.to_thread(_http_notion_request, endpoint, "POST")
        if not response:
            return

        results = response.get("results", [])
        if not IDS_MEMORIA_RAM and results:
            IDS_MEMORIA_RAM = {page["id"] for page in results}
            await asyncio.to_thread(_guardar_ids_disco, IDS_MEMORIA_RAM)
            return

        nuevos_ids = set()
        for page in results:
            page_id = page["id"]
            if page_id not in IDS_MEMORIA_RAM:
                props = page.get("properties", {})
                nombre = _extraer_titulo_pagina(props)
                fecha = _extraer_fecha_pagina(props)

                embed = discord.Embed(
                    title="🆕 Nuevo evento en Notion",
                    description="Se ha detectado una nueva entrada en tu calendario.",
                    color=discord.Color.green(),
                )
                embed.add_field(name="📌 Evento", value=nombre, inline=False)
                embed.add_field(name="📅 Fecha", value=fecha, inline=False)
                await canal.send(embed=embed)
                nuevos_ids.add(page_id)

        if nuevos_ids:
            IDS_MEMORIA_RAM.update(nuevos_ids)
            await asyncio.to_thread(_guardar_ids_disco, IDS_MEMORIA_RAM)
            await obtener_eventos_notion(forzar_refresco=True)
    except Exception as e:
        print(f"Excepción bucle Notion: {e}")


@comprobar_nuevos_eventos.before_loop
async def antes_de_comprobar():
    await bot.wait_until_ready()


async def enviar_mensaje_largo(destino, texto):
    limite = 1900
    for i in range(0, len(texto), limite):
        await destino.send(texto[i : i + limite])


@bot.event
async def on_ready():
    global IDS_MEMORIA_RAM
    IDS_MEMORIA_RAM = await asyncio.to_thread(_cargar_ids_disco)
    print(f"Zapy activo como {bot.user} (IDs en RAM: {len(IDS_MEMORIA_RAM)})")
    if not comprobar_nuevos_eventos.is_running():
        comprobar_nuevos_eventos.start()


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if not message.content.startswith("!"):
        es_hilo = isinstance(message.channel, discord.Thread)
        es_mencion = bot.user.mentioned_in(message)
        es_dm = isinstance(message.channel, discord.DMChannel)

        if es_hilo or es_mencion or es_dm:
            if not client_gemini:
                await message.channel.send("⚠️ API de Gemini no configurada.")
                return

            texto_limpio = message.content.replace(f"<@{bot.user.id}>", "").strip()
            destino = message.channel
            if not es_hilo and not es_dm:
                try:
                    destino = await message.create_thread(
                        name=f"Planificación - {message.author.display_name}"
                    )
                except Exception as e:
                    destino = message.channel

            try:
                async with destino.typing():
                    eventos_notion = await obtener_eventos_notion()
                    ahora = datetime.datetime.now()
                    dias_semana = [
                        "Lunes",
                        "Martes",
                        "Miércoles",
                        "Jueves",
                        "Viernes",
                        "Sábado",
                        "Domingo",
                    ]
                    dia_hoy = dias_semana[ahora.weekday()]
                    fecha_hoy_str = ahora.strftime("%d/%m/%Y")

                    prompt_completo = (
                        f"HOY ES: {dia_hoy}, {fecha_hoy_str}\n\n"
                        f"EXÁMENES Y EVENTOS EN NOTION:\n{eventos_notion}\n\n"
                        f"PETICIÓN DEL ALUMNO:\n{texto_limpio}"
                    )

                    config = types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT_BASE,
                        temperature=0.3,
                        max_output_tokens=400,
                    )

                    response = await asyncio.to_thread(
                        client_gemini.models.generate_content,
                        model=GEMINI_MODEL,
                        contents=prompt_completo,
                        config=config,
                    )
                    await enviar_mensaje_largo(destino, response.text)
            except Exception as e:
                print(f"Error Gemini: {e}")
                await destino.send(f"❌ Ocurrió un error al procesar con la IA: {e}")

    await bot.process_commands(message)


# --- COMANDOS ---
@bot.command(name="comandos")
async def mostrar_comandos(ctx):
    embed = discord.Embed(title="🤖 Comandos de Zapy", color=discord.Color.blue())
    embed.add_field(name="📅 Notion", value="`!eventos` - Lista exámenes/tareas.", inline=False)
    embed.add_field(name="➕ Añadir", value="`!añadir Nombre | AAAA-MM-DD` - Guarda un evento.", inline=False)
    embed.add_field(name="🚀 Masterclass", value="`!apuntes Asignatura | Tema` - Genera apuntes.", inline=False)
    embed.add_field(name="🧹 Limpiar", value="`!clear [n]` - Borra mensajes.", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="clear")
async def limpiar_mensajes(ctx, cantidad: int = 100):
    try:
        deleted = await ctx.channel.purge(limit=cantidad)
        await ctx.send(f"🧹 {len(deleted)} mensajes borrados.", delete_after=3)
    except Exception as e:
        await ctx.send(f"❌ Error: {e}", delete_after=5)


@bot.command(name="eventos")
async def ver_eventos(ctx):
    evs = await obtener_eventos_notion(forzar_refresco=True)
    await ctx.send(f"📅 **Eventos en tu Calendar/Notion:**\n{evs}")


@bot.command(name="añadir")
async def añadir_tarea_notion(ctx, *, args: str):
    if "|" in args:
        partes = args.split("|")
        nombre = partes[0].strip()
        fecha = partes[1].strip()
    else:
        nombre = args.strip()
        fecha = None

    async with ctx.typing():
        exito = await asyncio.to_thread(_crear_tarea_notion_sync, nombre, fecha)
        if exito:
            await obtener_eventos_notion(forzar_refresco=True)
            await ctx.send(f"✅ Evento **{nombre}** creado en Notion.")
        else:
            await ctx.send("❌ Error al guardar en Notion.")


@bot.command(name="apuntes")
async def generar_apuntes_completos(ctx, *, args: str):
    if "|" not in args:
        await ctx.send("⚠️ Usa el formato: `!apuntes Asignatura | Tema`")
        return

    partes = args.split("|")
    asignatura = partes[0].strip()
    tema = partes[1].strip()

    async with ctx.typing():
        try:
            algoritmo = await obtener_algoritmo_asignatura(asignatura)
            prompt = f"Elabora la Masterclass sobre '{tema}' de '{asignatura}'."
            if algoritmo:
                prompt += f"\n\n--- METODOLOGÍA Y ALGORITMO ---\n{algoritmo}"

            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT_MASTERCLASS,
                temperature=0.3,
                max_output_tokens=3000,
            )

            response = await asyncio.to_thread(
                client_gemini.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt,
                config=config,
            )

            exito = await asyncio.to_thread(_crear_apunte_notion_completo, asignatura, tema, response.text)
            if exito:
                await ctx.send(f"🚀 **Masterclass generada:** **{tema}** ({asignatura.capitalize()}) publicada en Notion con símbolos limpios.")
            else:
                await ctx.send("❌ Error al exportar a Notion.")
        except Exception as e:
            await ctx.send(f"❌ Error al generar masterclass: {e}")


bot.run(TOKEN)

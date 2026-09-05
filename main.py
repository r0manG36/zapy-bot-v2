import os
import json
import asyncio
import datetime
import aiohttp
import discord
from discord.ext import commands, tasks
from google import genai
from google.genai import types
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
CANAL_NOTIFICACIONES_ID = os.getenv("CANAL_NOTIFICACIONES_ID")

client_gemini = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

PETICIONES_FILE = "peticiones_informe.json"
NOTION_CACHE_FILE = "notion_ids.json"

# --- MEMORIA RAM Y CACHÉ ---
IDS_MEMORIA_RAM = set()
NOTION_EVENTOS_CACHE = None
NOTION_CACHE_TIMESTAMP = None
CACHE_TTL_SEGUNDOS = 60

# --- NOTION HELPERS OPTIMIZADOS ---
def _cargar_ids_disco():
    if os.path.exists(NOTION_CACHE_FILE):
        try:
            with open(NOTION_CACHE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"Error al leer caché de disco: {e}")
    return set()

def _guardar_ids_disco(ids_set):
    try:
        with open(NOTION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(ids_set), f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error al guardar caché de disco: {e}")

def _query_notion_sync():
    if not notion or not NOTION_DATABASE_ID:
        return None
    return notion.databases.query(database_id=NOTION_DATABASE_ID)

async def obtener_eventos_notion(forzar_refresco=False):
    global NOTION_EVENTOS_CACHE, NOTION_CACHE_TIMESTAMP

    ahora = datetime.datetime.now()
    if not forzar_refresco and NOTION_EVENTOS_CACHE is not None and NOTION_CACHE_TIMESTAMP:
        if (ahora - NOTION_CACHE_TIMESTAMP).total_seconds() < CACHE_TTL_SEGUNDOS:
            return NOTION_EVENTOS_CACHE

    if not notion or not NOTION_DATABASE_ID:
        return "No se ha configurado el Token o el ID de la base de datos de Notion en el archivo .env."

    try:
        response = await asyncio.to_thread(_query_notion_sync)
        if not response:
            return "Error al conectar con la base de datos de Notion."

        results = response.get("results", [])
        if not results:
            res_texto = "No hay eventos ni exámenes registrados actualmente en Notion."
            NOTION_EVENTOS_CACHE = res_texto
            NOTION_CACHE_TIMESTAMP = ahora
            return res_texto

        eventos = []
        for page in results:
            properties = page.get("properties", {})
            title_prop = properties.get("Nombre") or properties.get("Name") or properties.get("Title") or properties.get("Tarea")
            nombre = "Sin título"
            if title_prop and title_prop.get("title") and len(title_prop["title"]) > 0:
                nombre = title_prop["title"][0].get("plain_text", "Sin título")

            date_prop = properties.get("Fecha") or properties.get("Date")
            fecha_str = "Sin fecha asignada"
            if date_prop and date_prop.get("date") and date_prop["date"]:
                fecha_str = date_prop["date"].get("start", "Sin fecha")

            eventos.append(f"- {nombre} ({fecha_str})")

        res_texto = "\n".join(eventos)
        NOTION_EVENTOS_CACHE = res_texto
        NOTION_CACHE_TIMESTAMP = ahora
        return res_texto

    except Exception as err:
        return f"Error al consultar Notion: {err}"

def _crear_tarea_notion_sync(nombre, fecha_str):
    if not notion or not NOTION_DATABASE_ID:
        return False
    try:
        nueva_pagina = {
            "parent": {"database_id": NOTION_DATABASE_ID},
            "properties": {
                "Nombre": {
                    "title": [
                        {
                            "text": {
                                "content": nombre
                            }
                        }
                    ]
                }
            }
        }
        if fecha_str:
            nueva_pagina["properties"]["Fecha"] = {
                "date": {
                    "start": fecha_str
                }
            }
        
        notion.pages.create(**nueva_pagina)
        return True
    except Exception as e:
        print(f"Error al crear tarea en Notion: {e}")
        return False

# --- TAREA AUTOMÁTICA CADA 30s OPTIMIZADA ---
@tasks.loop(seconds=30)
async def comprobar_nuevos_eventos():
    global IDS_MEMORIA_RAM

    if not notion or not NOTION_DATABASE_ID or not CANAL_NOTIFICACIONES_ID:
        return

    try:
        canal = bot.get_channel(int(CANAL_NOTIFICACIONES_ID))
        if not canal:
            return

        response = await asyncio.to_thread(_query_notion_sync)
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
                properties = page.get("properties", {})
                title_prop = properties.get("Nombre") or properties.get("Name") or properties.get("Title") or properties.get("Tarea")
                nombre = "Sin título"
                if title_prop and title_prop.get("title") and len(title_prop["title"]) > 0:
                    nombre = title_prop["title"][0].get("plain_text", "Sin título")

                date_prop = properties.get("Fecha") or properties.get("Date")
                fecha_str = "Sin fecha asignada"
                if date_prop and date_prop.get("date") and date_prop["date"]:
                    fecha_str = date_prop["date"].get("start", "Sin fecha")

                embed = discord.Embed(
                    title="🆕 Nuevo evento en Notion",
                    description="Se ha añadido un nuevo evento o examen a tu planificación.",
                    color=discord.Color.green()
                )
                embed.add_field(name="📌 Evento", value=nombre, inline=False)
                embed.add_field(name="📅 Fecha", value=fecha_str, inline=False)

                await canal.send(embed=embed)
                nuevos_ids.add(page_id)

        if nuevos_ids:
            IDS_MEMORIA_RAM.update(nuevos_ids)
            await asyncio.to_thread(_guardar_ids_disco, IDS_MEMORIA_RAM)
            await obtener_eventos_notion(forzar_refresco=True)

    except Exception as e:
        print(f"Excepción aislada en bucle Notion (resiliencia activada): {e}")

@comprobar_nuevos_eventos.before_loop
async def antes_de_comprobar():
    await bot.wait_until_ready()

SYSTEM_PROMPT = """Zapy, a partir de ahora vas a ser un tutor academico experto en todas las areas academicas con los mejores metodos de estudio basados en la ciencia y en opiniones de expertos en el tema. Tambien vas a ser un experto en la organizacion de bloques de estudio y rutinas en general, tambien los metodos que usaras seran basadas en la ciencia y en opiniones de expertos. No hagas muy largas las respuestas

Actualmente, estoy en cuarto del eso cientifico con la siguientes asignaturas: Euskera, Lengua Castellana, Ingles, Geografia e Historia, Educacion Fisica, Tutoria, Matematicas academicas, Fisica y Quimica, Tecnologia, Digitalizacion y Robotica. Todas las asignaturas se explican, se hacen los deberes, proyectos y examenes en Euskera menos Ingles y Lengua Castellana.

Mi objetivos son tener una rutina muy bien estructurada para un estudio muy bueno y con la menor cantidad de horas de estudio gracias a los mejores metodos de estudio. Asi que necesito una rutina para cada dia o semana o periodo acorde a mis necesidades. Todo esto para sacar la maxima nota en cada asignatura.

Esta es mi rutina semanal con todos los horarios exactos, mis impedimentos, mis preferencias, mis huecos libres…:


Lunes 

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: Ejemplo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:20 - 16:45 Buscar a mi hermana, 17:30 - 20
:30 entrenar, 20:30 - 21:30 volver a casa y cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Martes

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: Ejemplo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:20 - 16:45 Buscar a mi hermana, 20:00 - 21:30 Estar con Familia y Cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Miercoles

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: Ejemplo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:20 - 16:45 Buscar a mi hermana, 17:30 - 20:30 entrenar, 20:30 - 21:30 volver a casa y cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Jueves

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: Ejemplo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:20 - 16:45 Buscar a mi hermana, 19:30 - 21:45 entrenar, 22:00 - 22:30 volver a casa y cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Viernes

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: Ejemplo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:20 - 16:45 Buscar a mi hermana, las tardes del viernes no estudio
- Hora de cierre / descanso nocturno: Nunca se sabe, pero tarde


Sabado: Los sabados a la mañana/mediodia hay partido y no suelo estar hasta las 16:00

Domingo: Entre las 13:00 y 16:00 no puedo.

Quiero que me respondas diciendo en que momento estudio, con que metodo, que asignatura… Ejemplo:  A las 3:15 Tienes que estudiar mates con este metodo “x” hasta las 5:00

"""

def cargar_peticiones():
    if os.path.exists(PETICIONES_FILE):
        try:
            with open(PETICIONES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def guardar_peticiones(peticiones):
    try:
        with open(PETICIONES_FILE, "w", encoding="utf-8") as f:
            json.dump(peticiones, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error al guardar peticiones: {e}")

def limpiar_peticiones():
    if os.path.exists(PETICIONES_FILE):
        try:
            os.remove(PETICIONES_FILE)
        except Exception:
            pass

async def enviar_mensaje_largo(destino, texto):
    limite = 1900
    for i in range(0, len(texto), limite):
        await destino.send(texto[i:i + limite])

async def obtener_tiempo():
    url = "https://wttr.in/Vitoria-Gasteiz?format=%C+%t+(Min/Max:+%f)+Lluvia:+%p&M"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.text()
                    return data.strip()
    except Exception:
        pass
    return "No se pudo obtener la previsión del tiempo."

async def generar_embed_informe():
    tiempo_info = await obtener_tiempo()
    peticiones_vars = cargar_peticiones()
    eventos_notion = await obtener_eventos_notion()
    ahora = datetime.datetime.now()

    embed = discord.Embed(
        title="🌤️ Resumen Diario - Zapy",
        description=f"Informe correspondiente al {ahora.strftime('%d/%m/%Y - %H:%M')}:",
        color=discord.Color.gold()
    )
    embed.add_field(name="🌤️ Tiempo en Vitoria-Gasteiz", value=f"`{tiempo_info}`", inline=False)
    embed.add_field(name="📅 Exámenes y Tareas Pendientes (Notion)", value=f"```{eventos_notion}```", inline=False)
    embed.add_field(name="⚽ Información Deportiva", value="• Consulta los marcadores recientes y próximos partidos de tu jornada.", inline=False)
    embed.add_field(name="📰 Noticias destacadas", value="• Novedades de Inteligencia Artificial y Videojuegos en [3DJuegos](https://www.3djuegos.com) o [Xataka](https://www.xataka.com).", inline=False)

    if peticiones_vars:
        texto_vars = "\n".join([f"• {p}" for p in peticiones_vars])
        embed.add_field(name="📌 Peticiones y Notas Guardadas", value=texto_vars, inline=False)
        limpiar_peticiones()
    else:
        embed.add_field(name="📌 Peticiones Guardadas", value="*Sin peticiones variables para hoy.*", inline=False)

    return embed

@bot.event
async def on_ready():
    global IDS_MEMORIA_RAM
    IDS_MEMORIA_RAM = await asyncio.to_thread(_cargar_ids_disco)
    print(f"Zapy optimizado y conectado como {bot.user} (IDs cargados en RAM: {len(IDS_MEMORIA_RAM)}).")
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
            if client_gemini:
                try:
                    texto_limpio = message.content.replace(f"<@{bot.user.id}>", "").strip()

                    if not es_hilo and not es_dm and hasattr(message, "create_thread"):
                        destino = await message.create_thread(name=f"Planificación - {message.author.display_name}")
                    else:
                        destino = message.channel

                    async with destino.typing():
                        eventos_notion = await obtener_eventos_notion()
                        prompt_completo = f"EXÁMENES Y EVENTOS EN NOTION:\n{eventos_notion}\n\nPETICIÓN DEL ALUMNO:\n{texto_limpio}"

                        config = types.GenerateContentConfig(
                            system_instruction=SYSTEM_PROMPT,
                            temperature=0.3,
                            max_output_tokens=300
                        )

                        response = await asyncio.to_thread(
                            client_gemini.models.generate_content,
                            model="gemini-3.5-flash-lite",
                            contents=prompt_completo,
                            config=config
                        )
                        await enviar_mensaje_largo(destino, response.text)
                except Exception as e:
                    await message.channel.send(f"❌ Error al procesar la solicitud: {e}")
            else:
                await message.channel.send("⚠️ La API de Gemini no está configurada correctamente.")

    await bot.process_commands(message)

# --- COMANDOS ---
@bot.command(name="comandos")
async def mostrar_comandos(ctx):
    embed = discord.Embed(title="🤖 Panel de Comandos de Zapy", color=discord.Color.blue())
    embed.add_field(name="📌 General", value="`!comandos` - Muestra esta ayuda.", inline=False)
    embed.add_field(name="🧹 Limpieza", value="`!clear [cantidad]` - Borra mensajes del canal o hilo.", inline=False)
    embed.add_field(name="📰 Informe Diario", value="`!informe` - Genera y envía el resumen diario.", inline=False)
    embed.add_field(name="📝 Peticiones", value="`!peticion <texto>` - Añade una nota al próximo informe.", inline=False)
    embed.add_field(name="📅 Notion", value="`!eventos` - Muestra los exámenes y tareas guardados en Notion.", inline=False)
    embed.add_field(name="➕ Añadir Tarea", value="`!añadir <nombre> | <AAAA-MM-DD>` - Crea una tarea en Notion.", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="clear")
async def limpiar_mensajes(ctx, cantidad: int = None):
    limite = cantidad if cantidad is not None else 100
    try:
        deleted = await ctx.channel.purge(limit=limite)
        await ctx.send(f"🧹 Se han borrado {len(deleted)} mensajes.", delete_after=3)
    except Exception as e:
        await ctx.send(f"❌ Error al borrar mensajes: {e}", delete_after=5)

@bot.command(name="informe")
async def enviar_informe(ctx):
    embed = await generar_embed_informe()
    await ctx.send(embed=embed)

@bot.command(name="peticion")
async def agregar_peticion(ctx, *, texto: str):
    peticiones = cargar_peticiones()
    peticiones.append(texto)
    guardar_peticiones(peticiones)
    await ctx.send(f"✅ Petición guardada: *{texto}*")

@bot.command(name="eventos")
async def ver_eventos(ctx):
    evs = await obtener_eventos_notion()
    await ctx.send(f"📅 **Eventos y exámenes en tu Notion:**\n{evs}")

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
            fecha_texto = f" para el `{fecha}`" if fecha else ""
            await ctx.send(f"✅ Tarea **{nombre}** añadida correctamente a tu Notion{fecha_texto}.")
        else:
            await ctx.send("❌ Hubo un error al conectar con Notion para crear la tarea.")

bot.run(TOKEN)

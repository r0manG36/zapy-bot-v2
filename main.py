import os
import json
import datetime
import aiohttp
import discord
from discord.ext import commands
from google import genai
from google.genai import types
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

client_gemini = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

PETICIONES_FILE = "peticiones_informe.json"

# --- NOTION HELPER ---
def obtener_eventos_notion():
    if not notion or not NOTION_DATABASE_ID:
        return "No se ha configurado el Token o el ID de la base de datos de Notion en el archivo .env."
    
    try:
        response = notion.databases.query(database_id=NOTION_DATABASE_ID)
        results = response.get("results", [])
        
        if not results:
            return "No hay eventos ni exámenes registrados actualmente en Notion."
        
        eventos = []
        for page in results:
            properties = page.get("properties", {})
            
            # Obtener nombre/título
            title_prop = properties.get("Nombre") or properties.get("Name") or properties.get("Title") or properties.get("Tarea")
            nombre = "Sin título"
            if title_prop and title_prop.get("title"):
                title_list = title_prop["title"]
                if len(title_list) > 0:
                    nombre = title_list[0].get("plain_text", "Sin título")

            # Obtener fecha
            date_prop = properties.get("Fecha") or properties.get("Date")
            fecha_str = "Sin fecha asignada"
            if date_prop and date_prop.get("date"):
                date_val = date_prop["date"]
                if date_val:
                    fecha_str = date_val.get("start", "Sin fecha")

            eventos.append(f"- {nombre} ({fecha_str})")

        return "\n".join(eventos)
    except Exception as err:
        return f"Error al consultar Notion: {err}"

SYSTEM_PROMPT = """
Eres Zapy, un tutor académico experto en todas las áreas académicas con los mejores métodos de estudio basados en la ciencia (Active Recall, Spaced Repetition, Técnica Pomodoro, Blurting, Feynman). Tu objetivo es optimizar el tiempo al máximo y obtener la máxima nota estudiando la menor cantidad de horas.

CONTEXTO DEL ESTUDIANTE:
- Nivel: 4º de la ESO (Vía Científica).
- Asignaturas: Euskera, Lengua Castellana, Inglés, Geografía e Historia, Educación Física, Tutoría, Matemáticas académicas, Física y Química, Tecnología, Digitalización y Robótica.
- Idiomas: Todas las asignaturas son en Euskera, excepto Inglés y Lengua Castellana.

HORARIOS Y BLOQUEOS FIJOS DEL ESTUDIANTE:
- Lunes: Despertar 7:10. Clases 8:15-14:15. Comida/Descanso 14:30-15:30. Buscar hermana 16:20-16:45. Entrenamiento 17:30-20:30. Cena/Vuelta 20:30-21:30.
- Martes: Despertar 7:10. Clases 8:15-14:15. Comida/Descanso 14:30-15:30. Buscar hermana 16:20-16:45. Familia/Cena 20:00-21:30.
- Miércoles: Despertar 7:10. Clases 8:15-14:15. Comida/Descanso 14:30-15:30. Buscar hermana 16:20-16:45. Entrenamiento 17:30-20:30. Cena/Vuelta 20:30-21:30.
- Jueves: Despertar 7:10. Clases 8:15-14:15. Comida/Descanso 14:30-15:30. Buscar hermana 16:20-16:45. Entrenamiento 19:30-21:45. Cena/Vuelta 22:00-22:30.
- Viernes: Despertar 7:10. Clases 8:15-14:15. Comida/Descanso 14:30-15:30. Buscar hermana 16:20-16:45. Tardes de viernes NO se estudia.
- Sábado: Partido a la mañana/mediodía (ocupado hasta las 16:00).
- Domingo: Ocupado entre 13:00 y 16:00.
- Prioridad general: Garantizar entre 8 y 9 horas de sueño.

INSTRUCCIONES DE ACTUACIÓN:
1. Ten en cuenta tanto tus bloques fijos como los exámenes/tareas leídos desde Notion.
2. Si el usuario pide planificar o responde a preguntas sobre entregas/exámenes, GENERA LA RUTINA EXACTA indicando: hora de inicio y fin, asignatura, método concreto (explicado brevemente) y tarea a realizar. Sé conciso y estructurado.
"""

def cargar_peticiones():
    if os.path.exists(PETICIONES_FILE):
        with open(PETICIONES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def guardar_peticiones(peticiones):
    with open(PETICIONES_FILE, "w", encoding="utf-8") as f:
        json.dump(peticiones, f, ensure_ascii=False, indent=4)

def limpiar_peticiones():
    if os.path.exists(PETICIONES_FILE):
        os.remove(PETICIONES_FILE)

async def enviar_mensaje_largo(destino, texto):
    limite = 1900
    for i in range(0, len(texto), limite):
        await destino.send(texto[i:i + limite])

async def obtener_tiempo():
    url = "https://wttr.in/Vitoria-Gasteiz?format=%C+%t+(Min/Max:+%f)+Lluvia:+%p&M"
    try:
        async with aiohttp.ClientSession() as session:
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
    ahora = datetime.datetime.now()

    embed = discord.Embed(
        title="🌤️ Resumen Diario - Zapy",
        description=f"Informe correspondiente al {ahora.strftime('%d/%m/%Y - %H:%M')}:",
        color=discord.Color.gold()
    )
    embed.add_field(name="🌤️ Tiempo en Vitoria-Gasteiz", value=f"`{tiempo_info}`", inline=False)
    embed.add_field(name="📅 Planificador y Lectura", value="• Revisa tus entregas y exámenes pendientes.\n• Recordatorio: Avanza con la lectura diaria programada.", inline=False)
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
    print(f"Zapy conectado correctamente como {bot.user} e integrado con Notion.")

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
                        eventos_notion = obtener_eventos_notion()
                        prompt_completo = f"EXÁMENES Y EVENTOS EN NOTION:\n{eventos_notion}\n\nPETICIÓN DEL ALUMNO:\n{texto_limpio}"

                        config = types.GenerateContentConfig(
                            system_instruction=SYSTEM_PROMPT,
                            temperature=0.3,
                            max_output_tokens=1200
                        )

                        response = client_gemini.models.generate_content(
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
    evs = obtener_eventos_notion()
    await ctx.send(f"📅 **Eventos y exámenes en tu Notion:**\n{evs}")

bot.run(TOKEN)

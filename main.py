import os
import json
import datetime
import aiohttp
import discord
from discord.ext import commands, tasks
from google import genai
from dotenv import load_dotenv

# Carga de variables de entorno desde el archivo .env
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
CANAL_ID = int(os.getenv("CANAL_NOTIFICACIONES_ID", "0"))

# Inicialización de la API de Gemini
client_gemini = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

# Configuración de Intents y Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

PETICIONES_FILE = "peticiones_informe.json"

# SYSTEM PROMPT CON TU CONTEXTO COMPLETO Y RUTINA BASE
SYSTEM_PROMPT = """
Eres Zapy, un tutor académico experto en todas las áreas académicas con los mejores métodos de estudio basados en la ciencia y opiniones de expertos (Active Recall, Spaced Repetition, Técnica Pomodoro, Blurting, Feynman). Eres un experto en la organización de bloques de estudio y rutinas para optimizar el tiempo al máximo y obtener la máxima nota estudiando la menor cantidad de horas.

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
1. Si el usuario te pide planificar una semana/periodo o no te ha dado los detalles de sus exámenes/deberes pendientes, HAZLE PREGUNTAS PRIMERO para conocer sus necesidades puntuales (exámenes, entregas de proyectos, deberes, preferencia de horas).
2. Una vez que el usuario te responda con sus entregas y exámenes, GENERA LA RUTINA EXACTA indicando: hora de inicio y fin, asignatura, método de estudio específico (explicado brevemente) y qué tarea concreta realizar.
   Ejemplo: "A las 15:30 tienes que estudiar Matemáticas con Active Recall (resolución de 3 problemas sin mirar soluciones) hasta las 16:15".
"""

# --- FUNCIONES AUXILIARES ---
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

    embed.add_field(
        name="🌤️ Tiempo en Vitoria-Gasteiz",
        value=f"`{tiempo_info}`",
        inline=False
    )

    embed.add_field(
        name="📅 Planificador y Lectura",
        value="• Revisa tus entregas y exámenes pendientes.\n• Recordatorio: Avanza con la lectura diaria programada.",
        inline=False
    )

    embed.add_field(
        name="⚽ Información Deportiva",
        value="• Consulta los marcadores recientes y próximos partidos de tu jornada.",
        inline=False
    )

    embed.add_field(
        name="📰 Noticias destacadas",
        value="• Novedades de Inteligencia Artificial y Videojuegos en [3DJuegos](https://www.3djuegos.com) o [Xataka](https://www.xataka.com).",
        inline=False
    )

    if peticiones_vars:
        texto_vars = "\n".join([f"• {p}" for p in peticiones_vars])
        embed.add_field(
            name="📌 Peticiones y Notas Guardadas",
            value=texto_vars,
            inline=False
        )
        limpiar_peticiones()
    else:
        embed.add_field(
            name="📌 Peticiones Guardadas",
            value="*Sin peticiones variables para hoy.*",
            inline=False
        )

    return embed

# --- EVENTOS Y ESCUCHA DE MENSAJES ---
@bot.event
async def on_ready():
    print(f"Zapy activado con gemini-3.5-flash-lite como {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Sincronizados {len(synced)} comandos.")
    except Exception as e:
        print(f"Error al sincronizar comandos: {e}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Responder con Gemini si lo mencionan o si es DM
    if not message.content.startswith("!"):
        if bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
            if client_gemini:
                try:
                    texto_limpio = message.content.replace(f"<@{bot.user.id}>", "").strip()
                    
                    if hasattr(message, "create_thread") and not isinstance(message.channel, discord.Thread):
                        hilo = await message.create_thread(name=f"Planificación - {message.author.display_name}")
                        destino = hilo
                    else:
                        destino = message.channel

                    async with destino.typing():
                        prompt = f"{SYSTEM_PROMPT}\n\nConsulta/Mensaje del alumno: {texto_limpio}"
                        response = client_gemini.models.generate_content(
                            model="gemini-3.5-flash-lite",
                            contents=prompt
                        )
                        await destino.send(response.text)
                except Exception as e:
                    await message.channel.send(f"❌ Error al procesar la solicitud: {e}")
            else:
                await message.channel.send("⚠️ La API de Gemini no está configurada correctamente.")

    # Procesar comandos con prefijo !
    await bot.process_commands(message)

# --- COMANDOS DEL BOT ---
@bot.command(name="comandos")
async def mostrar_comandos(ctx):
    embed = discord.Embed(
        title="🤖 Panel de Comandos de Zapy",
        description="Lista completa de funciones disponibles:",
        color=discord.Color.blue()
    )
    embed.add_field(name="📌 General", value="`!comandos` - Muestra esta ayuda.", inline=False)
    embed.add_field(name="📰 Informe Diario", value="`!informe` - Genera y envía el resumen diario.", inline=False)
    embed.add_field(name="📝 Peticiones", value="`!peticion <texto>` - Añade una nota al próximo informe.", inline=False)
    await ctx.send(embed=embed)

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

bot.run(TOKEN)

import os
import json
import datetime
import aiohttp
import discord
from discord.ext import commands, tasks
from google import genai
from dotenv import load_dotenv

# Carga de variables de entorno
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
                    
                    # Si es un canal de texto (no un DM) y no estamos ya dentro de un hilo, crea uno
                    if hasattr(message, "create_thread") and not isinstance(message.channel, discord.Thread):
                        hilo = await message.create_thread(name=f"Consulta - {message.author.display_name}")
                        destino = hilo
                    else:
                        destino = message.channel

                    async with destino.typing():
                        prompt = f"Eres Zapy, un asistente de organización personal. Responde de forma concisa y útil a: {texto_limpio}"
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

import os
import json
import datetime
import aiohttp
import discord
from discord.ext import commands, tasks
from google import genai

# --- CONFIGURACIÓN E INICIALIZACIÓN ---
TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
CANAL_ID = int(os.getenv("CANAL_NOTIFICACIONES_ID", "0"))

# Inicialización de Gemini (librería google-genai)
client_gemini = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

# Configuración de Intents y Bot de Discord
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

PETICIONES_FILE = "peticiones_informe.json"

# --- FUNCIONES AUXILIARES PARA PETICIONES VARIABLES ---
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
        title="☀️ Resumen Diario - Zapy",
        description=f"Informe correspondiente al {ahora.strftime('%d/%m/%Y - %H:%M')}:",
        color=discord.Color.gold()
    )

    # 1. Previsión del Tiempo
    embed.add_field(
        name="🌤️ Tiempo en Vitoria-Gasteiz",
        value=f"`{tiempo_info}`",
        inline=False
    )

    # 2. Planificador y Hábitos
    embed.add_field(
        name="📅 Planificador y Lectura",
        value="• Revisa tus entregas y exámenes pendientes.\n• Recordatorio: Avanza con la lectura diaria programada.",
        inline=False
    )

    # 3. Información Deportiva
    embed.add_field(
        name="⚽ Fútbol (FC Barcelona / Real Sociedad)",
        value="• Consulta los marcadores recientes y próximos partidos de tu jornada.",
        inline=False
    )

    # 4. Noticias destacadas
    embed.add_field(
        name="🎮 Noticias Tech & Gaming",
        value="• Novedades de Inteligencia Artificial y Videojuegos en [3DJuegos](https://www.3djuegos.com) o [Xataka](https://www.xataka.com).",
        inline=False
    )

    # 5. Peticiones Variables
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

# --- COMANDOS DEL BOT ---

@bot.command(name="comandos")
async def mostrar_comandos(ctx):
    embed = discord.Embed(
        title="🤖 Panel de Comandos de Zapy",
        description="Lista completa de funciones disponibles:",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="📌 General",
        value="`!comandos` - Muestra esta ayuda.",
        inline=False
    )
    embed.add_field(
        name="📰 Informe Diario",
        value=(
            "`!informe` (o `!resumen`) - Genera el informe diario al instante.\n"
            "`!informe+ <texto>` - Añade una nota puntual para el siguiente informe.\n"
            "`!informe- <número>` - Elimina una nota puntual guardada."
        ),
        inline=False
    )
    embed.set_footer(text="Zapy Bot • Asistente Local")
    await ctx.send(embed=embed)

# Comando para AÑADIR nota puntual
@bot.command(name="informe+")
async def agregar_nota(ctx, *, peticion: str):
    peticiones = cargar_peticiones()
    peticiones.append(peticion)
    guardar_peticiones(peticiones)
    await ctx.send(f"✅ Anotado para el próximo informe: *\"{peticion}\"*")

# Comando para BORRAR nota puntual
@bot.command(name="informe-")
async def eliminar_nota(ctx, numero: int):
    peticiones = cargar_peticiones()
    if 1 <= numero <= len(peticiones):
        eliminado = peticiones.pop(numero - 1)
        guardar_peticiones(peticiones)
        await ctx.send(f"🗑️ Se ha eliminado: *\"{eliminado}\"*")
    else:
        await ctx.send("⚠️ Número de nota no válido.")

@bot.command(name="informe", aliases=["resumen"])
async def enviar_informe_manual(ctx):
    embed = await generar_embed_informe()
    await ctx.send(embed=embed)

# --- TAREA PROGRAMADA (15:00) ---

@tasks.loop(minutes=1)
async def informe_diario_task():
    ahora = datetime.datetime.now()
    if ahora.hour == 15 and ahora.minute == 0:
        canal = bot.get_channel(CANAL_ID)
        if canal:
            embed = await generar_embed_informe()
            await canal.send(embed=embed)

@informe_diario_task.before_loop
async def antes_de_iniciar_tarea():
    await bot.wait_until_ready()

# --- EVENTO ON_READY ---

@bot.event
async def on_ready():
    print(f"Zapy activo como {bot.user}")
    if not informe_diario_task.is_running():
        informe_diario_task.start()

# --- ARRANQUE ---
if __name__ == "__main__":
    bot.run(TOKEN)

import os
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands

# 1. Servidor Web Flask para Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot activo y funcionando 24/7"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# Iniciar servidor web en segundo plano
keep_alive()

# 2. Configuración del Bot de Discord
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'Bot conectado exitosamente como {bot.user}')

@bot.event
async def on_message(message):
    # Evitar que el bot se responda a sí mismo
    if message.author == bot.user:
        return

    # Verificar si el bot ha sido mencionado
    if bot.user.mentioned_in(message):
        async with message.channel.typing():
            # Crea un hilo y responde cuando lo mencionan
            thread = await message.create_thread(name=f"Consulta de {message.author.name}")
            await thread.send(f"¡Hola {message.author.mention}! He recibido tu mensaje. ¿En qué te puedo ayudar?")

    await bot.process_commands(message)

# Cargar el token desde las variables de entorno de Render
TOKEN = os.environ.get("DISCORD_TOKEN")

if TOKEN:
    bot.run(TOKEN)
else:
    print("Error: No se encontró la variable DISCORD_TOKEN.")

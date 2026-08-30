import os
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands
from google import genai

# 1. Servidor Web Flask para Render (Keep-Alive)
app = Flask(__name__)


@app.route("/")
def home():
  return "Bot activo y funcionando 24/7"


def run_flask():
  port = int(os.environ.get("PORT", 8080))
  app.run(host="0.0.0.0", port=port)


def keep_alive():
  t = Thread(target=run_flask)
  t.daemon = True
  t.start()


keep_alive()

# 2. Configurar la API de Gemini
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

# Diccionario para almacenar la memoria/historial de chat por cada hilo
historiales_chat = {}

# 3. Configuración del Bot de Discord
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
  print(f"Bot conectado exitosamente como {bot.user}")


@bot.event
async def on_message(message):
  if message.author == bot.user:
    return

  es_hilo = isinstance(message.channel, discord.Thread)
  es_hilo_del_bot = es_hilo and message.channel.owner == bot.user
  fue_mencionado = bot.user.mentioned_in(message)

  # Responder si lo mencionan fuera de un hilo O si están hablando dentro del hilo creado por el bot
  if fue_mencionado or es_hilo_del_bot:
    async with message.channel.typing():
      try:
        # Limpiar la mención del mensaje
        prompt = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not prompt:
          prompt = "Hola"

        # CASO 1: Mención en un canal normal -> Se crea EL HILO ÚNICO
        if fue_mencionado and not es_hilo:
          thread = await message.create_thread(
              name=f"Conversación con {message.author.name}"
          )

          # Iniciar sesión de chat con memoria para este nuevo hilo
          chat_session = client.chats.create(model="gemini-3.6-flash")
          historiales_chat[thread.id] = chat_session

          # Enviar la primera respuesta con Gemini dentro del hilo recién creado
          response = chat_session.send_message(prompt)
          await thread.send(response.text)

        # CASO 2: Mensaje enviado dentro del hilo existente -> Mantiene el contexto
        elif es_hilo_del_bot:
          thread_id = message.channel.id

          # Si por alguna razón el hilo no está cargado en memoria, se crea una sesión nueva
          if thread_id not in historiales_chat:
            historiales_chat[thread_id] = client.chats.create(
                model="gemini-3.6-flash"
            )

          chat_session = historiales_chat[thread_id]

          # Responder manteniendo toda la memoria previa del hilo
          response = chat_session.send_message(prompt)
          await message.reply(response.text)

      except Exception as e:
        print(f"Error en la interacción: {e}")
        await message.reply(
            f"Error al procesar la solicitud: `{str(e)[:100]}`"
        )

  await bot.process_commands(message)


# Cargar el token de Discord
TOKEN = os.environ.get("DISCORD_TOKEN")

if TOKEN:
  bot.run(TOKEN)
else:
  print("Error: No se encontró la variable DISCORD_TOKEN.")

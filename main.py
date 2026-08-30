import os
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands
from google import genai
from google.genai import types

# 1. Servidor Web Flask para Render (Keep-Alive 24/7)
app = Flask(__name__)


@app.route("/")
def home():
  return "Bot activo 24/7"


def run_flask():
  port = int(os.environ.get("PORT", 8080))
  app.run(host="0.0.0.0", port=port)


def keep_alive():
  t = Thread(target=run_flask)
  t.daemon = True
  t.start()


keep_alive()

# 2. Configurar cliente de Gemini
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

# Memoria de chat por hilo
historiales_chat = {}

# 3. Configuración del Bot de Discord
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def obtener_o_crear_chat(thread_id):
  if thread_id in historiales_chat:
    return historiales_chat[thread_id]

  # Activa la herramienta de búsqueda en tiempo real
  configuracion = types.GenerateContentConfig(
      tools=[types.Tool(google_search=types.GoogleSearch())]
  )

  chat = client.chats.create(
      model="gemini-3.5-flash-lite", config=configuracion
  )
  historiales_chat[thread_id] = chat
  return chat


@bot.event
async def on_ready():
  print(f"Bot listo con búsqueda web como {bot.user}")


@bot.event
async def on_message(message):
  if message.author == bot.user:
    return

  es_hilo = isinstance(message.channel, discord.Thread)
  es_hilo_del_bot = es_hilo and message.channel.owner == bot.user
  fue_mencionado = bot.user.mentioned_in(message)

  if fue_mencionado or es_hilo_del_bot:
    async with message.channel.typing():
      try:
        prompt = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not prompt:
          prompt = "Hola"

        if fue_mencionado and not es_hilo:
          thread = await message.create_thread(
              name=f"Conversación con {message.author.name}"
          )
          chat_session = obtener_o_crear_chat(thread.id)
          response = chat_session.send_message(prompt)
          await thread.send(response.text)

        elif es_hilo_del_bot:
          chat_session = obtener_o_crear_chat(message.channel.id)
          response = chat_session.send_message(prompt)
          await message.reply(response.text)

      except Exception as e:
        print(f"Error en interacción: {e}")
        await message.reply(f"Error al procesar la respuesta: `{str(e)[:100]}`")

  await bot.process_commands(message)


TOKEN = os.environ.get("DISCORD_TOKEN")
if TOKEN:
  bot.run(TOKEN)

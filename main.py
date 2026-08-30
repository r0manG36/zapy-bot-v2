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

# 2. Configurar las 2 API Keys de Gemini
API_KEYS = [
    key
    for key in [
        os.environ.get("GEMINI_API_KEY"),
        os.environ.get("GEMINI_API_KEY_2"),
    ]
    if key
]

# Crear un cliente por cada API Key disponible
clientes_gemini = [genai.Client(api_key=k) for k in API_KEYS]

# Historial de chats por hilo de Discord
historiales_chat = {}

# 3. Configuración del Bot de Discord
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def crear_sesion_chat():
  """Intenta crear el chat probando todas las API Keys y modelos disponibles."""
  for client in clientes_gemini:
    for modelo in ["gemini-2.5-flash", "gemini-1.5-flash"]:
      try:
        return client.chats.create(model=modelo)
      except Exception as e:
        print(f"Falló cliente/modelo ({modelo}): {e}")
        continue

  raise Exception(
      "No se pudo conectar con ninguna de las API Keys o modelos de Gemini."
  )


def obtener_o_crear_chat(thread_id):
  if thread_id in historiales_chat:
    return historiales_chat[thread_id]

  chat = crear_sesion_chat()
  historiales_chat[thread_id] = chat
  return chat


def enviar_mensaje_hilo(thread_id, prompt):
  try:
    chat_session = obtener_o_crear_chat(thread_id)
    return chat_session.send_message(prompt)
  except Exception as e:
    print(f"Error en sesión activa ({e}). Intentando recrear con respaldo...")
    if thread_id in historiales_chat:
      del historiales_chat[thread_id]
    chat_session = obtener_o_crear_chat(thread_id)
    return chat_session.send_message(prompt)


@bot.event
async def on_ready():
  print(
      f"Bot conectado exitosamente como {bot.user}. Claves cargadas:"
      f" {len(API_KEYS)}"
  )


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
        # Limpiar mención del mensaje
        prompt = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not prompt:
          prompt = "Hola"

        # Caso 1: Mención en canal normal -> Crear hilo único inicial
        if fue_mencionado and not es_hilo:
          thread = await message.create_thread(
              name=f"Conversación con {message.author.name}"
          )
          response = enviar_mensaje_hilo(thread.id, prompt)
          await thread.send(response.text)

        # Caso 2: Mensaje dentro del hilo -> Mantiene contexto del chat
        elif es_hilo_del_bot:
          response = enviar_mensaje_hilo(message.channel.id, prompt)
          await message.reply(response.text)

      except Exception as e:
        print(f"Error detallado: {e}")
        await message.reply(f"Error de conexión con Gemini: `{str(e)[:120]}`")

  await bot.process_commands(message)


# Token de Discord
TOKEN = os.environ.get("DISCORD_TOKEN")

if TOKEN:
  bot.run(TOKEN)
else:
  print("Error: No se encontró la variable DISCORD_TOKEN.")

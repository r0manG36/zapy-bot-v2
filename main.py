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

# Diccionario para almacenar el historial de chat por cada hilo
historiales_chat = {}

# Lista de modelos por orden de preferencia
MODELOS_DISPONIBLES = ["gemini-2.5-flash", "gemini-1.5-flash"]

# 3. Configuración del Bot de Discord
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def obtener_o_crear_chat(thread_id):
  """Crea una sesión de chat intentando los modelos en orden si alguno da 503."""
  if thread_id in historiales_chat:
    return historiales_chat[thread_id]

  for modelo in MODELOS_DISPONIBLES:
    try:
      chat = client.chats.create(model=modelo)
      historiales_chat[thread_id] = chat
      return chat
    except Exception as e:
      print(f"Modelo {modelo} no disponible: {e}")
      continue

  raise Exception("Ningún modelo de Gemini se encuentra disponible ahora mismo.")


def enviar_mensaje_con_fallback(chat_session, prompt):
  """Envía el mensaje al chat activo; si falla por saturación, reintenta con el modelo secundario."""
  try:
    return chat_session.send_message(prompt)
  except Exception as e:
    if "503" in str(e) or "UNAVAILABLE" in str(e):
      print(
          "El modelo actual está saturado (503). Intentando cambiar de"
          " modelo..."
      )
      # Crear nuevo chat de respaldo con un modelo alternativo
      for modelo in MODELOS_DISPONIBLES:
        try:
          nuevo_chat = client.chats.create(model=modelo)
          return nuevo_chat.send_message(prompt)
        except Exception:
          continue
    raise e


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

  if fue_mencionado or es_hilo_del_bot:
    async with message.channel.typing():
      try:
        prompt = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not prompt:
          prompt = "Hola"

        # CASO 1: Mención fuera de un hilo -> Crear hilo e iniciar conversación
        if fue_mencionado and not es_hilo:
          thread = await message.create_thread(
              name=f"Conversación con {message.author.name}"
          )
          chat_session = obtener_o_crear_chat(thread.id)
          response = enviar_mensaje_con_fallback(chat_session, prompt)
          await thread.send(response.text)

        # CASO 2: Continuación de la conversación dentro del hilo
        elif es_hilo_del_bot:
          chat_session = obtener_o_crear_chat(message.channel.id)
          response = enviar_mensaje_con_fallback(chat_session, prompt)
          await message.reply(response.text)

      except Exception as e:
        print(f"Error en la interacción: {e}")
        await message.reply(
            f"El servicio está muy concurrido en este momento. Por favor,"
            f" reintenta en unos segundos. `{str(e)[:80]}`"
        )

  await bot.process_commands(message)


TOKEN = os.environ.get("DISCORD_TOKEN")

if TOKEN:
  bot.run(TOKEN)
else:
  print("Error: No se encontró la variable DISCORD_TOKEN.")

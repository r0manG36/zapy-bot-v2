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

# 2. Configurar el cliente de Gemini
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

# Diccionario para mantener la memoria/historial en cada hilo
historiales_chat = {}

# Lista de modelos oficiales válidos
MODELOS_DISPONIBLES = ["gemini-1.5-flash", "gemini-1.5-pro"]

# 3. Configuración del Bot de Discord
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def obtener_o_crear_chat(thread_id):
  """Obtiene el chat existente o crea uno nuevo usando un modelo válido."""
  if thread_id in historiales_chat:
    return historiales_chat[thread_id]

  for modelo in MODELOS_DISPONIBLES:
    try:
      chat = client.chats.create(model=modelo)
      historiales_chat[thread_id] = chat
      return chat
    except Exception as e:
      print(f"No se pudo inicializar con {modelo}: {e}")
      continue

  raise Exception("No se pudo conectar con los modelos de Gemini.")


def enviar_mensaje_con_fallback(thread_id, prompt):
  """Envía el mensaje al chat activo y gestiona reconexiones en caso de error de red."""
  chat_session = obtener_o_crear_chat(thread_id)
  try:
    return chat_session.send_message(prompt)
  except Exception as e:
    print(f"Error al enviar mensaje, reintentando con sesión nueva: {e}")
    # Si falla la sesión activa, forzamos la creación de una nueva
    if thread_id in historiales_chat:
      del historiales_chat[thread_id]
    nuevo_chat = obtener_o_crear_chat(thread_id)
    return nuevo_chat.send_message(prompt)


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
        # Limpiar la mención del texto del mensaje
        prompt = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not prompt:
          prompt = "Hola"

        # CASO 1: Mención en canal normal -> Crear el hilo inicial
        if fue_mencionado and not es_hilo:
          thread = await message.create_thread(
              name=f"Conversación con {message.author.name}"
          )
          response = enviar_mensaje_con_fallback(thread.id, prompt)
          await thread.send(response.text)

        # CASO 2: Mensajes dentro del hilo existente -> Conservar la memoria
        elif es_hilo_del_bot:
          response = enviar_mensaje_con_fallback(message.channel.id, prompt)
          await message.reply(response.text)

      except Exception as e:
        print(f"Error en la interacción: {e}")
        await message.reply(
            f"Error al procesar la respuesta: `{str(e)[:100]}`"
        )

  await bot.process_commands(message)


# Cargar el token de Discord
TOKEN = os.environ.get("DISCORD_TOKEN")

if TOKEN:
  bot.run(TOKEN)
else:
  print("Error: No se encontró la variable DISCORD_TOKEN.")

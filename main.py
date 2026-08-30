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
  return "Bot MVP Activo 24/7"


def run_flask():
  port = int(os.environ.get("PORT", 8080))
  app.run(host="0.0.0.0", port=port)


def keep_alive():
  t = Thread(target=run_flask)
  t.daemon = True
  t.start()


keep_alive()

# 2. Cliente de Gemini
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

  configuracion = types.GenerateContentConfig(
      system_instruction=(
          "Eres un asistente personal de productividad amigable y eficiente."
          " Tu objetivo principal es ayudar al usuario a organizar su día y"
          " rutina diaria. Da respuestas estructuradas usando listas con"
          " viñetas o bloques de hora claros. Sé breve, motivador y directo."
      )
  )

  chat = client.chats.create(
      model="gemini-3.5-flash-lite", config=configuracion
  )
  historiales_chat[thread_id] = chat
  return chat


def generar_titulo_hilo(prompt):
  """Genera un título de 3 palabras máximo para el hilo."""
  try:
    respuesta = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=(
            f"Resume el siguiente texto en 3 palabras como máximo para usarlo"
            f" como título de un hilo de Discord. No uses comillas ni puntos:"
            f" '{prompt}'"
        ),
    )
    titulo = respuesta.text.strip().replace('"', "")
    # Cortar a máximo 3 palabras por seguridad
    palabras = titulo.split()[:3]
    return " ".join(palabras) if palabras else "Nueva rutina"
  except Exception:
    return "Nueva rutina"


async def enviar_mensaje_largo(destino, texto):
  limite = 1900
  for i in range(0, len(texto), limite):
    await destino.send(texto[i : i + limite])


@bot.event
async def on_ready():
  print(f"Bot MVP listo y conectado como {bot.user}")


@bot.event
async def on_message(message):
  if message.author == bot.user:
    return

  es_hilo = isinstance(message.channel, discord.Thread)
  es_hilo_del_bot = es_hilo and message.channel.owner == bot.user
  fue_mencionado = bot.user.mentioned_in(message)

  if (fue_mencionado and not es_hilo) or es_hilo_del_bot:
    async with message.channel.typing():
      try:
        prompt = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not prompt:
          prompt = "Hola, ayúdame a organizar mi día."

        # Caso 1: Mención fuera de un hilo -> Generar título corto y crear el hilo
        if fue_mencionado and not es_hilo:
          titulo_hilo = generar_titulo_hilo(prompt)
          thread = await message.create_thread(name=titulo_hilo)

          chat_session = obtener_o_crear_chat(thread.id)
          response = chat_session.send_message(prompt)
          await enviar_mensaje_largo(thread, response.text)

        # Caso 2: Escribir dentro del hilo existente
        elif es_hilo_del_bot:
          chat_session = obtener_o_crear_chat(message.channel.id)
          response = chat_session.send_message(prompt)
          await enviar_mensaje_largo(message.channel, response.text)

      except Exception as e:
        error_str = str(e)
        print(f"Error en interacción: {error_str}")

        if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
          await message.reply(
              "⏳ **Límite temporal alcanzado:** He recibido muchas solicitudes"
              " seguidas. Espera unos 30-60 segundos e inténtalo de nuevo."
          )
        else:
          await message.reply(
              f"⚠️ Ocurrió un error al responder: `{error_str[:100]}`"
          )

  await bot.process_commands(message)


TOKEN = os.environ.get("DISCORD_TOKEN")
if TOKEN:
  bot.run(TOKEN)

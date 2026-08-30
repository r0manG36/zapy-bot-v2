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

# 3. Configuración del Bot de Discord
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
  print(f"Bot conectado exitosamente como {bot.user}")


@bot.event
async def on_message(message):
  # Evitar que el bot se responda a sí mismo
  if message.author == bot.user:
    return

  # Detectar si le hablan por mención o si están conversando dentro de un hilo creado por el bot
  es_hilo_del_bot = (
      isinstance(message.channel, discord.Thread)
      and message.channel.owner == bot.user
  )
  fue_mencionado = bot.user.mentioned_in(message)

  if fue_mencionado or es_hilo_del_bot:
    async with message.channel.typing():
      try:
        # Limpiar la mención del texto para pasárselo limpio a Gemini
        contenido_prompt = message.content.replace(
            f"<@{bot.user.id}>", ""
        ).strip()
        if not contenido_prompt:
          contenido_prompt = "Hola"

        # Generar respuesta usando Gemini
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contenido_prompt,
        )
        texto_respuesta = response.text

        # Si le hablan en un canal principal, crea una nueva publicación (hilo/foro)
        if fue_mencionado and not isinstance(message.channel, discord.Thread):
          try:
            thread = await message.create_thread(
                name=f"Publicación de {message.author.name}"
            )
            await thread.send(texto_respuesta)
          except Exception:
            await message.reply(texto_respuesta)
        else:
          # Si ya estamos dentro de la publicación, responde con normalidad
          await message.reply(texto_respuesta)

      except Exception as e:
        print(f"Error al generar respuesta con Gemini: {e}")
        await message.reply(
            "Lo siento, ha ocurrido un error procesando tu consulta con Gemini."
        )

  await bot.process_commands(message)


# Cargar el token de Discord desde las variables de entorno
TOKEN = os.environ.get("DISCORD_TOKEN")

if TOKEN:
  bot.run(TOKEN)
else:
  print("Error: No se encontró la variable DISCORD_TOKEN.")

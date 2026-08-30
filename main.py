import os
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands
from google import genai

# 1. Servidor Web Flask para Render (Keep-Alive)
app = Flask(__name__)


@app.route('/')
def home():
  return 'Bot activo y funcionando 24/7'


def run_flask():
  port = int(os.environ.get('PORT', 8080))
  app.run(host='0.0.0.0', port=port)


def keep_alive():
  t = Thread(target=run_flask)
  t.daemon = True
  t.start()


keep_alive()

# 2. Configurar el cliente de Gemini
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY)

# Memoria de historial de chat por cada hilo
historiales_chat = {}

# Lista ordenada de preferencia de modelos estándar compatibles
MODELOS_PREFERIDOS = [
    'gemini-2.5-flash',
    'gemini-1.5-flash',
    'gemini-2.0-flash',
]

# 3. Configuración del Bot de Discord
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)


def obtener_modelo_valido():
  """Prueba la lista de modelos preferidos para encontrar uno que funcione correctamente."""
  for modelo in MODELOS_PREFERIDOS:
    try:
      # Probar si el modelo responde a una creación rápida de chat
      client.chats.create(model=modelo)
      return modelo
    except Exception as e:
      print(f'Modelo {modelo} no disponible: {e}')
      continue
  # Fallback a gemini-1.5-flash si no hay respuesta de la lista
  return 'gemini-1.5-flash'


# Detectar modelo activo al iniciar
MODELO_ACTIVO = None


def obtener_o_crear_chat(thread_id):
  """Obtiene el chat existente o crea uno nuevo con el modelo validado."""
  global MODELO_ACTIVO
  if thread_id in historiales_chat:
    return historiales_chat[thread_id]

  if not MODELO_ACTIVO:
    MODELO_ACTIVO = obtener_modelo_valido()

  chat = client.chats.create(model=MODELO_ACTIVO)
  historiales_chat[thread_id] = chat
  return chat


def enviar_mensaje_con_fallback(thread_id, prompt):
  """Envía el mensaje al chat activo o reinicia si la sesión o modelo fallan."""
  global MODELO_ACTIVO
  try:
    chat_session = obtener_o_crear_chat(thread_id)
    return chat_session.send_message(prompt)
  except Exception as e:
    print(f'Error en sesión activa, buscando modelo alternativo: {e}')
    if thread_id in historiales_chat:
      del historiales_chat[thread_id]

    MODELO_ACTIVO = obtener_modelo_valido()
    chat_session = client.chats.create(model=MODELO_ACTIVO)
    historiales_chat[thread_id] = chat_session
    return chat_session.send_message(prompt)


@bot.event
async def on_ready():
  global MODELO_ACTIVO
  MODELO_ACTIVO = obtener_modelo_valido()
  print(
      f'Bot conectado como {bot.user}. Usando modelo de Gemini:'
      f' {MODELO_ACTIVO}'
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
        # Limpiar la mención del texto del mensaje
        prompt = message.content.replace(f'<@{bot.user.id}>', '').strip()
        if not prompt:
          prompt = 'Hola'

        # CASO 1: Mención fuera del hilo -> Crea el hilo único inicial
        if fue_mencionado and not es_hilo:
          thread = await message.create_thread(
              name=f'Conversación con {message.author.name}'
          )
          response = enviar_mensaje_con_fallback(thread.id, prompt)
          await thread.send(response.text)

        # CASO 2: Mensaje dentro del hilo del bot -> Continúa la conversación con historial
        elif es_hilo_del_bot:
          response = enviar_mensaje_con_fallback(message.channel.id, prompt)
          await message.reply(response.text)

      except Exception as e:
        print(f'Error en la interacción: {e}')
        await message.reply(
            f'Error al procesar la respuesta: `{str(e)[:100]}`'
        )

  await bot.process_commands(message)


# Cargar el token de Discord
TOKEN = os.environ.get('DISCORD_TOKEN')

if TOKEN:
  bot.run(TOKEN)
else:
  print('Error: No se encontró la variable DISCORD_TOKEN.')

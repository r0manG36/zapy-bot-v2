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

# EXCLUSIVAMENTE MODELOS 3.6 FLASH Y 3.5 FLASH LITE
MODELOS_DISPONIBLES = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

# Memoria por hilo y memoria global de exámenes
historiales_chat = {}
memoria_global = {
    "examenes_y_entregas": [],
}

# 3. Configuración del Bot de Discord
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

SYSTEM_PROMPT_BASE = """
Eres Zapy, el asistente personal de productividad del usuario. Conoces su rutina exacta de memoria:

- **Despertar diario:** 7:10 AM.
- **Clases:** Lunes a Viernes de 8:15 a 14:15.
- **Comida y descanso:** 14:30 a 15:30 (L-V).
- **Buscar a su hermana:** Lunes a Viernes de 16:20 a 16:45.

**Compromisos por día:**
- **Lunes:** Entreno 17:30-20:30. Cena y vuelta 20:30-21:30. Hueco de estudio: 15:30-16:20 y 16:45-17:30.
- **Martes:** Tarde muy libre. Estar con familia/Cena 20:00-21:30. Hueco de estudio principal: 15:30-16:20 y 16:45-20:00.
- **Miércoles:** Entreno duro 17:30-20:30. Cena y vuelta 20:30-21:30. Hueco de estudio: 15:30-16:20 y 16:45-17:30.
- **Jueves:** Entreno 19:30-21:45. Cena y vuelta 22:00-22:30. Hueco de estudio: 15:30-16:20 y 16:45-19:30.
- **Viernes:** NO ESTUDIA por la tarde. Tiempo libre total.
- **Sábado:** Ocupado con partidos hasta las 16:00. Tarde disponible para repasar o descansar.
- **Domingo:** Ocupado de 13:00 a 16:00. Resto disponible para planificar la semana o estudiar.

**Tus Reglas de Organización:**
1. Mantén la prioridad absoluta de garantizar entre 8 y 9 horas de sueño (irse a la cama entre las 22:10 y 23:10 aprox).
2. Cuando el usuario te pida organizar su día o meter tareas, encájalas ÚNICAMENTE en sus huecos libres según el día de la semana.
3. Responde siempre de forma clara, motivadora, directa y estructurada con bloques de horas o viñetas.
"""


def construir_system_prompt():
  ex_str = (
      "\n".join(f"- {e}" for e in memoria_global["examenes_y_entregas"])
      if memoria_global["examenes_y_entregas"]
      else "Ninguno registrado aún."
  )
  return (
      f"{SYSTEM_PROMPT_BASE}\n\n"
      "**EXÁMENES Y ENTREGAS REGISTRADOS EN MEMORIA GLOBAL (Acuérdate de"
      " estos datos en cualquier hilo):**\n"
      f"{ex_str}\n\n"
      "Si el usuario te menciona un nuevo examen, entrega o fecha importante,"
      " responde normalmente confirmando que lo has anotado."
  )


def obtener_o_crear_chat(thread_id):
  if thread_id in historiales_chat:
    return historiales_chat[thread_id]

  configuracion = types.GenerateContentConfig(
      system_instruction=construir_system_prompt()
  )

  for modelo in MODELOS_DISPONIBLES:
    try:
      chat = client.chats.create(model=modelo, config=configuracion)
      historiales_chat[thread_id] = chat
      return chat
    except Exception:
      continue

  # Si falla el principal, intenta forzar con 3.5 Flash Lite
  chat = client.chats.create(
      model=MODELOS_DISPONIBLES[0], config=configuracion
  )
  historiales_chat[thread_id] = chat
  return chat


def enviar_mensaje_con_respaldo(chat_session, prompt, thread_id):
  for modelo in MODELOS_DISPONIBLES:
    try:
      return chat_session.send_message(prompt)
    except Exception as e:
      err_msg = str(e)
      if (
          "503" in err_msg
          or "429" in err_msg
          or "UNAVAILABLE" in err_msg
          or "404" in err_msg
      ):
        print(f"Cambiando a modelo de respaldo: {modelo}...")
        try:
          configuracion = types.GenerateContentConfig(
              system_instruction=construir_system_prompt()
          )
          nueva_sesion = client.chats.create(
              model=modelo, config=configuracion
          )
          historiales_chat[thread_id] = nueva_sesion
          return nueva_sesion.send_message(prompt)
        except Exception:
          continue
      else:
        raise e
  raise Exception(
      "Los modelos Gemini 3.6 Flash y 3.5 Flash Lite están experimentando"
      " alta carga."
  )


def actualizar_memoria_extraer_examenes(texto_usuario):
  palabras_clave = [
      "examen",
      "examenes",
      "entrega",
      "prueba",
      "control",
      "tengo que entregar",
  ]
  if any(clave in texto_usuario.lower() for clave in palabras_clave):
    if texto_usuario not in memoria_global["examenes_y_entregas"]:
      memoria_global["examenes_y_entregas"].append(texto_usuario)
      historiales_chat.clear()


def generar_titulo_hilo(prompt):
  for modelo in MODELOS_DISPONIBLES:
    try:
      respuesta = client.models.generate_content(
          model=modelo,
          contents=(
              f"Resume el siguiente texto en 3 palabras como máximo para el"
              f" título de un hilo de Discord. Sin comillas: '{prompt}'"
          ),
      )
      titulo = respuesta.text.strip().replace('"', "")
      palabras = titulo.split()[:3]
      return " ".join(palabras) if palabras else "Plan del día"
    except Exception:
      continue
  return "Plan del día"


async def enviar_mensaje_largo(destino, texto):
  limite = 1900
  for i in range(0, len(texto), limite):
    await destino.send(texto[i : i + limite])


@bot.event
async def on_ready():
  print(
      f"Zapy activado operando exclusivamente con Gemini 3.6 Flash y 3.5 Flash"
      f" Lite como {bot.user}"
  )


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
          prompt = "Organízame el día de hoy"

        actualizar_memoria_extraer_examenes(prompt)

        if fue_mencionado and not es_hilo:
          titulo_hilo = generar_titulo_hilo(prompt)
          thread = await message.create_thread(name=titulo_hilo)

          chat_session = obtener_o_crear_chat(thread.id)
          response = enviar_mensaje_con_respaldo(
              chat_session, prompt, thread.id
          )
          await enviar_mensaje_largo(thread, response.text)

        elif es_hilo_del_bot:
          chat_session = obtener_o_crear_chat(message.channel.id)
          response = enviar_mensaje_con_respaldo(
              chat_session, prompt, message.channel.id
          )
          await enviar_mensaje_largo(message.channel, response.text)

      except Exception as e:
        error_str = str(e)
        if "503" in error_str or "UNAVAILABLE" in error_str:
          await message.reply(
              "⚙️ Alta demanda temporal en los servidores. Reintenta en unos"
              " segundos."
          )
        elif "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
          await message.reply(
              "⏳ **Límite alcanzado:** Espera unos 30 segundos."
          )
        else:
          await message.reply(
              f"⚠️ Ocurrió un error al responder: `{error_str[:100]}`"
          )

  await bot.process_commands(message)


TOKEN = os.environ.get("DISCORD_TOKEN")
if TOKEN:
  bot.run(TOKEN)

import asyncio
from datetime import datetime, time
import io
import os
from threading import Thread
import zoneinfo

from flask import Flask
import discord
from discord.ext import commands, tasks
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

MODELO_UNICO = "gemini-3.5-flash-lite"
CANAL_NOTIFICACIONES_ID = int(os.environ.get("CANAL_NOTIFICACIONES_ID", 0))

historiales_chat = {}
memoria_global = {"examenes_y_entregas": []}

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
4. Si el usuario te envía un audio o nota de voz, escucha con atención lo que pide, extrae la información relevante o tareas que mencione y responde en texto de forma organizada.
"""


def construir_system_prompt():
  ex_str = (
      "\n".join(f"- {e}" for e in memoria_global["examenes_y_entregas"])
      if memoria_global["examenes_y_entregas"]
      else "Ninguno registrado aún."
  )
  return (
      f"{SYSTEM_PROMPT_BASE}\n\n"
      "**EXÁMENES Y ENTREGAS REGISTRADOS EN MEMORIA GLOBAL:**\n"
      f"{ex_str}\n"
  )


def obtener_o_crear_chat(thread_id):
  if thread_id in historiales_chat:
    return historiales_chat[thread_id]

  configuracion = types.GenerateContentConfig(
      system_instruction=construir_system_prompt(), max_output_tokens=1000
  )
  chat = client.chats.create(model=MODELO_UNICO, config=configuracion)
  historiales_chat[thread_id] = chat
  return chat


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


def generar_titulo_hilo_optimizado(prompt):
  palabras = prompt.strip().split()
  # Si el mensaje es corto, creamos el título al instante sin llamar a Gemini (Ahorra ~1.5 segundos)
  if len(palabras) <= 3 and prompt != "Organízame el día de hoy":
    return prompt[:30]

  try:
    temp_chat = client.chats.create(model=MODELO_UNICO)
    respuesta = temp_chat.send_message(
        f"Resume el siguiente texto en 3 palabras como máximo para el título de"
        f" un hilo de Discord. Sin comillas: '{prompt}'"
    )
    titulo = respuesta.text.strip().replace('"', "")
    palabras_res = titulo.split()[:3]
    return " ".join(palabras_res) if palabras_res else "Plan del día"
  except Exception:
    return "Plan del día"


# COMANDOS DE GESTIÓN DE MEMORIA
@bot.command(name="limpiar_examenes")
async def cmd_limpiar_examenes(ctx):
  memoria_global["examenes_y_entregas"].clear()
  historiales_chat.clear()
  await ctx.send("🧹 Se ha vaciado la lista de exámenes y entregas guardadas.")


@bot.command(name="borrar_examen")
async def cmd_borrar_examen(ctx, *, texto: str):
  encontrados = [
      e
      for e in memoria_global["examenes_y_entregas"]
      if texto.lower() in e.lower()
  ]
  if encontrados:
    for e in encontrados:
      memoria_global["examenes_y_entregas"].remove(e)
    historiales_chat.clear()
    await ctx.send(f"✅ Se ha eliminado: `{encontrados[0]}`")
  else:
    await ctx.send("❌ No se encontró ningún examen que coincida con ese texto.")


# RECORDATORIO AUTOMÁTICO
HORA_AVISO = time(hour=7, minute=30, tzinfo=zoneinfo.ZoneInfo("Europe/Madrid"))


@tasks.loop(time=HORA_AVISO)
async def recordatorio_diario():
  if CANAL_NOTIFICACIONES_ID == 0:
    return

  canal = bot.get_channel(CANAL_NOTIFICACIONES_ID)
  if canal:
    try:
      dias_semana = [
          "Lunes",
          "Martes",
          "Miércoles",
          "Jueves",
          "Viernes",
          "Sábado",
          "Domingo",
      ]
      dia_hoy = dias_semana[datetime.now().weekday()]

      prompt_autogen = (
          f"Hoy es {dia_hoy}. Dame un resumen breve de buenos días con mi"
          " estructura de horarios de hoy y recuérdame las entregas o exámenes"
          " pendientes."
      )

      configuracion = types.GenerateContentConfig(
          system_instruction=construir_system_prompt(), max_output_tokens=600
      )
      chat_temp = client.chats.create(
          model=MODELO_UNICO, config=configuracion
      )
      respuesta = chat_temp.send_message(prompt_autogen)

      embed = discord.Embed(
          title=f"☀️ Buenos días - Plan del {dia_hoy}",
          description=respuesta.text,
          color=discord.Color.gold(),
      )
      embed.set_footer(text="Zapy • Recordatorio Automático 7:30 AM")

      await canal.send(embed=embed)
    except Exception as e:
      print(f"Error en recordatorio automático: {e}")


@bot.command(name="resumen")
async def cmd_resumen(ctx):
  embed = discord.Embed(
      title="📌 Resumen de Rutina y Memoria Global",
      description="Vista rápida de tus bloques clave y tareas pendientes.",
      color=discord.Color.blue(),
  )

  embed.add_field(
      name="⏰ Bloques fijos (Lunes a Viernes)",
      value=(
          "• **Despertar:** 7:10 AM\n"
          "• **Clases:** 8:15 - 14:15\n"
          "• **Comida/Descanso:** 14:30 - 15:30\n"
          "• **Buscar hermana:** 16:20 - 16:45"
      ),
      inline=False,
  )

  ex_str = (
      "\n".join(f"• {e}" for e in memoria_global["examenes_y_entregas"])
      if memoria_global["examenes_y_entregas"]
      else "Sin exámenes o entregas registradas."
  )
  embed.add_field(
      name="📝 Exámenes y Entregas Registradas", value=ex_str, inline=False
  )

  embed.set_footer(
      text="Zapy Productivity • Mencióname o envíame una nota de voz"
  )
  await ctx.send(embed=embed)


async def procesar_y_enviar_respuesta(
    chat_session, contenido_prompt, destino, titulo
):
  response_stream = chat_session.send_message_stream(contenido_prompt)
  texto_acumulado = ""

  for chunk in response_stream:
    if chunk.text:
      texto_acumulado += chunk.text

  actualizar_memoria_extraer_examenes(texto_acumulado)

  color = discord.Color.purple()
  texto_lower = texto_acumulado.lower()
  if "estudiar" in texto_lower or "tarea" in texto_lower or "deberes" in texto_lower:
    color = discord.Color.blue()
  elif "entreno" in texto_lower or "entrenamiento" in texto_lower:
    color = discord.Color.orange()
  elif "libre" in texto_lower or "descanso" in texto_lower or "cena" in texto_lower:
    color = discord.Color.green()

  if len(texto_acumulado) <= 4000:
    embed = discord.Embed(
        title=f"⚡ {titulo}", description=texto_acumulado, color=color
    )
    embed.set_footer(text="Zapy Productivity Bot")
    await destino.send(embed=embed)
  else:
    limite = 1900
    for i in range(0, len(texto_acumulado), limite):
      await destino.send(texto_acumulado[i : i + limite])


@bot.event
async def on_ready():
  print(f"Zapy activado con {MODELO_UNICO} como {bot.user}")
  if not recordatorio_diario.is_running():
    recordatorio_diario.start()


@bot.event
async def on_message(message):
  if message.author == bot.user:
    return

  es_hilo = isinstance(message.channel, discord.Thread)
  es_hilo_del_bot = es_hilo and message.channel.owner == bot.user
  fue_mencionado = bot.user.mentioned_in(message)

  if (fue_mencionado and not es_hilo) or es_hilo_del_bot:
    async with message.channel.typing():
      audio_uploaded = None
      audio_bytes_io = None
      try:
        prompt_texto = message.content.replace(f"<@{bot.user.id}>", "").strip()

        # Procesamiento optimizado de archivos de audio directo a memoria
        if message.attachments:
          for attachment in message.attachments:
            extensiones_audio = [
                ".ogg",
                ".mp3",
                ".wav",
                ".m4a",
                ".aac",
                ".flac",
            ]
            if any(
                attachment.filename.lower().endswith(ext)
                for ext in extensiones_audio
            ):
              audio_bytes = await attachment.read()
              audio_bytes_io = io.BytesIO(audio_bytes)
              audio_bytes_io.name = attachment.filename

              audio_uploaded = client.files.upload(
                  file=audio_bytes_io, mime_type=attachment.content_type
              )
              break

        if audio_uploaded:
          instruccion_audio = (
              prompt_texto
              if prompt_texto
              else (
                  "Escucha este audio atentamente, responde a lo que pido o"
                  " hazme un resumen estructurado."
              )
          )
          contenido_prompt = [audio_uploaded, instruccion_audio]
          texto_para_titulo = prompt_texto or "Nota de voz recibida"
        else:
          contenido_prompt = (
              prompt_texto if prompt_texto else "Organízame el día de hoy"
          )
          texto_para_titulo = contenido_prompt
          actualizar_memoria_extraer_examenes(prompt_texto)

        if fue_mencionado and not es_hilo:
          titulo_hilo = generar_titulo_hilo_optimizado(texto_para_titulo)
          thread = await message.create_thread(name=titulo_hilo)

          chat_session = obtener_o_crear_chat(thread.id)
          await procesar_y_enviar_respuesta(
              chat_session, contenido_prompt, thread, titulo_hilo
          )

        elif es_hilo_del_bot:
          chat_session = obtener_o_crear_chat(message.channel.id)
          await procesar_y_enviar_respuesta(
              chat_session, contenido_prompt, message.channel, "Planificación"
          )

      except Exception as e:
        error_str = str(e)
        print(f"Error procesando mensaje: {error_str}")
        await message.reply(
            f"⚠️ Ocurrió un error al responder: `{error_str[:100]}`"
        )
      finally:
        if audio_bytes_io:
          audio_bytes_io.close()

  await bot.process_commands(message)


TOKEN = os.environ.get("DISCORD_TOKEN")
if TOKEN:
  bot.run(TOKEN)

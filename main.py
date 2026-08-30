import os
import discord
from google import genai

# Configuración del cliente de Gemini
ai_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# Configuración de Discord
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Tu ID de usuario
MI_ID = int(os.environ.get("MI_DISCORD_ID", "0"))

# Tu rutina personalizada
PROMPT_MI_RUTINA = """Lunes 

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: Ejemplo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:20 - 16:45 Buscar a mi hermana, 17:30 - 16:30 entrenar, 20:30 - 21:30 volver a casa y cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Martes

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: Ejemplo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:20 - 16:45 Buscar a mi hermana, 20:00 - 21:30 Estar con Familia y Cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Miercoles

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: Ejemplo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:20 - 16:45 Buscar a mi hermana, 17:30 - 20:30 entrenar, 20:30 - 21:30 volver a casa y cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Jueves

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: Ejemplo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:20 - 16:45 Buscar a mi hermana, 19:30 - 21:45 entrenar, 22:00 - 22:30 volver a casa y cenar
- Hora de cierre / descanso nocturno: Depende de que tareas me queden, (Siempre priorizar 8-9 horas de sueño)

Viernes

- Hora de despertar / inicio del día: 7:10
- Trabajo / Clases / Compromisos fijos: 8:15 - 14:15
- Comida / Descanso fijo: Ejemplo: 14:30 a 15:30
- Otros bloqueos (ej. gimnasio, traslados): 16:20 - 16:45 Buscar a mi hermana, las tardes del viernes no estudio
- Hora de cierre / descanso nocturno: Nunca se sabe, pero tarde


Sabado: Los sabados a la mañana/mediodia hay partido y no suelo estar hasta las 16:00

Domingo: Entre las 13:00 y 16:00 no puedo.



"""

@client.event
async def on_ready():
    print(f'Bot conectado correctamente como {client.user}')

@client.event
async def on_message(message):
    # Ignorar mensajes del propio bot
    if message.author == client.user:
        return

    # Responder solo en el canal #planificacion y si el mensaje es tuyo
    if message.channel.name == 'planificacion' and message.author.id == MI_ID:
        prompt_final = f"{PROMPT_MI_RUTINA}\n\nMensaje del usuario: {message.content}"

        response = ai_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt_final,
        )

        await message.create_thread(name="Planificación", content=response.text)

client.run(os.environ["DISCORD_TOKEN"])


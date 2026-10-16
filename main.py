import os
import httpx
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from google import genai
from google.genai import types

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_TELEGRAM = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

cliente_ia = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODELO = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
MODELO_RESPALDO = os.getenv("GEMINI_MODEL_RESPALDO", "gemini-flash-lite-latest")

# ---------- Información del negocio (aquí personalizas cada cliente) ----------
INSTRUCCIONES = """
Eres el asistente virtual de "Barbería Demo", una barbería en Medellín.
Atiendes a los clientes por chat de forma amable, cercana y breve,
como un buen recepcionista paisa. Usa pocos emojis.

INFORMACIÓN DE LA BARBERÍA:
- Servicios y precios:
  * Corte de pelo: $50.000
  * Corte de barba: $30.000
  * Corte de pelo y barba: $80.000
- Horario: lunes a viernes de 8:00 a.m. a 8:00 p.m. Sábados y domingos cerrado.
- Barberos: Daniel, Sebastián, Camilo y Santiago.
- Formas de pago: datáfono, QR y efectivo.
- Dirección: Calle de Ejemplo #00-00, Medellín (CAMBIAR).

REGLAS:
1. Responde solo con la información de arriba. Si te preguntan algo que no está
   (por ejemplo, otros servicios o promociones), di que no tienes ese dato
   y que pueden consultarlo directamente en la barbería. Nunca inventes precios.
2. Si el cliente quiere agendar una cita, pídele estos datos, uno o dos a la vez:
   nombre, servicio, barbero preferido (o "cualquiera"), día y hora.
3. Solo acepta citas dentro del horario de atención. Si pide fuera del horario,
   propón una alternativa.
4. Cuando tengas todos los datos, haz un resumen de la cita y dile que su
   solicitud quedó registrada y que la barbería le confirmará pronto.
5. Mensajes cortos: máximo 3 o 4 líneas.
"""

# Memoria de conversaciones: chat_id -> lista de mensajes
historiales = {}
MAX_MENSAJES = 20  # cuántos mensajes recordar por chat

app = FastAPI()


async def enviar_telegram(chat_id: int, texto: str):
    async with httpx.AsyncClient() as cliente:
        r = await cliente.post(
            f"{API_TELEGRAM}/sendMessage",
            json={"chat_id": chat_id, "text": texto},
        )
        if r.status_code != 200:
            print("Error de Telegram:", r.status_code, r.text)


async def responder_con_ia(chat_id: int, texto: str) -> str:
    historial = historiales.setdefault(chat_id, [])
    historial.append({"role": "user", "parts": [{"text": texto}]})

    # Probamos el modelo principal y luego el de respaldo, 2 intentos cada uno
    for modelo in [MODELO, MODELO_RESPALDO]:
        for intento in range(2):
            try:
                respuesta = await cliente_ia.aio.models.generate_content(
                    model=modelo,
                    contents=historial,
                    config=types.GenerateContentConfig(
                        system_instruction=INSTRUCCIONES,
                        temperature=0.4,
                    ),
                )
                texto_respuesta = respuesta.text or "Perdón, no entendí. ¿Me lo repites?"
                historial.append({"role": "model", "parts": [{"text": texto_respuesta}]})
                del historial[:-MAX_MENSAJES]
                return texto_respuesta
            except Exception as error:
                print(f"Error con {modelo} (intento {intento + 1}): {error}")
                await asyncio.sleep(2)  # esperamos un poco antes de reintentar

    # Si todo falló
    historial.pop()
    return "Uy, tuve un problema técnico. Intenta de nuevo en un momento 🙏"

@app.get("/")
def inicio():
    return {"mensaje": "¡El bot está vivo!"}


@app.post("/telegram")
async def telegram(request: Request):
    datos = await request.json()
    mensaje = datos.get("message")

    if not mensaje or "text" not in mensaje:
        return {"ok": True}

    chat_id = mensaje["chat"]["id"]
    texto = mensaje["text"]
    print(f"Cliente ({chat_id}): {texto}")

    respuesta = await responder_con_ia(chat_id, texto)
    print(f"Bot: {respuesta}")

    await enviar_telegram(chat_id, respuesta)
    return {"ok": True}
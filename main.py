import os
import re
import json
import uuid
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import gspread
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from google import genai
from google.genai import types

load_dotenv()

# ---------- Configuración ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_TELEGRAM = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

cliente_ia = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODELO = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
MODELO_RESPALDO = os.getenv("GEMINI_MODEL_RESPALDO", "gemini-flash-latest")

ZONA_COLOMBIA = timezone(timedelta(hours=-5))
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# Columnas de la hoja (empezando en 0):
# 0 Fecha registro | 1 Nombre | 2 Servicio | 3 Barbero | 4 Día | 5 Hora
# 6 Chat ID | 7 ID cita | 8 Estado
COLUMNAS = 9


# ---------- Google Sheets ----------
def conectar_hoja():
    credenciales_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if credenciales_json:
        gc = gspread.service_account_from_dict(json.loads(credenciales_json))
    else:
        gc = gspread.service_account(filename="credenciales-google.json")
    return gc.open_by_key(os.getenv("GOOGLE_SHEET_ID")).sheet1

hoja_citas = conectar_hoja()


def obtener_citas_activas(chat_id: int) -> list[dict]:
    """Citas futuras y no canceladas agendadas desde este chat."""
    hoy = datetime.now(ZONA_COLOMBIA).strftime("%Y-%m-%d")
    filas = hoja_citas.get_all_values()
    citas = []
    for numero_fila, fila in enumerate(filas[1:], start=2):  # saltamos encabezados
        fila = fila + [""] * (COLUMNAS - len(fila))  # por si hay celdas vacías
        if (fila[6] == str(chat_id) and fila[7] and fila[4] >= hoy and fila[8] != "cancelada"):
            citas.append({
                "fila": numero_fila,
                "id": fila[7],
                "nombre": fila[1],
                "servicio": fila[2],
                "barbero": fila[3],
                "dia": fila[4],
                "hora": fila[5],
            })
    return citas


def guardar_cita(cita: dict, chat_id: int) -> str:
    accion = cita.get("accion", "nueva")
    ahora = datetime.now(ZONA_COLOMBIA).strftime("%Y-%m-%d %H:%M")

    def armar_fila(id_cita: str) -> list:
        return [
            ahora,
            cita.get("nombre", ""),
            cita.get("servicio", ""),
            cita.get("barbero", ""),
            cita.get("dia", ""),
            cita.get("hora", ""),
            str(chat_id),
            id_cita,
            "activa",
        ]

    if accion in ("modificar", "cancelar"):
        for existente in obtener_citas_activas(chat_id):
            if existente["id"] == cita.get("id"):
                fila = existente["fila"]
                if accion == "cancelar":
                    hoja_citas.update_cell(fila, 9, "cancelada")
                    return f"cancelada ({existente['id']})"
                hoja_citas.update(
                    range_name=f"A{fila}:I{fila}",
                    values=[armar_fila(existente["id"])],
                    value_input_option="RAW",
                )
                return f"actualizada ({existente['id']})"

        if accion == "cancelar":
            return "no encontrada para cancelar"
        # Si pidió modificar pero no encontramos el ID, la guardamos como nueva

    nuevo_id = uuid.uuid4().hex[:6].upper()
    hoja_citas.append_row(armar_fila(nuevo_id), value_input_option="RAW")
    return f"nueva ({nuevo_id})"


# ---------- Información del negocio ----------
INFO_NEGOCIO = """
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
1. Responde solo con la información de arriba. Si te preguntan algo que no está,
   di que no tienes ese dato y que pueden consultarlo en la barbería.
   Nunca inventes precios ni servicios.
2. Para agendar necesitas: nombre de la persona que va a asistir (puede ser
   el cliente u otra persona, como un amigo o familiar; si no está claro,
   pregunta "¿la cita es para ti o para otra persona?"). Recuerda que sea lo que sea necesitas el nombre, servicio, barbero
   (o "cualquiera"), día y hora. Pide uno o dos datos a la vez.
3. Solo acepta citas dentro del horario y en fechas que no hayan pasado.
   Si pide fuera del horario, propón una alternativa.
4. Un cliente puede tener varias citas activas (para él o para otras personas).
   Si el cliente pide un cambio y no está claro si quiere modificar una cita
   existente o agendar una adicional, pregúntale.
5. Antes de registrar, cancelar o modificar, muestra un resumen y pregunta si confirma.
6. SOLO cuando el cliente confirme, responde con un mensaje de confirmación y
   al final agrega una línea por cada cita afectada, exactamente con este formato:
   CITA: {"accion": "nueva", "id": "", "nombre": "...", "servicio": "...", "barbero": "...", "dia": "AAAA-MM-DD", "hora": "HH:MM"}
   - "accion" puede ser "nueva", "modificar" o "cancelar".
   - Para "modificar" o "cancelar", usa el "id" de la lista de citas activas
     y escribe TODOS los datos (los que cambian y los que no).
   - Fecha en formato AAAA-MM-DD y hora en formato 24 horas.
   - Nunca escribas esas líneas en otro momento ni le muestres los ID al cliente.
7. Si el cliente pregunta por sus citas, usa la lista de citas activas.
8. No afirmes que hay disponibilidad. Di que tomas la solicitud y que la
   barbería confirmará el espacio.
9. Mensajes cortos: máximo 3 o 4 líneas.
10. Nunca registres una cita sin el nombre real de quien asiste. No uses
    palabras genéricas como "Cliente", "Usuario" o "yo" como nombre.
"""


def armar_instrucciones(citas: list[dict], nombre_telegram: str) -> str:
    hoy = datetime.now(ZONA_COLOMBIA)
    fecha = f"{DIAS[hoy.weekday()]} {hoy.strftime('%Y-%m-%d')}, hora actual {hoy.strftime('%H:%M')}"
    texto = INFO_NEGOCIO + f"\nHOY ES: {fecha} (hora de Colombia).\n"

    if nombre_telegram:
        texto += (f"\nEn Telegram, el cliente aparece como '{nombre_telegram}'. "
                  "Si la cita es para él, pregúntale si la dejas a ese nombre "
                  "o a otro antes de registrarla.\n")

    if citas:
        texto += "\nCITAS ACTIVAS AGENDADAS DESDE ESTE CHAT:\n"
        for c in citas:
            texto += (f"- ID {c['id']}: {c['nombre']}, {c['servicio']}, "
                      f"con {c['barbero']}, {c['dia']} a las {c['hora']}\n")
    else:
        texto += "\nEste cliente no tiene citas activas.\n"
    return texto


# ---------- Memoria de conversaciones ----------
historiales = {}
MAX_MENSAJES = 20

app = FastAPI()


async def enviar_telegram(chat_id: int, texto: str):
    async with httpx.AsyncClient() as cliente:
        r = await cliente.post(
            f"{API_TELEGRAM}/sendMessage",
            json={"chat_id": chat_id, "text": texto},
        )
        if r.status_code != 200:
            print("Error de Telegram:", r.status_code, r.text)


async def preguntar_ia(historial: list, instrucciones: str) -> str | None:
    for modelo in [MODELO, MODELO_RESPALDO]:
        for intento in range(2):
            try:
                respuesta = await cliente_ia.aio.models.generate_content(
                    model=modelo,
                    contents=historial,
                    config=types.GenerateContentConfig(
                        system_instruction=instrucciones,
                        temperature=0.4,
                    ),
                )
                return respuesta.text or "Perdón, no entendí. ¿Me lo repites?"
            except Exception as error:
                mensaje_error = str(error)
                print(f"Error con {modelo} (intento {intento + 1}): {mensaje_error[:120]}")
                if "429" in mensaje_error or "RESOURCE_EXHAUSTED" in mensaje_error:
                    break
                await asyncio.sleep(2)
    return None

NOMBRES_INVALIDOS = {"", "cliente", "usuario", "yo", "mi", "mí", "...", "nombre"}


def nombre_valido(cita: dict) -> bool:
    if cita.get("accion") == "cancelar":
        return True  # para cancelar no hace falta validar el nombre
    nombre = str(cita.get("nombre", "")).strip().lower()
    return nombre not in NOMBRES_INVALIDOS

async def responder(chat_id: int, texto: str, nombre_telegram: str) -> str:
    historial = historiales.setdefault(chat_id, [])
    historial.append({"role": "user", "parts": [{"text": texto}]})

    try:
        citas = await asyncio.to_thread(obtener_citas_activas, chat_id)
    except Exception as error:
        print("No se pudieron leer las citas:", error)
        citas = []

    instrucciones = armar_instrucciones(citas, nombre_telegram)
    texto_ia = await preguntar_ia(historial, instrucciones)
    if texto_ia is None:
        historial.pop()
        return "Uy, tuve un problema técnico. Intenta de nuevo en un momento 🙏"

    historial.append({"role": "model", "parts": [{"text": texto_ia}]})
    del historial[:-MAX_MENSAJES]

    coincidencias = list(re.finditer(r"CITA:\s*(\{.*?\})", texto_ia, re.DOTALL))
    if not coincidencias:
        return texto_ia

    texto_cliente = texto_ia[: coincidencias[0].start()].strip()
    for coincidencia in coincidencias:
        try:
            cita = json.loads(coincidencia.group(1))
        except Exception as error:
            print("❌ JSON inválido:", error)
            continue

        # Validación: sin nombre real, no guardamos
        if not nombre_valido(cita):
            print("⚠️ Cita sin nombre real, no se guardó:", cita)
            pregunta = "¡Casi listo! ¿A nombre de quién dejo la cita? 😊"
            # Corregimos la memoria para que la IA sepa que NO se registró
            historial[-1] = {"role": "model", "parts": [{"text": pregunta}]}
            return pregunta

        try:
            resultado = await asyncio.to_thread(guardar_cita, cita, chat_id)
            print(f"✅ Cita {resultado}:", cita)
        except Exception as error:
            print("❌ Error guardando la cita:", error)
            texto_cliente += "\n\n(Hubo un problema registrando tu cita, la barbería te contactará para confirmarla.)"

    return texto_cliente


# ---------- Rutas ----------
@app.api_route("/", methods=["GET", "HEAD"])
def inicio():
    return {"mensaje": "¡El bot está vivo!"}


updates_procesados = set()
tareas_activas = set()


async def procesar_mensaje(chat_id: int, texto: str, nombre_telegram: str):
    try:
        respuesta = await responder(chat_id, texto, nombre_telegram)
        print(f"Bot: {respuesta}")
        await enviar_telegram(chat_id, respuesta)
    except Exception as error:
        print("Error procesando mensaje:", error)


@app.post("/telegram")
async def telegram(request: Request):
    datos = await request.json()

    update_id = datos.get("update_id")
    if update_id in updates_procesados:
        print(f"Mensaje repetido ignorado ({update_id})")
        return {"ok": True}
    updates_procesados.add(update_id)
    if len(updates_procesados) > 1000:
        updates_procesados.clear()

    mensaje = datos.get("message")
    if not mensaje or "text" not in mensaje:
        return {"ok": True}

    chat_id = mensaje["chat"]["id"]
    texto = mensaje["text"]
    nombre_telegram = mensaje.get("from", {}).get("first_name", "")
    print(f"Cliente ({chat_id}): {texto}")

    tarea = asyncio.create_task(procesar_mensaje(chat_id, texto, nombre_telegram))
    tareas_activas.add(tarea)
    tarea.add_done_callback(tareas_activas.discard)

    return {"ok": True}
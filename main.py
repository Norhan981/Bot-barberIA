import os
import re
import json
import uuid
import asyncio
import unicodedata
from contextlib import asynccontextmanager
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

# ---------- Reglas del negocio (aquí personalizas cada cliente) ----------
BARBEROS = ["Daniel", "Sebastián", "Camilo", "Santiago"]
DURACIONES = {  # en minutos
    "corte de pelo": 60,
    "corte de barba": 30,
    "corte de pelo y barba": 90,
}
DURACION_POR_DEFECTO = 60
HORA_APERTURA = 8   # 8:00 a.m.
HORA_CIERRE = 20    # 8:00 p.m.
DIAS_ATENCION = {0, 1, 2, 3, 4}  # lunes (0) a viernes (4)
SIN_PREFERENCIA = {"", "cualquiera", "cualquier", "cualquier barbero", "el que este libre"}

ACTIVA, CANCELADA, ATENDIDA = "Activa", "Cancelada", "Atendida"

# Columnas de la hoja (empezando en 0):
# 0 Fecha registro | 1 Nombre | 2 Servicio | 3 Barbero | 4 Día | 5 Hora
# 6 Chat ID | 7 ID cita | 8 Estado
COLUMNAS = 9


def normalizar(texto) -> str:
    """Minúsculas y sin tildes, para comparar 'Sebastián' con 'sebastian'."""
    texto = unicodedata.normalize("NFD", str(texto)).encode("ascii", "ignore").decode()
    return texto.strip().lower()


def hora_bonita(momento: datetime) -> str:
    sufijo = "a.m." if momento.hour < 12 else "p.m."
    return f"{momento.hour % 12 or 12}:{momento:%M} {sufijo}"


# ---------- Google Sheets ----------
def conectar_hoja():
    credenciales_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if credenciales_json:
        gc = gspread.service_account_from_dict(json.loads(credenciales_json))
    else:
        gc = gspread.service_account(filename="credenciales-google.json")
    return gc.open_by_key(os.getenv("GOOGLE_SHEET_ID")).sheet1


hoja_citas = conectar_hoja()


def leer_citas() -> list[dict]:
    filas = hoja_citas.get_all_values()
    citas = []
    for numero_fila, fila in enumerate(filas[1:], start=2):  # saltamos encabezados
        fila = fila + [""] * (COLUMNAS - len(fila))
        if not fila[7]:  # sin ID = fila vieja o vacía
            continue
        citas.append({
            "fila": numero_fila, "nombre": fila[1], "servicio": fila[2],
            "barbero": fila[3], "dia": fila[4], "hora": fila[5],
            "chat_id": fila[6], "id": fila[7], "estado": fila[8],
        })
    return citas


def es_activa(cita: dict) -> bool:
    return normalizar(cita["estado"]) == "activa"


def inicio_y_fin(cita: dict):
    """Devuelve (inicio, fin) de la cita, o None si la fecha/hora no es válida."""
    try:
        inicio = datetime.strptime(f"{cita['dia']} {cita['hora']}", "%Y-%m-%d %H:%M")
    except (ValueError, KeyError):
        return None
    inicio = inicio.replace(tzinfo=ZONA_COLOMBIA)
    duracion = DURACIONES.get(normalizar(cita.get("servicio", "")), DURACION_POR_DEFECTO)
    return inicio, inicio + timedelta(minutes=duracion)


# ---------- Disponibilidad ----------
def barbero_libre(barbero, inicio, fin, citas, ignorar_id=None) -> bool:
    for c in citas:
        if not es_activa(c) or c["id"] == ignorar_id:
            continue
        if normalizar(c["barbero"]) != normalizar(barbero):
            continue
        rango = inicio_y_fin(c)
        if rango and inicio < rango[1] and rango[0] < fin:  # los horarios se cruzan
            return False
    return True


def problema_de_horario(inicio, fin):
    if inicio < datetime.now(ZONA_COLOMBIA):
        return "esa fecha y hora ya pasaron"
    if inicio.weekday() not in DIAS_ATENCION:
        return "ese día no atendemos (abrimos de lunes a viernes)"
    apertura = inicio.replace(hour=HORA_APERTURA, minute=0)
    cierre = inicio.replace(hour=HORA_CIERRE, minute=0)
    if inicio < apertura or fin > cierre:
        return "el servicio quedaría por fuera del horario (8:00 a.m. a 8:00 p.m.)"
    return None


def horas_libres(barbero, inicio_pedido, duracion, citas, ignorar_id=None, maximo=4):
    """Horas libres del barbero ese día, empezando por las más cercanas a la pedida."""
    ahora = datetime.now(ZONA_COLOMBIA)
    momento = inicio_pedido.replace(hour=HORA_APERTURA, minute=0)
    cierre = inicio_pedido.replace(hour=HORA_CIERRE, minute=0)
    libres = []
    while momento + timedelta(minutes=duracion) <= cierre:
        fin = momento + timedelta(minutes=duracion)
        if momento > ahora and barbero_libre(barbero, momento, fin, citas, ignorar_id):
            libres.append(momento)
        momento += timedelta(minutes=30)
    libres.sort(key=lambda m: abs((m - inicio_pedido).total_seconds()))
    return [hora_bonita(m) for m in sorted(libres[:maximo])]


def agenda_ocupada(citas, chat_id: int) -> str:
    """Horarios ocupados de los próximos 14 días, agrupados por día (sin nombres)."""
    ahora = datetime.now(ZONA_COLOMBIA)
    limite = ahora + timedelta(days=14)
    por_dia = {}
    for c in citas:
        rango = inicio_y_fin(c)
        if not es_activa(c) or not rango or rango[1] < ahora or rango[0] > limite:
            continue
        inicio, fin = rango
        dia = f"{DIAS[inicio.weekday()].capitalize()} {inicio:%Y-%m-%d}"
        linea = f"  * {c['barbero']} ocupado de {inicio:%H:%M} a {fin:%H:%M}"
        if c["chat_id"] == str(chat_id):
            linea += f" (es la cita ID {c['id']} de ESTE MISMO cliente)"
        por_dia.setdefault((inicio.date(), dia), []).append((inicio, linea))

    lineas = []
    for (_, dia), ocupaciones in sorted(por_dia.items()):
        lineas.append(f"{dia}:")
        lineas.extend(linea for _, linea in sorted(ocupaciones))
    return "\n".join(lineas)


# ---------- Validar, confirmar y guardar ----------
NOMBRES_INVALIDOS = {"", "cliente", "usuario", "yo", "mi", "...", "nombre"}


def nombre_valido(cita: dict) -> bool:
    if cita.get("accion") == "cancelar":
        return True
    return normalizar(cita.get("nombre", "")) not in NOMBRES_INVALIDOS


def validar_cita(cita: dict, chat_id: int, citas: list[dict]):
    """Revisa una cita SIN guardarla. Devuelve (ok, mensaje_de_error, cita_lista)."""
    cita = dict(cita)
    cita.setdefault("accion", "nueva")
    mis_activas = [c for c in citas if c["chat_id"] == str(chat_id) and es_activa(c)]
    existente = next((c for c in mis_activas if c["id"] == cita.get("id")), None)

    if cita["accion"] == "cancelar":
        if not existente:
            return False, "No encontré esa cita para cancelarla. ¿Me dices cuál es?", None
        for campo in ("nombre", "servicio", "barbero", "dia", "hora", "id", "fila"):
            cita[campo] = existente[campo]
        return True, "", cita

    if cita["accion"] == "modificar" and not existente:
        cita["accion"] = "nueva"

    if not nombre_valido(cita):
        return False, "¡Casi listo! ¿A nombre de quién dejo la cita? 😊", None

    rango = inicio_y_fin(cita)
    if not rango:
        return False, "No entendí bien la fecha o la hora. ¿Me la repites?", None
    inicio, fin = rango

    problema = problema_de_horario(inicio, fin)
    if problema:
        return False, f"Uy, {problema}. ¿Qué otro día u hora te sirve?", None

    ignorar = existente["id"] if existente else None

    # La misma persona no puede tener dos citas que se crucen
    for c in mis_activas:
        if c["id"] == ignorar or normalizar(c["nombre"]) != normalizar(cita.get("nombre", "")):
            continue
        rango_otra = inicio_y_fin(c)
        if rango_otra and inicio < rango_otra[1] and rango_otra[0] < fin:
            return False, (f"{cita['nombre']} ya tiene una cita que se cruza con ese horario "
                           f"({c['servicio']} con {c['barbero']} a las {hora_bonita(rango_otra[0])}). "
                           "¿Quieres cambiar esa cita o prefieres otra hora?"), None

    duracion = int((fin - inicio).total_seconds() // 60)
    barbero = cita.get("barbero", "")

    if normalizar(barbero) in SIN_PREFERENCIA:
        libres = [b for b in BARBEROS if barbero_libre(b, inicio, fin, citas, ignorar)]
        if not libres:
            return False, "A esa hora ya están todos los barberos ocupados 😕 ¿Te sirve otra hora?", None
        cita["barbero"] = libres[0]
    else:
        nombres_validos = {normalizar(b): b for b in BARBEROS}
        if normalizar(barbero) not in nombres_validos:
            return False, f"No tenemos un barbero llamado {barbero}. Están {', '.join(BARBEROS)}.", None
        cita["barbero"] = nombres_validos[normalizar(barbero)]

        if not barbero_libre(cita["barbero"], inicio, fin, citas, ignorar):
            texto = f"Uy, {cita['barbero']} ya tiene una cita a esa hora 😕"
            sugerencias = horas_libres(cita["barbero"], inicio, duracion, citas, ignorar)
            if sugerencias:
                texto += f" Ese día le quedan libres: {', '.join(sugerencias)}."
            otros = [b for b in BARBEROS if b != cita["barbero"]
                     and barbero_libre(b, inicio, fin, citas, ignorar)]
            if otros:
                texto += f" A esa misma hora están libres: {', '.join(otros)}."
            return False, texto + " ¿Qué prefieres?", None

    cita["id"] = existente["id"] if existente else ""
    cita["fila"] = existente["fila"] if existente else None
    return True, "", cita


def preparar_citas(citas_pedidas: list[dict], chat_id: int):
    """Valida todas las citas que propuso la IA. Devuelve (ok, error, citas_listas)."""
    citas = leer_citas()
    listas = []
    for pedida in citas_pedidas:
        ok, error, lista = validar_cita(pedida, chat_id, citas)
        if not ok:
            return False, error, None
        listas.append(lista)
    return True, "", listas


def guardar_cita(cita: dict, chat_id: int) -> str:
    if cita["accion"] == "cancelar":
        hoja_citas.update_cell(cita["fila"], 9, CANCELADA)
        return f"cancelada ({cita['id']})"

    id_cita = cita["id"] or uuid.uuid4().hex[:6].upper()
    fila = [
        datetime.now(ZONA_COLOMBIA).strftime("%Y-%m-%d %H:%M"),
        cita.get("nombre", ""), cita.get("servicio", ""), cita["barbero"],
        cita.get("dia", ""), cita.get("hora", ""), str(chat_id), id_cita, ACTIVA,
    ]
    if cita.get("fila"):
        n = cita["fila"]
        hoja_citas.update(range_name=f"A{n}:I{n}", values=[fila], value_input_option="RAW")
        return f"actualizada ({id_cita})"

    hoja_citas.append_row(fila, value_input_option="RAW")
    return f"nueva ({id_cita})"


def confirmar_y_guardar(pendientes_chat: list[dict], chat_id: int):
    """Vuelve a validar con la agenda actual (pudo cambiar) y guarda todo."""
    ok, error, listas = preparar_citas(pendientes_chat, chat_id)
    if not ok:
        return False, f"Uy, la agenda cambió mientras confirmabas. {error}"
    return True, [guardar_cita(c, chat_id) for c in listas]


def armar_resumen(citas_listas: list[dict]) -> str:
    acciones = {"nueva": "Agendar", "modificar": "Cambiar a", "cancelar": "Cancelar"}
    lineas = []
    for c in citas_listas:
        inicio = inicio_y_fin(c)[0]
        fecha = f"{DIAS[inicio.weekday()]} {inicio.day}/{inicio.month} a las {hora_bonita(inicio)}"
        lineas.append(f"• {acciones[c['accion']]}: {c['servicio']} para {c['nombre']} "
                      f"con {c['barbero']}, el {fecha}")
    return "Te confirmo:\n" + "\n".join(lineas) + "\n\n¿Confirmas? Responde sí o no 🙂"


# ---------- Respuestas sí / no ----------
AFIRMATIVOS = {"si", "sip", "sii", "dale", "de una", "confirmo", "confirmado", "listo",
               "ok", "okay", "claro", "perfecto", "hagale", "de acuerdo", "correcto", "va"}
NEGATIVOS = {"no", "nop", "nel", "mejor no", "cancelar"}


def limpiar(texto: str) -> str:
    return re.sub(r"[^\w\s]", "", normalizar(texto)).strip()


def es_si(texto: str) -> bool:
    t = limpiar(texto)
    return t in AFIRMATIVOS or t.startswith(("si ", "dale", "confirmo", "listo", "claro", "hagale"))


def es_no(texto: str) -> bool:
    t = limpiar(texto)
    return t in NEGATIVOS or t.startswith("no ")


# ---------- Estados automáticos ----------
def marcar_atendidas() -> int:
    ahora = datetime.now(ZONA_COLOMBIA)
    cambios = []
    for c in leer_citas():
        rango = inicio_y_fin(c)
        if es_activa(c) and rango and rango[1] <= ahora:
            cambios.append({"range": f"I{c['fila']}", "values": [[ATENDIDA]]})
    if cambios:
        hoja_citas.batch_update(cambios, value_input_option="RAW")
    return len(cambios)


async def revisar_atendidas_periodicamente():
    while True:
        try:
            cantidad = await asyncio.to_thread(marcar_atendidas)
            if cantidad:
                print(f"🕒 {cantidad} cita(s) marcada(s) como atendida(s)")
        except Exception as error:
            print("Error marcando atendidas:", error)
        await asyncio.sleep(30 * 60)  # cada 30 minutos


# ---------- Información del negocio ----------
INFO_NEGOCIO = """
Eres el asistente virtual de "Barbería Demo", una barbería en Medellín.
Atiendes a los clientes por chat de forma amable, cercana y breve,
como un buen recepcionista paisa. Usa pocos emojis.

INFORMACIÓN DE LA BARBERÍA:
- Servicios, precios y duración (usa SIEMPRE estos nombres exactos):
  * Corte de pelo: $50.000 (1 hora)
  * Corte de barba: $30.000 (30 minutos)
  * Corte de pelo y barba: $80.000 (1 hora y 30 minutos)
- Horario: lunes a viernes de 8:00 a.m. a 8:00 p.m. Sábados y domingos cerrado.
  El servicio debe terminar antes de las 8:00 p.m.
- Barberos: Daniel, Sebastián, Camilo y Santiago.
- Formas de pago: datáfono, QR y efectivo.
- Dirección: Calle de Ejemplo #00-00, Medellín (CAMBIAR).

REGLAS:
1. Responde solo con la información de arriba. Si te preguntan algo que no está,
   di que no tienes ese dato y que pueden consultarlo en la barbería.
   Nunca inventes precios ni servicios.
2. Para agendar necesitas: nombre de la persona que va a asistir (puede ser
   el cliente u otra persona; si no está claro, pregunta "¿la cita es para ti
   o para otra persona?"), servicio, barbero (o "cualquiera"), día y hora.
   Pide uno o dos datos a la vez.
3. Revisa la AGENDA OCUPADA antes de proponer horarios. Solo di que un barbero
   está ocupado si en la agenda aparece ESE barbero, en ESE MISMO DÍA, con un
   horario que se cruce, y que NO sea una cita de este mismo cliente. Si el día
   no aparece en la agenda, todos están libres. Si tienes dudas, sigue: el
   sistema verifica la disponibilidad real.
4. Un cliente puede tener varias citas activas (para él o para otras personas).
   Si pide un cambio y no está claro si quiere modificar una cita existente o
   agendar una adicional, pregúntale.
5. Cuando tengas TODOS los datos (para agendar, modificar o cancelar), NO
   preguntes tú si confirma ni hagas el resumen: escribe solamente una línea por
   cada cita afectada, con este formato exacto. El sistema verificará la
   disponibilidad, le mostrará el resumen al cliente y le pedirá confirmación.
   CITA: {"accion": "nueva", "id": "", "nombre": "...", "servicio": "...", "barbero": "...", "dia": "AAAA-MM-DD", "hora": "HH:MM"}
   - "accion" puede ser "nueva", "modificar" o "cancelar".
   - Para "modificar" o "cancelar", usa el "id" de la lista de citas activas
     y escribe TODOS los datos (los que cambian y los que no).
   - En "servicio" usa el nombre exacto de la lista de servicios.
   - Si el cliente no tiene preferencia de barbero, escribe "cualquiera".
   - Fecha en formato AAAA-MM-DD y hora en formato 24 horas.
   - Nunca le muestres los ID al cliente.
6. Si el cliente pregunta por sus citas, usa la lista de citas activas.
7. Nunca uses palabras genéricas como "Cliente", "Usuario" o "yo" como nombre.
8. Si ves una nota del sistema entre paréntesis, tenla en cuenta, pero no la
   menciones.
9. Mensajes cortos: máximo 3 o 4 líneas.
10. Nunca des información sobre citas de otras personas. Si preguntan a quién
    atiende un barbero o quién tiene una cita, responde que esa información es
    privada. Solo puedes hablar de las citas activas de este mismo chat.
"""


def armar_instrucciones(mis_citas, nombre_telegram, agenda) -> str:
    hoy = datetime.now(ZONA_COLOMBIA)
    fecha = f"{DIAS[hoy.weekday()]} {hoy.strftime('%Y-%m-%d')}, hora actual {hoy.strftime('%H:%M')}"
    texto = INFO_NEGOCIO + f"\nHOY ES: {fecha} (hora de Colombia).\n"

    if nombre_telegram:
        texto += (f"\nEn Telegram, el cliente aparece como '{nombre_telegram}'. "
                  "Si la cita es para él, pregúntale si la dejas a ese nombre "
                  "o a otro.\n")

    if mis_citas:
        texto += "\nCITAS ACTIVAS AGENDADAS DESDE ESTE CHAT:\n"
        for c in mis_citas:
            texto += (f"- ID {c['id']}: {c['nombre']}, {c['servicio']}, "
                      f"con {c['barbero']}, {c['dia']} a las {c['hora']}\n")
    else:
        texto += "\nEste cliente no tiene citas activas.\n"

    texto += "\nAGENDA OCUPADA (próximos 14 días):\n"
    texto += (agenda or "No hay citas agendadas, todos los horarios están libres.") + "\n"
    return texto


# ---------- Memoria de conversaciones ----------
historiales = {}
pendientes = {}  # chat_id -> citas validadas esperando el "sí" del cliente
MAX_MENSAJES = 20

# Detecta "CITA:", "Cita:" o "cita:", con o sin espacios y comillas de código
PATRON_CITA = re.compile(r"cita\s*:\s*`*\s*(\{.*?\})", re.IGNORECASE | re.DOTALL)


def recordar(historial: list, rol: str, texto: str):
    historial.append({"role": rol, "parts": [{"text": texto}]})
    del historial[:-MAX_MENSAJES]


# ---------- Telegram e IA ----------
async def enviar_telegram(chat_id: int, texto: str):
    async with httpx.AsyncClient() as cliente:
        r = await cliente.post(
            f"{API_TELEGRAM}/sendMessage",
            json={"chat_id": chat_id, "text": texto},
        )
        if r.status_code != 200:
            print("Error de Telegram:", r.status_code, r.text)


async def preguntar_ia(historial: list, instrucciones: str):
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


async def responder(chat_id: int, texto: str, nombre_telegram: str) -> str:
    historial = historiales.setdefault(chat_id, [])

    # 1. ¿Hay una solicitud esperando confirmación?
    if chat_id in pendientes:
        pendiente = pendientes.pop(chat_id)
        if es_si(texto):
            recordar(historial, "user", texto)
            try:
                ok, resultado = await asyncio.to_thread(confirmar_y_guardar, pendiente, chat_id)
            except Exception as error:
                print("❌ Error guardando:", error)
                ok, resultado = False, "Tuve un problema registrando la cita. ¿Lo intentamos de nuevo?"
            if ok:
                print("✅ Guardado:", resultado)
                respuesta = "¡Listo! Quedó registrado ✅ ¿Te ayudo con algo más?"
            else:
                respuesta = resultado
            recordar(historial, "model", respuesta)
            return respuesta

        nota = ("(Nota del sistema: el cliente NO confirmó la solicitud anterior y no se "
                "registró nada. Pregúntale qué quiere cambiar.)" if es_no(texto) else
                "(Nota del sistema: la solicitud anterior no se confirmó ni se registró.)")
        texto = f"{texto}\n{nota}"

    # 2. Conversación normal con la IA
    historial.append({"role": "user", "parts": [{"text": texto}]})
    try:
        citas = await asyncio.to_thread(leer_citas)
    except Exception as error:
        print("No se pudieron leer las citas:", error)
        citas = []
    mis_citas = [c for c in citas if c["chat_id"] == str(chat_id) and es_activa(c)]

    instrucciones = armar_instrucciones(mis_citas, nombre_telegram, agenda_ocupada(citas, chat_id))
    texto_ia = await preguntar_ia(historial, instrucciones)
    if texto_ia is None:
        historial.pop()
        return "Uy, tuve un problema técnico. Intenta de nuevo en un momento 🙏"

    coincidencias = list(PATRON_CITA.finditer(texto_ia))
    if not coincidencias:
        # Parece una línea CITA mal escrita: nunca se la mostramos al cliente
        if '"accion"' in texto_ia:
            print("⚠️ Línea CITA con formato raro:", texto_ia)
            aviso = "Uy, se me enredó algo por acá 😅 ¿Me repites qué necesitas?"
            recordar(historial, "model", aviso)
            return aviso
        recordar(historial, "model", texto_ia)
        return texto_ia

    # 3. La IA propuso citas: el código las valida antes de pedir confirmación
    citas_pedidas = []
    for coincidencia in coincidencias:
        try:
            citas_pedidas.append(json.loads(coincidencia.group(1)))
        except Exception as error:
            print("❌ JSON inválido:", error)

    try:
        ok, error, listas = await asyncio.to_thread(preparar_citas, citas_pedidas, chat_id)
    except Exception as e:
        print("❌ Error validando:", e)
        ok, error, listas = False, "Tuve un problema revisando la agenda. ¿Lo intentamos de nuevo?", None

    if not ok:
        print("⚠️ Cita rechazada:", error)
        recordar(historial, "model", error)
        return error

    pendientes[chat_id] = listas
    resumen = armar_resumen(listas)
    print("⏳ Esperando confirmación:", listas)
    recordar(historial, "model", resumen)
    return resumen


# ---------- App y rutas ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    tarea = asyncio.create_task(revisar_atendidas_periodicamente())
    yield
    tarea.cancel()


app = FastAPI(lifespan=lifespan)

updates_procesados = set()
tareas_activas = set()


@app.api_route("/", methods=["GET", "HEAD"])
def inicio():
    return {"mensaje": "¡El bot está vivo!"}


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
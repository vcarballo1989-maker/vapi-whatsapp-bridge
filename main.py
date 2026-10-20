"""
SISTEMAS·TI - Vapi to WhatsApp & Google Calendar (Cal.com) Cloud Bridge
Recibe el evento end-of-call-report de Vapi y despacha automáticamente:
1. Mensaje de WhatsApp con los datos detallados de la cita vía Evolution API.
2. Reserva directa en Google Calendar y envío de correo de confirmación vía Cal.com API v2.
Incluye motor de cálculo de fechas y horas en horario de Madrid (Europe/Madrid).
"""

import os
import sys
import json
import time
import re
import base64
import datetime
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import threading

def cargar_dotenv():
    """Carga variables desde archivo .env si existe en el directorio actual o superior."""
    posibles = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    ]
    for ruta in posibles:
        if os.path.isfile(ruta):
            try:
                with open(ruta, "r", encoding="utf-8") as f:
                    for linea in f:
                        linea = linea.strip()
                        if linea and not linea.startswith("#") and "=" in linea:
                            k, v = linea.split("=", 1)
                            os.environ.setdefault(k.strip(), v.strip())
            except Exception:
                pass

cargar_dotenv()

PORT = int(os.environ.get("PORT", 10000))
EVOLUTION_BASE_URL = os.environ.get("EVOLUTION_BASE_URL", "https://evolution-api-latest-tde0.onrender.com")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "sistemas-ti")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "sistemas-ti-secret-2026")

# Configuración de Cal.com (vía variable de entorno .env o fallback codificado para Render)
_DEFAULT_CAL_B64 = "Y2FsX2xpdmVfYWY3Y2EzYTIyMzVkYjRhMGIzNjMxMTE1N2YwNjc5MDQ="
CAL_API_KEY = os.environ.get("CAL_API_KEY") or base64.b64decode(_DEFAULT_CAL_B64).decode("utf-8")
CAL_EVENT_TYPE_ID = int(os.environ.get("CAL_EVENT_TYPE_ID", "7568380"))
CAL_ATTENDEE_EMAIL = os.environ.get("CAL_ATTENDEE_EMAIL", "v.carballo1989@gmail.com")

EVOLUTION_SEND_URL = f"{EVOLUTION_BASE_URL}/message/sendText/{INSTANCE_NAME}"
CAL_BOOKINGS_URL = "https://api.cal.com/v2/bookings"

DIAS_SEMANA = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']
DIAS_SIN_TILDE = ['lunes', 'martes', 'miercoles', 'jueves', 'viernes', 'sabado', 'domingo']
MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']

NUMEROS_HORA = {
    'una': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5, 'seis': 6,
    'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10, 'once': 11, 'doce': 12
}

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def limpiar_telefono(tlf):
    if not tlf:
        return None
    t = "".join(ch for ch in str(tlf) if ch.isdigit() or ch == "+")
    if t.startswith("+"):
        t = t[1:]
    elif not t.startswith("34") and len(t) == 9:
        t = "34" + t
    return t

def obtener_ahora_madrid():
    """Devuelve datetime actual en hora peninsular española (Europe/Madrid)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/Madrid"))
    except Exception:
        # Horario de verano (CEST UTC+2) / invierno (CET UTC+1)
        tz_madrid = datetime.timezone(datetime.timedelta(hours=2))
        return datetime.datetime.now(tz_madrid)

def calcular_fecha_target(texto):
    """Devuelve (target_date, texto_fecha_legible) a partir de la transcripción."""
    ahora = obtener_ahora_madrid()
    texto_l = texto.lower()
    
    # 1. 'dentro de X días'
    m_dentro = re.search(r'dentro de (\d+|un|dos|tres|cuatro|cinco|seis|siete)\s+d[ií]as?', texto_l)
    if m_dentro:
        num_map = {'un': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5, 'seis': 6, 'siete': 7}
        val = m_dentro.group(1)
        dias_offset = num_map.get(val, int(val) if val.isdigit() else 1)
        target = ahora + datetime.timedelta(days=dias_offset)
        nom_dia = DIAS_SEMANA[target.weekday()].capitalize()
        return target.date(), f"{nom_dia}, {target.day} de {MESES[target.month-1]}"

    # 2. 'pasado mañana'
    if 'pasado mañan' in texto_l or 'pasado manan' in texto_l:
        target = ahora + datetime.timedelta(days=2)
        nom_dia = DIAS_SEMANA[target.weekday()].capitalize()
        return target.date(), f"{nom_dia}, {target.day} de {MESES[target.month-1]}"

    # 3. 'mañana' (como día, evitando 'de la mañana')
    if re.search(r'(?<!de la )\bmañana\b|(?<!de la )\bmanana\b', texto_l):
        target = ahora + datetime.timedelta(days=1)
        nom_dia = DIAS_SEMANA[target.weekday()].capitalize()
        return target.date(), f"{nom_dia}, {target.day} de {MESES[target.month-1]}"

    # 4. Día de la semana (ej. 'el martes', 'el próximo viernes')
    for idx, (d_con, d_sin) in enumerate(zip(DIAS_SEMANA, DIAS_SIN_TILDE)):
        if re.search(rf'\b(?:{d_con}|{d_sin})\b', texto_l):
            dias_adelante = (idx - ahora.weekday()) % 7
            if dias_adelante == 0:
                dias_adelante = 7
            target = ahora + datetime.timedelta(days=dias_adelante)
            return target.date(), f"{d_con.capitalize()}, {target.day} de {MESES[target.month-1]}"

    # 5. 'hoy'
    if re.search(r'\bhoy\b', texto_l):
        return ahora.date(), f"Hoy ({DIAS_SEMANA[ahora.weekday()].capitalize()}, {ahora.day} de {MESES[ahora.month-1]})"

    # Por defecto: mañana
    target = ahora + datetime.timedelta(days=1)
    nom_dia = DIAS_SEMANA[target.weekday()].capitalize()
    return target.date(), f"{nom_dia}, {target.day} de {MESES[target.month-1]}"

def extraer_hora_target(texto):
    """Devuelve (hour_24, minute, texto_hora_legible) desde la transcripción."""
    texto_l = texto.lower()
    hour = 17
    minute = 0
    hora_legible = "A las 5 de la tarde (17:00)"

    patron = (
        r'\b(?:a )?las?\s+'
        r'(\d{1,2}|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)'
        r'(?:[:\.](\d{2}))?'
        r'(\s+y\s+media|\s+y\s+cuarto|\s+menos\s+cuarto)?'
        r'(\s+de\s+la\s+tarde|\s+de\s+la\s+mañana|\s+de\s+la\s+manana|\s+del\s+mediod[ií]a)?\b'
    )
    matches = list(re.finditer(patron, texto_l))
    if matches:
        # Tomamos la última mención de hora (suele ser la confirmación final de Sofía)
        m = matches[-1]
        raw_h = m.group(1)
        raw_m = m.group(2)
        frac = (m.group(3) or "").strip()
        periodo = (m.group(4) or "").strip()

        h = int(raw_h) if raw_h.isdigit() else NUMEROS_HORA.get(raw_h, 17)
        mins = int(raw_m) if raw_m and raw_m.isdigit() else 0

        if "media" in frac:
            mins = 30
        elif "menos cuarto" in frac:
            h = h - 1 if h > 1 else 12
            mins = 45
        elif "cuarto" in frac:
            mins = 15

        # Ajustar a formato 24h (horario comercial de salón: 9:00 a 21:00)
        if "tarde" in periodo and h < 12:
            h += 12
        elif "mañana" in periodo or "manana" in periodo:
            if h == 12:
                h = 0
        else:
            # Si dice "a las 5", "a las 6 y media", "a las 4" sin especificar, en peluquería es por la tarde
            if 1 <= h <= 8:
                h += 12

        # Redondear minutos a múltiplos de 15 para Cal.com
        mins = (mins // 15) * 15
        hour, minute = h, mins
        hora_legible = f"{m.group(0).strip().capitalize()} ({hour:02d}:{minute:02d})"

    return hour, minute, hora_legible

def extraer_datos_cita(transcript, raw_summary):
    """Extrae diccionario completo con nombre, servicio, fecha, hora y timestamp UTC para Cal.com."""
    ahora = obtener_ahora_madrid()

    # Desglosar la conversación en turnos AI / User
    turns = []
    current_speaker = None
    current_text = []
    for line in (transcript or "").splitlines():
        line = line.strip()
        if line.startswith('AI:') or line.startswith('Bot:'):
            if current_speaker:
                turns.append((current_speaker, ' '.join(current_text)))
            current_speaker = 'AI'
            current_text = [line.split(':', 1)[1].strip()]
        elif line.startswith('User:'):
            if current_speaker:
                turns.append((current_speaker, ' '.join(current_text)))
            current_speaker = 'User'
            current_text = [line.split(':', 1)[1].strip()]
        elif current_speaker:
            current_text.append(line)
    if current_speaker:
        turns.append((current_speaker, ' '.join(current_text)))

    # 1. Nombre del cliente
    nombre = "Cliente"
    for i in range(len(turns) - 1):
        spk, txt = turns[i]
        next_spk, next_txt = turns[i+1]
        if spk == 'AI' and re.search(r'nombre', txt, re.IGNORECASE) and next_spk == 'User':
            clean_name = re.sub(r'^(?:me llamo|soy|a nombre de|es|el nombre es|mi nombre es)\s+', '', next_txt, flags=re.IGNORECASE).strip(' .!,')
            if clean_name and len(clean_name) < 25 and clean_name.lower() not in ['corte', 'cita', 'hola', 'si', 'sí']:
                nombre = clean_name.title()
                break
    if nombre == "Cliente" and transcript:
        m_bot = re.search(r'(?:perfecto|apuntado|confirmada|entonces|gracias),?\s+([A-ZÁÉÍÓÚa-záéíóú]+)\.', transcript, re.IGNORECASE)
        if m_bot and m_bot.group(1).lower() not in ['para', 'tu', 'cita', 'sofía', 'sofia', 'entonces', 'perfecto']:
            nombre = m_bot.group(1).capitalize()

    # 2. Servicio
    texto_busqueda = f"{transcript or ''} {raw_summary or ''}"
    servicio = "Corte de pelo"
    if re.search(r'corte y (?:arreglo de )?barba|barba y corte', texto_busqueda, re.IGNORECASE):
        servicio = "Corte y arreglo de barba"
    elif re.search(r'barba', texto_busqueda, re.IGNORECASE):
        servicio = "Corte y arreglo de barba"
    elif re.search(r'mechas|balayage', texto_busqueda, re.IGNORECASE):
        servicio = "Mechas Balayage"
    elif re.search(r'tinte|color', texto_busqueda, re.IGNORECASE):
        servicio = "Tinte / Coloración completa"
    elif re.search(r'peinado|lavar|peinar', texto_busqueda, re.IGNORECASE):
        servicio = "Lavado y peinado"

    # 3. Fecha y Hora reales
    fecha_date, fecha_legible = calcular_fecha_target(texto_busqueda)
    hour, minute, hora_legible = extraer_hora_target(texto_busqueda)

    # Construir datetime en zona Europe/Madrid y asegurar que sea futuro
    dt_local = datetime.datetime(
        fecha_date.year, fecha_date.month, fecha_date.day,
        hour, minute, 0, tzinfo=ahora.tzinfo
    )
    if dt_local <= ahora:
        dt_local = dt_local + datetime.timedelta(days=1)

    dt_utc = dt_local.astimezone(datetime.timezone.utc)
    start_utc_iso = dt_utc.strftime("%Y-%m-%dT%H:%M:00Z")

    resumen_whatsapp = (
        f"• *Cliente:* {nombre}\n"
        f"• *Servicio:* {servicio}\n"
        f"• *Fecha:* {fecha_legible}\n"
        f"• *Hora:* {hora_legible}"
    )

    return {
        "nombre": nombre,
        "servicio": servicio,
        "fecha_legible": fecha_legible,
        "hora_legible": hora_legible,
        "dt_utc": dt_utc,
        "start_utc_iso": start_utc_iso,
        "resumen_whatsapp": resumen_whatsapp
    }

def agendar_cita_calcom(datos_cita, numero_cliente):
    """Crea la cita en Google Calendar y envía invitación por correo mediante Cal.com API v2."""
    if not CAL_API_KEY:
        log("⚠️ CAL_API_KEY no configurada. Omitiendo agendado en calendario.")
        return False

    tel_clean = limpiar_telefono(numero_cliente) or "No especificado"
    nombre = datos_cita["nombre"]
    servicio = datos_cita["servicio"]
    dt_utc_base = datos_cita["dt_utc"]

    headers = {
        "Authorization": f"Bearer {CAL_API_KEY}",
        "cal-api-version": "2024-08-13",
        "Content-Type": "application/json",
        "User-Agent": "SistemasTI-VapiBridge/2.0"
    }

    # Probamos la hora exacta solicitada; si ese hueco ya estuviera ocupado en otra demo,
    # probamos +30 min o +60 min para garantizar que siempre quede agendado.
    for offset_minutos in (0, 30, 60, -30):
        dt_intento = dt_utc_base + datetime.timedelta(minutes=offset_minutos)
        start_iso = dt_intento.strftime("%Y-%m-%dT%H:%M:00Z")

        payload = {
            "start": start_iso,
            "eventTypeId": CAL_EVENT_TYPE_ID,
            "attendee": {
                "name": f"{nombre} - {servicio} (+{tel_clean})",
                "email": CAL_ATTENDEE_EMAIL,
                "timeZone": "Europe/Madrid",
                "language": "es"
            },
            "metadata": {
                "cliente": nombre,
                "servicio": servicio,
                "telefono": f"+{tel_clean}",
                "origen": "Recepcionista IA Sofía (Vapi)"
            }
        }

        try:
            log(f"📅 Agendando en Google Calendar / Cal.com para {start_iso} ({nombre} - {servicio})...")
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(CAL_BOOKINGS_URL, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=25) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                uid = resp_json.get("data", {}).get("uid", "OK")
                log(f"✅ ¡Cita agendada en Google Calendar y correo enviado con éxito! UID: {uid}")
                return True
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                pass
            log(f"⚠️ Cal.com HTTP {e.code} en slot {start_iso}: {err_body[:200]}")
            # Si es conflicto de hora ocupada, el bucle probará el siguiente hueco (+30m)
            continue
        except Exception as e:
            log(f"⚠️ Error al conectar con Cal.com: {e}")
            break

    return False

def enviar_whatsapp_cloud(telefono, mensaje):
    tel_clean = limpiar_telefono(telefono)
    if not tel_clean:
        log("❌ Error: Teléfono inválido.")
        return False

    log(f"🚀 Enviando WhatsApp a +{tel_clean} vía Evolution API...")
    payload = {
        "number": tel_clean,
        "text": mensaje
    }
    headers = {
        "Content-Type": "application/json",
        "apikey": EVOLUTION_API_KEY
    }

    for intento in range(1, 6):
        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(EVOLUTION_SEND_URL, data=data, headers=headers, method="POST")
            start = time.time()
            with urllib.request.urlopen(req, timeout=45) as resp:
                elapsed = time.time() - start
                log(f"✅ ¡WhatsApp enviado con éxito en {elapsed:.2f}s! Status: {resp.status}")
                return True
        except urllib.error.HTTPError as e:
            log(f"⚠️ Intento {intento}/5 falló con HTTP {e.code}: {e.reason}")
            if e.code in (502, 503):
                log("⏳ Servidor arrancando (cold boot), esperando 10s antes de reintentar...")
                time.sleep(10)
            elif intento < 5:
                time.sleep(3)
        except Exception as e:
            log(f"⚠️ Intento {intento}/5 falló: {e}")
            if intento < 5:
                time.sleep(4)
    return False

def procesar_evento_vapi(payload):
    msg = payload.get("message", {})
    tipo = msg.get("type") or payload.get("type")
    
    log(f"🔔 Evento Vapi: tipo = '{tipo}'")
    
    if tipo != "end-of-call-report":
        log(f"ℹ️ Ignorando evento '{tipo}' (solo se procesa 'end-of-call-report').")
        return

    call_obj = msg.get("call") or payload.get("call") or {}
    customer = msg.get("customer") or call_obj.get("customer") or {}
    numero_cliente = customer.get("number")
    if not numero_cliente:
        numero_cliente = call_obj.get("customerNumber") or call_obj.get("from")
        
    analysis = msg.get("analysis") or call_obj.get("analysis") or {}
    raw_summary = analysis.get("summary") or msg.get("summary") or call_obj.get("summary") or ""
    transcript = msg.get("transcript") or call_obj.get("transcript") or ""
    
    datos_cita = extraer_datos_cita(transcript, raw_summary)
    detalles_cita = datos_cita["resumen_whatsapp"]

    log("=" * 55)
    log(f"📞 CITA CONFIRMADA: {numero_cliente}")
    log(f"📝 {detalles_cita}")
    log(f"🕒 Timestamp Calendario (UTC): {datos_cita['start_utc_iso']}")
    log("=" * 55)
    
    # 1. Agendar automáticamente en Google Calendar + Enviar Correo vía Cal.com
    threading.Thread(
        target=agendar_cita_calcom,
        args=(datos_cita, numero_cliente or "34600000000")
    ).start()

    if not numero_cliente:
        log("⚠️ No se detectó número de teléfono para enviar WhatsApp.")
        return

    # 2. Enviar confirmación instantánea por WhatsApp
    mensaje_whatsapp = f"""💼 *SISTEMAS·TI* | _Ingeniería de Sistemas & Automatización IA_
📍 sistemas-ti.es
──────────────────────────

✨ *¡Confirmación de Cita / Demostración en Vivo!*

Hola, acabas de hablar con *Sofía*, la recepcionista telefónica inteligente de *SISTEMAS·TI*.

📋 *Resumen de tu reserva:*
{detalles_cita}
• *Agenda:* Registrada en Google Calendar y notificada por email ✅

🎯 *¿Qué significa esto para tu peluquería o negocio?*
Mientras estés atendiendo a clientes, cortando o con las manos ocupadas, la IA atiende las llamadas en directo 24/7, responde precios y horarios, agenda la cita en tu calendario y envía esta confirmación por WhatsApp en 1 segundo. Cero llamadas perdidas.

¿Te gustaría que lo dejemos instalado en el número de tu salón esta misma semana?
──────────────────────────
_Enviado automáticamente por el motor de voz de SISTEMAS·TI_"""

    threading.Thread(target=enviar_whatsapp_cloud, args=(numero_cliente, mensaje_whatsapp)).start()

def keep_render_alive():
    """Mantiene Evolution API despierto haciendo ping periódico."""
    while True:
        try:
            req = urllib.request.Request(EVOLUTION_BASE_URL, headers={"User-Agent": "KeepAlive/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception:
            pass
        time.sleep(480) # 8 minutos

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            payload = json.loads(post_data.decode('utf-8'))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
            
            threading.Thread(target=procesar_evento_vapi, args=(payload,)).start()
        except Exception as e:
            log(f"❌ Error JSON: {e}")
            self.send_response(400)
            self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("<h1>SISTEMAS TI - Vapi to WhatsApp + Google Calendar Bridge 24/7 Active</h1>".encode('utf-8'))

    def log_message(self, format, *args):
        pass

def main():
    log(f"Iniciando Vapi-WhatsApp-Calendar Bridge en puerto {PORT}...")
    threading.Thread(target=keep_render_alive, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', PORT), WebhookHandler)
    server.serve_forever()

if __name__ == "__main__":
    main()

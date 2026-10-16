"""
SISTEMAS·TI - Vapi to WhatsApp Cloud Bridge (Render Deployment)
Recibe el evento end-of-call-report de Vapi y despacha automáticamente
el mensaje de WhatsApp con los datos detallados de la cita vía Evolution API.
"""

import os
import sys
import json
import time
import re
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import threading

PORT = int(os.environ.get("PORT", 10000))
EVOLUTION_BASE_URL = os.environ.get("EVOLUTION_BASE_URL", "https://evolution-api-latest-tde0.onrender.com")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "sistemas-ti")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "sistemas-ti-secret-2026")

EVOLUTION_SEND_URL = f"{EVOLUTION_BASE_URL}/message/sendText/{INSTANCE_NAME}"

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

def extraer_detalles_cita(transcript, raw_summary):
    """Extrae de forma limpia y desglosada: Cliente, Servicio, Fecha y Hora."""
    if raw_summary and len(raw_summary.strip()) > 20:
        return raw_summary.strip()
    
    if not transcript:
        return "• *Estado:* Cita confirmada con el salón"
        
    nombres_prohibidos = {'sofía', 'sofia', 'estilo', 'peluquería', 'peluqueria', 'gracias', 'hola', 'para', 'cita', 'buenos', 'días', 'tardes'}
    
    # 1. Nombre del cliente
    nombre = "Cliente"
    m_user = re.search(r'User:[^\n]*(?:me llamo|soy|a nombre de)\s+([A-ZÁÉÍÓÚa-záéíóú]+)', transcript, re.IGNORECASE)
    if m_user and m_user.group(1).lower() not in nombres_prohibidos:
        nombre = m_user.group(1).capitalize()
    else:
        m_line = re.search(r'User:\s*([A-ZÁÉÍÓÚa-záéíóú]{2,15})\.?\s*$', transcript, re.MULTILINE)
        if m_line and m_line.group(1).lower() not in nombres_prohibidos:
            nombre = m_line.group(1).capitalize()
        else:
            m_bot = re.search(r'(?:perfecto|apuntado|confirmada|entonces|estupendo),?\s+([A-ZÁÉÍÓÚa-záéíóú]+)', transcript, re.IGNORECASE)
            if m_bot and m_bot.group(1).lower() not in nombres_prohibidos:
                nombre = m_bot.group(1).capitalize()

    # 2. Servicio
    servicio = "Corte de pelo"
    if re.search(r'barba', transcript, re.IGNORECASE):
        servicio = "Corte y arreglo de barba"
    elif re.search(r'mechas|balayage', transcript, re.IGNORECASE):
        servicio = "Mechas Balayage"
    elif re.search(r'tinte|color', transcript, re.IGNORECASE):
        servicio = "Tinte / Coloración completa"
    elif re.search(r'peinado|lavar|peinar', transcript, re.IGNORECASE):
        servicio = "Lavado y peinado"

    # 3. Día (priorizar días específicos sobre el 'hoy' de saludo)
    dia = "Hoy"
    dias = re.findall(r'\b(pasado mañana|mañana|lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|hoy)\b', transcript, re.IGNORECASE)
    for d in dias:
        if d.lower() != 'hoy':
            dia = d.capitalize()
            break

    # 4. Hora
    hora = "A convenir"
    m_hora = re.search(r'\b((?:a )?las \d+([:\.]\d+)?(?:\s+y media|\s+y cuarto)?(?:\s+de la tarde|\s+de la mañana|\s+del mediodía)?|(?:a )?las (?:una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)(?:\s+y media|\s+y cuarto)?(?:\s+de la tarde|\s+de la mañana)?)\b', transcript, re.IGNORECASE)
    if m_hora:
        hora = m_hora.group(0).capitalize()
    elif re.search(r'6 y media|seis y media', transcript, re.IGNORECASE):
        hora = "A las 6 y media de la tarde"
    elif re.search(r'5 de la tarde|cinco de la tarde', transcript, re.IGNORECASE):
        hora = "A las 5 de la tarde"

    return f"• *Cliente:* {nombre}\n• *Servicio:* {servicio}\n• *Fecha:* {dia}\n• *Hora:* {hora}"

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

    # Reintentos con tolerancia a cold start (502 Bad Gateway / arranque de Render)
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
            if e.code == 502 or e.code == 503:
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
    
    detalles_cita = extraer_detalles_cita(transcript, raw_summary)

    log("=" * 50)
    log(f"📞 CITA CONFIRMADA: {numero_cliente}")
    log(f"📝 {detalles_cita}")
    log("=" * 50)
    
    if not numero_cliente:
        log("⚠️ No se detectó número de teléfono.")
        return

    mensaje_whatsapp = f"""💼 *SISTEMAS·TI* | _Ingeniería de Sistemas & Automatización IA_
📍 sistemas-ti.es
──────────────────────────

✨ *¡Confirmación de Cita / Demostración en Vivo!*

Hola, acabas de hablar con *Sofía*, la recepcionista telefónica inteligente de *SISTEMAS·TI*.

📋 *Resumen de tu reserva:*
{detalles_cita}

🎯 *¿Qué significa esto para tu peluquería o negocio?*
Mientras estés atendiendo a clientes, cortando o con las manos ocupadas, la IA atiende las llamadas en directo 24/7, responde precios y horarios, agenda la cita y envía esta confirmación por WhatsApp en 1 segundo. Cero llamadas perdidas.

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
        self.wfile.write("<h1>SISTEMAS TI - Vapi to WhatsApp Bridge 24/7 Active</h1>".encode('utf-8'))

    def log_message(self, format, *args):
        pass

def main():
    log(f"Iniciando Vapi-WhatsApp Bridge en puerto {PORT}...")
    threading.Thread(target=keep_render_alive, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', PORT), WebhookHandler)
    server.serve_forever()

if __name__ == "__main__":
    main()

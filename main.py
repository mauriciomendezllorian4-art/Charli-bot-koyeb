import os
import sqlite3
import requests
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GROQ_KEY = os.getenv("GROQ_API_KEY", "")
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")

DB_PATH = os.getenv("DATABASE_PATH", "/var/data/chali.db")
if not os.path.isdir(os.path.dirname(DB_PATH)):
    DB_PATH = "/tmp/chali.db"

ia = Groq(api_key=GROQ_KEY) if GROQ_KEY else None


def conectar():
    carpeta = os.path.dirname(DB_PATH)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    return sqlite3.connect(DB_PATH, timeout=20)


def iniciar():
    with conectar() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS mensajes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat TEXT, rol TEXT, texto TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS recuerdos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat TEXT, texto TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS operaciones(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat TEXT, resultado TEXT)""")


iniciar()


def guardar(chat, rol, texto):
    with conectar() as db:
        db.execute(
            "INSERT INTO mensajes(chat,rol,texto) VALUES(?,?,?)",
            (str(chat), rol, texto[:8000])
        )


def historial(chat):
    with conectar() as db:
        filas = db.execute(
            "SELECT rol,texto FROM mensajes WHERE chat=? "
            "ORDER BY id DESC LIMIT 12",
            (str(chat),)
        ).fetchall()
    return [{"role": r, "content": t} for r, t in reversed(filas)]


def recuerdos(chat):
    with conectar() as db:
        filas = db.execute(
            "SELECT id,texto FROM recuerdos WHERE chat=? "
            "ORDER BY id DESC LIMIT 20",
            (str(chat),)
        ).fetchall()
    if not filas:
        return "Sin recuerdos guardados."
    return "\n".join(f"{i}. {t}" for i, t in filas)


def enviar(chat, texto, teclado=None):
    if not TOKEN:
        print("Falta TELEGRAM_TOKEN")
        return
    datos = {"chat_id": chat, "text": str(texto)[:4000]}
    if teclado:
        datos["reply_markup"] = teclado
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json=datos, timeout=20
        )
        print("Telegram:", r.status_code, r.text[:200])
    except Exception as e:
        print("Telegram error:", e)


def preguntar(chat, texto):
    if not ia:
        return "Configura GROQ_API_KEY en Render."

    instrucciones = """
    Eres CHALI IA, asistente personal en español.
    Responde preguntas generales y ayuda con trading.
    Analiza SMC, BOS, CHOCH, liquidez, Order Blocks,
    Fair Value Gaps, soportes, resistencias y riesgo.
    No inventes precios ni garantices ganancias.
    No ejecutes operaciones reales.
    Usa el historial y los recuerdos para contextualizar.
    """

    mensajes = [{
        "role": "system",
        "content": instrucciones + "\nRecuerdos:\n" + recuerdos(chat)
    }]
    mensajes.extend(historial(chat))
    mensajes.append({"role": "user", "content": texto[:5000]})

    try:
        r = ia.chat.completions.create(
            model=MODEL,
            messages=mensajes,
            temperature=0.3,
            max_tokens=1000
        )
        respuesta = r.choices[0].message.content or "Sin respuesta."
        guardar(chat, "user", texto)
        guardar(chat, "assistant", respuesta)
        return respuesta
    except Exception as e:
        print("Groq error:", repr(e))
        return "Error de Groq. Revisa la API key y el modelo en Render."


def procesar(chat, texto):
    texto = texto.strip()
    bajo = texto.lower()

    if bajo.startswith("/start"):
        return (
            "CHALI IA V20 ONLINE\n\n"
            "Puedo responder preguntas y ayudarte con trading.\n\n"
            "/memoria - ver recuerdos\n"
            "/recordar texto - guardar recuerdo\n"
            "/olvidar - borrar recuerdos\n"
            "/btc - analizar BTC\n"
            "/ayuda - ver comandos"
        )

    if bajo.startswith("/ayuda"):
        return (
            "/memoria: ver recuerdos\n"
            "/recordar texto: guardar recuerdo\n"
            "/olvidar: borrar recuerdos\n"
            "/btc: analizar BTC\n"
            "También puedes escribirme cualquier pregunta."
        )

    if bajo.startswith("/recordar "):
        recuerdo = texto[len("/recordar "):].strip()
        if not recuerdo:
            return "Escribe: /recordar tu recuerdo"
        with conectar() as db:
            db.execute(
                "INSERT INTO recuerdos(chat,texto) VALUES(?,?)",
                (str(chat), recuerdo[:1500])
            )
        return "Recuerdo guardado."

    if bajo.startswith("/memoria"):
        return recuerdos(chat)

    if bajo.startswith("/olvidar"):
        with conectar() as db:
            db.execute(
                "DELETE FROM recuerdos WHERE chat=?", (str(chat),)
            )
        return "Recuerdos borrados."

    if bajo.startswith("/btc"):
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "15m",
                    "limit": 80
                },
                timeout=15
            )
            r.raise_for_status()
            velas = r.json()
            cierres = [float(v[4]) for v in velas]
            precio = cierres[-1]
            ema20 = sum(cierres[-20:]) / 20
            ema50 = sum(cierres[-50:]) / 50
            maximo = max(float(v[2]) for v in velas[-21:-1])
            minimo = min(float(v[3]) for v in velas[-21:-1])

            if precio > ema20 > ema50:
                tendencia = "ALCISTA"
            elif precio < ema20 < ema50:
                tendencia = "BAJISTA"
            else:
                tendencia = "LATERAL / MIXTA"

            return (
                "CHALI IA - ANALISIS BTCUSDT\n"
                "Temporalidad: 15 minutos\n"
                f"Precio actual consultado: {precio:.2f} USDT\n"
                f"Tendencia por medias: {tendencia}\n"
                f"EMA20 aproximada: {ema20:.2f}\n"
                f"EMA50 aproximada: {ema50:.2f}\n"
                f"Maximo reciente: {maximo:.2f}\n"
                f"Minimo reciente: {minimo:.2f}\n\n"
                "Esto es un análisis básico, no una orden de compra "
                "o venta. Confirma estructura, liquidez y riesgo antes "
                "de operar."
            )
        except Exception as e:
            print("BTC error:", repr(e))
            return "No pude obtener datos de BTC. Intenta nuevamente."

    return preguntar(chat, texto)


@app.route("/", methods=["GET"])
def inicio():
    return "CHALI IA V20 ONLINE", 200


@app.route("/health", methods=["GET"])
def salud():
    return {"status": "online", "bot": "CHALI IA V20"}, 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "CHALI webhook activo", 200

    data = request.get_json(silent=True) or {}

    try:
        if "callback_query" in data:
            cb = data["callback_query"]
            chat = cb.get("message", {}).get("chat", {}).get("id")
            resultado = cb.get("data", "")
            if chat and resultado in ("ganada", "perdida"):
                with conectar() as db:
                    db.execute(
                        "INSERT INTO operaciones(chat,resultado) VALUES(?,?)",
                        (str(chat), resultado)
                    )
                requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
                    json={
                        "callback_query_id": cb["id"],
                        "text": "Resultado guardado: " + resultado
                    },
                    timeout=10
                )
            return "OK", 200

        mensaje = data.get("message", {})
        chat = mensaje.get("chat", {}).get("id")
        usuario = mensaje.get("from", {}).get("id")
        texto = mensaje.get("text", "")

        if not chat or not texto:
            return "OK", 200

        if ADMIN_ID and str(usuario) != ADMIN_ID:
            enviar(chat, "Acceso no autorizado.")
            return "OK", 200

        respuesta = procesar(chat, texto)
        enviar(chat, respuesta)

    except Exception as e:
        print("Webhook error:", repr(e))

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

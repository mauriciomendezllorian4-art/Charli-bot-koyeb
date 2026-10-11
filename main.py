import os
import sqlite3
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

# CONFIGURACION
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GROQ_KEY = os.getenv("GROQ_API_KEY", "")
TD_KEY = os.getenv("TWELVEDATA_API_KEY", "")
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")

DB_PATH = os.getenv("DATABASE_PATH", "/var/data/chali.db")
if not os.path.isdir(os.path.dirname(DB_PATH)):
    DB_PATH = "/tmp/chali.db"

ia = Groq(api_key=GROQ_KEY) if GROQ_KEY else None


# BASE DE DATOS Y MEMORIA
def conectar():
    carpeta = os.path.dirname(DB_PATH)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    return sqlite3.connect(DB_PATH, timeout=20)


def iniciar():
    with conectar() as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS mensajes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat TEXT, rol TEXT, texto TEXT)
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS recuerdos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat TEXT, texto TEXT)
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS resultados(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat TEXT, resultado TEXT)
        """)


iniciar()


def guardar(chat, rol, texto):
    with conectar() as db:
        db.execute(
            "INSERT INTO mensajes(chat,rol,texto) VALUES(?,?,?)",
            (str(chat), rol, texto[:6000])
        )


def memoria(chat):
    with conectar() as db:
        filas = db.execute(
            "SELECT id,texto FROM recuerdos WHERE chat=? "
            "ORDER BY id DESC LIMIT 20", (str(chat),)
        ).fetchall()
    return "\n".join(f"{i}. {t}" for i, t in filas) or "Sin recuerdos."


def historial(chat):
    with conectar() as db:
        filas = db.execute(
            "SELECT rol,texto FROM mensajes WHERE chat=? "
            "ORDER BY id DESC LIMIT 10", (str(chat),)
        ).fetchall()
    return [{"role": r, "content": t} for r, t in reversed(filas)]


def enviar(chat, texto):
    if not TOKEN:
        print("Falta TELEGRAM_TOKEN")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat, "text": str(texto)[:4000]},
            timeout=20
        )
        r.raise_for_status()
    except Exception as e:
        print("Telegram:", repr(e))


def enviar_grafico(chat, texto, archivo):
    try:
        with open(archivo, "rb") as foto:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                data={"chat_id": chat, "caption": texto[:1000]},
                files={"photo": foto},
                timeout=30
            )
        r.raise_for_status()
        enviar(chat, texto)  # Envia el informe completo
    except Exception as e:
        print("Error grafico:", repr(e))
        enviar(chat, texto)


# IA GENERAL
def preguntar(chat, pregunta):
    if not ia:
        return "Falta GROQ_API_KEY en las variables de Render."

    sistema = """
Eres CHALI IA, asistente personal en español.
Ayudas con trading y preguntas generales.
Analiza SMC, estructura, BOS, CHOCH, liquidez,
Order Blocks, FVG, soportes, resistencias y riesgo.
No inventes cotizaciones ni garantices ganancias.
No afirmes haber ejecutado operaciones.
La memoria aporta contexto, no entrena automaticamente el modelo.
"""

    mensajes = [{
        "role": "system",
        "content": sistema + "\nMemoria del usuario:\n" + memoria(chat)
    }]
    mensajes.extend(historial(chat))
    mensajes.append({"role": "user", "content": pregunta[:4000]})

    try:
        r = ia.chat.completions.create(
            model=MODEL,
            messages=mensajes,
            temperature=0.3,
            max_tokens=1000
        )
        respuesta = r.choices[0].message.content or "Sin respuesta."
        guardar(chat, "user", pregunta)
        guardar(chat, "assistant", respuesta)
        return respuesta
    except Exception as e:
        print("Groq:", repr(e))
        return "Error de Groq. Revisa la API key y el modelo."


# OBTENER VELAS DE MERCADO
def obtener_velas(mercado):
    if mercado == "BTC":
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "15m", "limit": 100},
            timeout=20
        )
        r.raise_for_status()
        datos = r.json()
        velas = [{
            "o": float(x[1]), "h": float(x[2]),
            "l": float(x[3]), "c": float(x[4])
        } for x in datos]
        return velas, "BTCUSDT", "USDT"

    if mercado == "ORO":
        if not TD_KEY:
            raise ValueError("Falta TWELVEDATA_API_KEY en Render.")

        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": "XAU/USD",
                "interval": "15min",
                "outputsize": 100,
                "apikey": TD_KEY
            },
            timeout=25
        )
        r.raise_for_status()
        datos = r.json()

        if "values" not in datos:
            raise ValueError(datos.get("message", "Error Twelve Data."))

        velas = [{
            "o": float(x["open"]), "h": float(x["high"]),
            "l": float(x["low"]), "c": float(x["close"])
        } for x in reversed(datos["values"])]

        return velas, "XAU/USD", "USD"

    raise ValueError("Mercado desconocido.")


# ANALISIS SMC
def analizar(mercado):
    velas, nombre, moneda = obtener_velas(mercado)

    if len(velas) < 55:
        raise ValueError("Faltan velas para el analisis.")

    o = [v["o"] for v in velas]
    h = [v["h"] for v in velas]
    l = [v["l"] for v in velas]
    c = [v["c"] for v in velas]
    n = len(c)

    def ema(valores, periodo):
        k = 2 / (periodo + 1)
        salida = [valores[0]]
        for x in valores[1:]:
            salida.append(k * x + (1-k) * salida[-1])
        return salida

    e20 = ema(c, 20)
    e50 = ema(c, 50)

    # ATR
    tr = []
    for i in range(1, n):
        tr.append(max(
            h[i] - l[i],
            abs(h[i] - c[i-1]),
            abs(l[i] - c[i-1])
        ))
    atr = sum(tr[-14:]) / min(14, len(tr))
    precio = c[-1]

    # Pivotes confirmados
    ph, pl = [], []
    for i in range(2, n-2):
        if h[i] > max(h[i-2:i]) and h[i] >= max(h[i+1:i+3]):
            ph.append(i)
        if l[i] < min(l[i-2:i]) and l[i] <= min(l[i+1:i+3]):
            pl.append(i)

    # BOS
    bos = "Sin ruptura confirmada"
    if ph and c[-1] > h[ph[-1]]:
        bos = "BOS ALCISTA"
    elif pl and c[-1] < l[pl[-1]]:
        bos = "BOS BAJISTA"

    # CHOCH aproximado: ruptura opuesta al sesgo anterior
    choch = "Sin CHOCH detectado"
    if bos == "BOS ALCISTA" and e20[-10] < e50[-10]:
        choch = "Posible CHOCH ALCISTA"
    elif bos == "BOS BAJISTA" and e20[-10] > e50[-10]:
        choch = "Posible CHOCH BAJISTA"

    # Order Block: ultima vela contraria antes de ruptura
    ob = None
    if bos == "BOS ALCISTA":
        for i in range(n-2, max(-1, n-16), -1):
            if c[i] < o[i]:
                ob = (l[i], h[i], "OB ALCISTA")
                break
    elif bos == "BOS BAJISTA":
        for i in range(n-2, max(-1, n-16), -1):
            if c[i] > o[i]:
                ob = (l[i], h[i], "OB BAJISTA")
                break

    # Zonas de liquidez estimada: maximos/minimos repetidos
    def liquidez(indices, valores):
        if len(indices) < 2:
            return None
        recientes = [valores[i] for i in indices[-8:]]
        ultimo = recientes[-1]
        cercanos = [x for x in recientes if abs(x-ultimo) <= atr*0.35]
        return sum(cercanos)/len(cercanos) if len(cercanos) >= 2 else None

    liq_alta = liquidez(ph, h)
    liq_baja = liquidez(pl, l)

    # Rebote aproximado cerca de OB o pivotes
    rebote = "Sin rebote identificado"
    if ob and ob[0] - atr*0.2 <= precio <= ob[1] + atr*0.2:
        rebote = "Precio cerca de " + ob[2]
    elif pl and abs(precio-l[pl[-1]]) <= atr*0.3:
        rebote = "Posible rebote en soporte"
    elif ph and abs(precio-h[ph[-1]]) <= atr*0.3:
        rebote = "Posible rechazo en resistencia"

    # Senal orientativa
    direccion = "ESPERAR"
    entrada = sl = tp1 = tp2 = tp3 = None

    alcista = e20[-1] > e50[-1] and precio > e20[-1]
    bajista = e20[-1] < e50[-1] and precio < e20[-1]

    if alcista and bos != "BOS BAJISTA":
        direccion = "COMPRA POSIBLE"
        entrada = precio
        sl = entrada - 1.5*atr
        tp1 = entrada + 1.5*atr
        tp2 = entrada + 2.5*atr
        tp3 = entrada + 3.5*atr
    elif bajista and bos != "BOS ALCISTA":
        direccion = "VENTA POSIBLE"
        entrada = precio
        sl = entrada + 1.5*atr
        tp1 = entrada - 1.5*atr
        tp2 = entrada - 2.5*atr
        tp3 = entrada - 3.5*atr

    # GRAFICO DE VELAS
    archivo = f"/tmp/chali_{mercado.lower()}.png"
    fig, ax = plt.subplots(figsize=(12, 7))
    inicio = max(0, n-60)

    for i in range(inicio, n):
        color = "green" if c[i] >= o[i] else "red"
        ax.plot([i, i], [l[i], h[i]], color=color, linewidth=1)
        cuerpo = max(abs(c[i]-o[i]), precio*0.00001)
        ax.add_patch(patches.Rectangle(
            (i-0.3, min(o[i], c[i])), 0.6, cuerpo,
            facecolor=color, edgecolor=color
        ))

    ax.plot(range(inicio, n), e20[inicio:], label="EMA20")
    ax.plot(range(inicio, n), e50[inicio:], label="EMA50")

    if ob:
        ax.axhspan(ob[0], ob[1], alpha=0.2, label=ob[2])
    if liq_alta is not None:
        ax.axhline(liq_alta, linestyle=":", label="Liquidez alta")
    if liq_baja is not None:
        ax.axhline(liq_baja, linestyle=":", label="Liquidez baja")

    if entrada is not None:
        for nivel, etiqueta in [
            (entrada, "ENTRADA"), (sl, "SL"),
            (tp1, "TP1"), (tp2, "TP2"), (tp3, "TP3")
        ]:
            ax.axhline(nivel, linestyle="--", linewidth=1)
            ax.text(n-1, nivel, f" {etiqueta} {nivel:.2f}", fontsize=8)

    ax.set_title(f"CHALI IA SMC | {nombre} | 15m")
    ax.set_ylabel(moneda)
    ax.grid(True, alpha=0.2)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(archivo, dpi=120)
    plt.close(fig)

    texto = (
        f"CHALI IA SMC - {nombre}\n"
        f"Precio de referencia: {precio:.2f} {moneda}\n"
        f"Direccion: {direccion}\n"
        f"Estructura: {bos}\n"
        f"CHOCH: {choch}\n"
        f"Rebote: {rebote}\n"
        f"ATR14: {atr:.2f}\n"
    )

    if ob:
        texto += f"Order Block: {ob[2]} {ob[0]:.2f} - {ob[1]:.2f}\n"
    else:
        texto += "Order Block: no detectado\n"

    texto += (
        f"Liquidez estimada sobre maximos: "
        f"{liq_alta if liq_alta is not None else 'No detectada'}\n"
        f"Liquidez estimada bajo minimos: "
        f"{liq_baja if liq_baja is not None else 'No detectada'}\n"
    )

    if entrada is not None:
        texto += (
            f"\nEntrada orientativa: {entrada:.2f}\n"
            f"Stop Loss: {sl:.2f}\n"
            f"TP1: {tp1:.2f}\n"
            f"TP2: {tp2:.2f}\n"
            f"TP3: {tp3:.2f}\n"
        )
    else:
        texto += "\nSin entrada: esperar confirmacion.\n"

    texto += (
        "\nLas zonas son estimaciones tecnicas. No se consultan "
        "liquidaciones reales del mercado. Sin ejecucion automatica."
    )

    return texto, archivo


# WEB Y TELEGRAM
@app.route("/", methods=["GET"])
def inicio():
    return "CHALI IA ORO + BTC ONLINE", 200


@app.route("/health", methods=["GET"])
def salud():
    return {"status": "online"}, 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "CHALI webhook activo", 200

    data = request.get_json(silent=True) or {}
    mensaje = data.get("message", {})
    chat = mensaje.get("chat", {}).get("id")
    usuario = mensaje.get("from", {}).get("id")
    texto = mensaje.get("text", "").strip()

    if not chat or not texto:
        return "OK", 200

    if ADMIN_ID and str(usuario) != ADMIN_ID:
        enviar(chat, "Acceso no autorizado.")
        return "OK", 200

    bajo = texto.lower()

    try:
        if bajo.startswith("/start") or bajo.startswith("/ayuda"):
            enviar(chat,
                "CHALI IA - ORO + BTC\n\n"
                "/oro - Analizar oro XAU/USD\n"
                "/btc - Analizar BTCUSDT\n"
                "/mercados - Analizar ambos\n"
                "/recordar texto - Guardar memoria\n"
                "/memoria - Ver memoria\n"
                "/olvidar - Borrar memoria\n"
                "Tambien puedes preguntarme cualquier cosa."
            )

        elif bajo.startswith("/recordar "):
            recuerdo = texto.split(" ", 1)[1].strip()
            with conectar() as db:
                db.execute(
                    "INSERT INTO recuerdos(chat,texto) VALUES(?,?)",
                    (str(chat), recuerdo[:1500])
                )
            enviar(chat, "Recuerdo guardado.")

        elif bajo.startswith("/memoria"):
            enviar(chat, memoria(chat))

        elif bajo.startswith("/olvidar"):
            with conectar() as db:
                db.execute("DELETE FROM recuerdos WHERE chat=?",
                           (str(chat),))
            enviar(chat, "Memoria borrada.")

        elif bajo.startswith("/btc") or bajo.startswith("/oro"):
            mercado = "BTC" if bajo.startswith("/btc") else "ORO"
            enviar(chat, f"Analizando {mercado}, preparando grafico...")
            try:
                informe, grafico = analizar(mercado)
                enviar_grafico(chat, informe, grafico)
            except Exception as e:
                print("Analisis error:", repr(e))
                enviar(chat, "Error de analisis: " + str(e)[:500])

        elif bajo.startswith("/mercados"):
            for mercado in ("ORO", "BTC"):
                try:
                    enviar(chat, f"Analizando {mercado}...")
                    informe, grafico = analizar(mercado)
                    enviar_grafico(chat, informe, grafico)
                except Exception as e:
                    enviar(chat, f"Error en {mercado}: {str(e)[:300]}")

        else:
            enviar(chat, preguntar(chat, texto))

    except Exception as e:
        print("Webhook error:", repr(e))

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

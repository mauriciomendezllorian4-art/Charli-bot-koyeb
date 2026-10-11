# ============================================================
# CHALI IA - ORO + BTC - RENDER
# Telegram + Groq + memoria SQLite + gráficos + análisis SMC
# ============================================================

import os
import time
import json
import sqlite3
import logging
import threading
from datetime import datetime, timezone
from urllib.parse import quote

import requests
import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from flask import Flask, request, jsonify

# ---------------- CONFIGURACION ----------------

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL", "llama-3.3-70b-versatile"
).strip()

TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv(
    "TELEGRAM_WEBHOOK_SECRET", ""
).strip()

DATABASE_PATH = os.getenv("DATABASE_PATH", "/var/data/chali.db")
if not os.path.isdir(os.path.dirname(DATABASE_PATH) or "."):
    DATABASE_PATH = "/tmp/chali.db"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHART_DIR = "/tmp"
TIMEFRAME = "15m"

session = requests.Session()
db_lock = threading.RLock()


# ---------------- MEMORIA SQLITE ----------------

def conectar_db():
    con = sqlite3.connect(DATABASE_PATH, timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    return con


def iniciar_db():
    with db_lock:
        con = conectar_db()
        con.execute("""
            CREATE TABLE IF NOT EXISTS mensajes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                rol TEXT,
                contenido TEXT,
                fecha TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS recuerdos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                recuerdo TEXT,
                fecha TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS resultados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mercado TEXT,
                senal TEXT,
                entrada REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                fecha TEXT
            )
        """)
        con.commit()
        con.close()


def guardar_mensaje(chat_id, rol, contenido):
    try:
        with db_lock:
            con = conectar_db()
            con.execute(
                """INSERT INTO mensajes
                (chat_id, rol, contenido, fecha)
                VALUES (?, ?, ?, ?)""",
                (
                    str(chat_id),
                    rol,
                    str(contenido)[:12000],
                    datetime.now(timezone.utc).isoformat()
                )
            )
            con.commit()
            con.close()
    except Exception:
        logging.exception("Error guardando mensaje")


def guardar_recuerdo(chat_id, recuerdo):
    with db_lock:
        con = conectar_db()
        con.execute(
            """INSERT INTO recuerdos (chat_id, recuerdo, fecha)
            VALUES (?, ?, ?)""",
            (
                str(chat_id),
                recuerdo[:2000],
                datetime.now(timezone.utc).isoformat()
            )
        )
        con.commit()
        con.close()


def obtener_memoria(chat_id, limite=12):
    with db_lock:
        con = conectar_db()

        recuerdos = con.execute(
            """SELECT recuerdo FROM recuerdos
            WHERE chat_id = ? ORDER BY id DESC LIMIT 15""",
            (str(chat_id),)
        ).fetchall()

        mensajes = con.execute(
            """SELECT rol, contenido FROM mensajes
            WHERE chat_id = ? ORDER BY id DESC LIMIT ?""",
            (str(chat_id), limite)
        ).fetchall()

        con.close()

    recuerdos_texto = "\n".join(
        "- " + fila[0] for fila in reversed(recuerdos)
    )
    mensajes_texto = [
        {"role": rol, "content": contenido}
        for rol, contenido in reversed(mensajes)
        if rol in ("user", "assistant")
    ]

    return recuerdos_texto, mensajes_texto


def guardar_resultado(mercado, analisis):
    try:
        with db_lock:
            con = conectar_db()
            con.execute(
                """INSERT INTO resultados
                (mercado, senal, entrada, sl, tp1, tp2, fecha)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    mercado,
                    analisis["senal"],
                    analisis["entrada"],
                    analisis["sl"],
                    analisis["tp1"],
                    analisis["tp2"],
                    datetime.now(timezone.utc).isoformat()
                )
            )
            con.commit()
            con.close()
    except Exception:
        logging.exception("Error guardando resultado")


iniciar_db()


# ---------------- TELEGRAM ----------------

def telegram_api(metodo, datos=None, archivos=None, timeout=30):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN en Render")

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN + "/" + metodo
    )

    respuesta = session.post(
        url,
        data=datos,
        files=archivos,
        timeout=timeout
    )
    respuesta.raise_for_status()
    resultado = respuesta.json()

    if not resultado.get("ok"):
        raise RuntimeError(str(resultado))

    return resultado


def enviar_texto(chat_id, texto):
    texto = str(texto)
    if not texto:
        return

    # Telegram permite hasta 4096 caracteres por mensaje.
    partes = [
        texto[i:i + 3900]
        for i in range(0, len(texto), 3900)
    ]

    for parte in partes:
        telegram_api(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": parte
            }
        )


def enviar_grafico(chat_id, ruta, informe):
    if os.path.exists(ruta):
        with open(ruta, "rb") as foto:
            telegram_api(
                "sendPhoto",
                data={
                    "chat_id": str(chat_id),
                    "caption": informe[:950]
                },
                archivos={"photo": foto}
            )

        if len(informe) > 950:
            enviar_texto(chat_id, informe[950:])
    else:
        enviar_texto(chat_id, informe)


# ---------------- DATOS DE MERCADO ----------------

def obtener_btc():
    url = "https://api.binance.com/api/v3/klines"
    respuesta = session.get(
        url,
        params={
            "symbol": "BTCUSDT",
            "interval": TIMEFRAME,
            "limit": 180
        },
        timeout=20
    )
    respuesta.raise_for_status()
    datos = respuesta.json()

    if not isinstance(datos, list) or len(datos) < 60:
        raise ValueError("Binance no devolvió suficientes velas BTC")

    df = pd.DataFrame(datos, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    df["time"] = pd.to_datetime(
        df["time"], unit="ms", utc=True
    )

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df[["time", "open", "high", "low", "close", "volume"]].dropna()


def obtener_oro():
    if not TWELVEDATA_API_KEY:
        raise RuntimeError("Falta TWELVEDATA_API_KEY en Render")

    url = "https://api.twelvedata.com/time_series"

    respuesta = session.get(
        url,
        params={
            "symbol": "XAU/USD",
            "interval": TIMEFRAME,
            "outputsize": 180,
            "apikey": TWELVEDATA_API_KEY,
            "format": "JSON",
            "order": "ASC"
        },
        timeout=25
    )
    respuesta.raise_for_status()
    datos = respuesta.json()

    if "values" not in datos:
        raise ValueError(
            "Twelve Data: " +
            str(datos.get("message", datos.get("status", datos)))[:500]
        )

    df = pd.DataFrame(datos["values"])
    df["time"] = pd.to_datetime(df["datetime"], utc=True)

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "volume" not in df.columns:
        df["volume"] = 0
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    df = df[["time", "open", "high", "low", "close", "volume"]]
    return df.sort_values("time").dropna().reset_index(drop=True)


# ---------------- INDICADORES Y ESTRUCTURA ----------------

def calcular_indicadores(df):
    df = df.copy()

    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    delta = df["close"].diff()
    ganancias = delta.clip(lower=0)
    perdidas = -delta.clip(upper=0)

    media_g = ganancias.ewm(alpha=1/14, adjust=False).mean()
    media_p = perdidas.ewm(alpha=1/14, adjust=False).mean()

    rs = media_g / media_p.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)

    anterior = df["close"].shift(1)

    rango = pd.concat([
        df["high"] - df["low"],
        (df["high"] - anterior).abs(),
        (df["low"] - anterior).abs()
    ], axis=1).max(axis=1)

    df["atr"] = rango.ewm(span=14, adjust=False).mean()

    return df


def detectar_estructura(df):
    ult = df.iloc[-1]
    ventana = df.iloc[-21:-1]

    maximo = float(ventana["high"].max())
    minimo = float(ventana["low"].min())
    cierre = float(ult["close"])

    if cierre > maximo:
        bos = "BOS ALCISTA"
    elif cierre < minimo:
        bos = "BOS BAJISTA"
    else:
        bos = "Sin ruptura confirmada"

    # CHOCH aproximado: cambio de dirección respecto a las EMAs.
    previo = df.iloc[-2]
    tendencia_previa = (
        "ALCISTA" if previo["ema20"] > previo["ema50"] else "BAJISTA"
    )
    tendencia_actual = (
        "ALCISTA" if ult["ema20"] > ult["ema50"] else "BAJISTA"
    )

    if tendencia_actual != tendencia_previa:
        choch = "Posible CHOCH"
    else:
        choch = "Sin CHOCH confirmado"

    # Zonas de liquidez estimadas por máximos y mínimos recientes.
    liquidez_compra = maximo
    liquidez_venta = minimo

    # Order Block aproximado: última vela contraria de las últimas 10.
    ob = "No detectado"
    ob_zona = None

    ultimas = df.iloc[-11:-1]

    if cierre > maximo:
        candidatas = ultimas[ultimas["close"] < ultimas["open"]]
        if not candidatas.empty:
            vela = candidatas.iloc[-1]
            ob = "Posible OB alcista"
            ob_zona = (
                float(vela["low"]),
                float(vela["high"])
            )

    elif cierre < minimo:
        candidatas = ultimas[ultimas["close"] > ultimas["open"]]
        if not candidatas.empty:
            vela = candidatas.iloc[-1]
            ob = "Posible OB bajista"
            ob_zona = (
                float(vela["low"]),
                float(vela["high"])
            )

    return {
        "bos": bos,
        "choch": choch,
        "ob": ob,
        "ob_zona": ob_zona,
        "liquidez_compra": liquidez_compra,
        "liquidez_venta": liquidez_venta
    }


# ---------------- ANALISIS Y GRAFICO ----------------

def fmt(precio):
    if precio is None:
        return "N/D"
    return f"{precio:.2f}"


def analizar(mercado):
    mercado = mercado.upper()

    if mercado in ("ORO", "XAU", "XAUUSD"):
        nombre = "ORO XAU/USD"
        df = obtener_oro()
        archivo = os.path.join(CHART_DIR, "chali_oro.png")
    elif mercado in ("BTC", "BTCUSD", "BTCUSDT", "BITCOIN"):
        nombre = "BTC/USDT"
        df = obtener_btc()
        archivo = os.path.join(CHART_DIR, "chali_btc.png")
    else:
        raise ValueError("Mercado desconocido")

    df = calcular_indicadores(df)

    if len(df) < 55:
        raise ValueError("No hay suficientes velas para analizar")

    ult = df.iloc[-1]
    precio = float(ult["close"])
    atr = float(ult["atr"])

    if not np.isfinite(atr) or atr <= 0:
        raise ValueError("No se pudo calcular el ATR")

    estructura = detectar_estructura(df)

    # Señal básica por tendencia, RSI y ruptura.
    tendencia_alcista = ult["ema20"] > ult["ema50"]
    tendencia_bajista = ult["ema20"] < ult["ema50"]
    rsi = float(ult["rsi"])

    ruptura_alcista = estructura["bos"] == "BOS ALCISTA"
    ruptura_bajista = estructura["bos"] == "BOS BAJISTA"

    if tendencia_alcista and 45 <= rsi <= 68 and ruptura_alcista:
        senal = "COMPRA"
        direccion = 1
    elif tendencia_bajista and 32 <= rsi <= 55 and ruptura_bajista:
        senal = "VENTA"
        direccion = -1
    else:
        senal = "ESPERAR"
        direccion = 0

    # Niveles orientativos calculados con ATR.
    if direccion == 1:
        entrada = precio
        sl = entrada - 1.5 * atr
        tp1 = entrada + 1.5 * atr
        tp2 = entrada + 2.5 * atr
    elif direccion == -1:
        entrada = precio
        sl = entrada + 1.5 * atr
        tp1 = entrada - 1.5 * atr
        tp2 = entrada - 2.5 * atr
    else:
        entrada = precio
        sl = entrada - 1.5 * atr
        tp1 = entrada + 1.5 * atr
        tp2 = entrada + 2.5 * atr

    # Dibujo del gráfico.
    graf = df.tail(100).copy()

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(graf["time"], graf["close"], label="Precio", linewidth=1.4)
    ax.plot(graf["time"], graf["ema20"], label="EMA 20", linewidth=1)
    ax.plot(graf["time"], graf["ema50"], label="EMA 50", linewidth=1)

    ax.axhline(entrada, linestyle="--", label="Entrada")
    ax.axhline(sl, linestyle=":", label="SL")
    ax.axhline(tp1, linestyle=":", label="TP1")
    ax.axhline(tp2, linestyle=":", label="TP2")

    ax.set_title(f"CHALI IA | {nombre} | {TIMEFRAME}")
    ax.set_xlabel("Hora UTC")
    ax.set_ylabel("Precio")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(archivo, dpi=130)
    plt.close(fig)

    zona_ob = estructura["ob_zona"]
    if zona_ob:
        zona_texto = f"{fmt(zona_ob[0])} - {fmt(zona_ob[1])}"
    else:
        zona_texto = "No calculada"

    informe = (
        f"CHALI IA - {nombre}\n"
        f"Temporalidad: {TIMEFRAME}\n"
        f"Último cierre recibido: {fmt(precio)}\n"
        f"Señal orientativa: {senal}\n\n"
        f"ENTRADA de referencia: {fmt(entrada)}\n"
        f"SL orientativo: {fmt(sl)}\n"
        f"TP1 orientativo: {fmt(tp1)}\n"
        f"TP2 orientativo: {fmt(tp2)}\n\n"
        f"EMA20: {fmt(float(ult['ema20']))}\n"
        f"EMA50: {fmt(float(ult['ema50']))}\n"
        f"RSI: {rsi:.1f}\n"
        f"ATR: {fmt(atr)}\n\n"
        f"Estructura: {estructura['bos']}\n"
        f"CHOCH: {estructura['choch']}\n"
        f"Order Block: {estructura['ob']}\n"
        f"Zona OB estimada: {zona_texto}\n"
        f"Liquidez superior estimada: "
        f"{fmt(estructura['liquidez_compra'])}\n"
        f"Liquidez inferior estimada: "
        f"{fmt(estructura['liquidez_venta'])}\n\n"
        "Importante: análisis técnico aproximado. No confirma "
        "liquidaciones reales ni garantiza resultados. "
        "Verificá spread, comisiones y precio del broker antes de operar."
    )

    resultado = {
        "senal": senal,
        "entrada": entrada,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2
    }

    guardar_resultado(nombre, resultado)

    return archivo, informe


# ---------------- IA GROQ + CONTEXTO ----------------

def preguntar(chat_id, pregunta):
    if not GROQ_API_KEY:
        return (
            "Falta GROQ_API_KEY en Render. "
            "Agregala en Environment y volvé a probar."
        )

    recuerdos, historial = obtener_memoria(chat_id)

    sistema = (
        "Sos CHALI IA, asistente en español. "
        "Ayudás con trading, programación y preguntas generales. "
        "No afirmes que una señal está garantizada. "
        "No inventes precios actuales: para analizarlos, pedí o generá "
        "el análisis de mercado correspondiente. "
        "Distinguí hechos de hipótesis. "
        "No afirmes que te entrenaste automáticamente. "
        "Usá los recuerdos guardados solo cuando sean relevantes.\n\n"
        "RECUERDOS DEL USUARIO:\n"
        + (recuerdos or "Todavía no hay recuerdos guardados.")
    )

    mensajes = [{"role": "system", "content": sistema}]
    mensajes.extend(historial[-10:])
    mensajes.append({"role": "user", "content": pregunta})

    try:
        r = session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": GROQ_MODEL,
                "messages": mensajes,
                "temperature": 0.4,
                "max_tokens": 1200
            },
            timeout=45
        )

        if not r.ok:
            logging.error("Groq error: %s", r.text[:1000])
            return (
                "Groq devolvió un error. Revisá GROQ_API_KEY, "
                "GROQ_MODEL y los Logs de Render."
            )

        datos = r.json()
        respuesta = datos["choices"][0]["message"]["content"].strip()

        guardar_mensaje(chat_id, "user", pregunta)
        guardar_mensaje(chat_id, "assistant", respuesta)

        return respuesta

    except Exception:
        logging.exception("Error consultando Groq")
        return "No pude conectar con Groq. Intentá de nuevo."


# ---------------- DETECTAR PETICIONES DE MERCADO ----------------

def detectar_mercado(texto):
    t = texto.lower()

    palabras_grafico = [
        "grafico", "gráfico", "graficame", "gráficame",
        "chart", "velas", "candlestick", "analiza",
        "analizá", "analizar", "analisis", "análisis",
        "senal", "señal", "entrada", "operacion",
        "operación", "mercado", "precio", "bos", "choch",
        "order block", "liquidez", "rebote"
    ]

    pide_mercado = any(p in t for p in palabras_grafico)

    oro = any(p in t for p in [
        "oro", "xau", "xauusd", "gold"
    ])

    btc = any(p in t for p in [
        "btc", "bitcoin", "btc/usdt", "btc usd"
    ])

    ambos = any(p in t for p in [
        "ambos", "los dos", "los mercados", "oro y btc",
        "btc y oro", "todo el mercado"
    ])

    if ambos:
        return "AMBOS"

    if oro and btc:
        return "AMBOS"

    if pide_mercado and oro:
        return "ORO"

    if pide_mercado and btc:
        return "BTC"

    return None


# ---------------- COMANDOS TELEGRAM ----------------

def procesar_mensaje(chat_id, texto):
    texto = (texto or "").strip()
    if not texto:
        return

    comando = texto.split()[0].split("@")[0].lower()

    if comando in ("/start", "/ayuda", "/help"):
        enviar_texto(
            chat_id,
            "CHALI IA está disponible.\n\n"
            "/oro - Analizar oro y enviar gráfico\n"
            "/btc - Analizar BTC y enviar gráfico\n"
            "/mercados - Analizar oro y BTC\n"
            "/memoria - Ver recuerdos guardados\n"
            "/recordar texto - Guardar un recuerdo\n"
            "/olvidar - Borrar recuerdos guardados\n\n"
            "También podés escribir: "
            "'Mostrame el gráfico del oro' o "
            "'Analizá BTC con entrada, SL y TP'."
        )
        return

    if comando == "/recordar":
        recuerdo = texto[len("/recordar"):].strip()
        if not recuerdo:
            enviar_texto(chat_id, "Usá: /recordar tu recuerdo")
            return
        guardar_recuerdo(chat_id, recuerdo)
        enviar_texto(chat_id, "Recuerdo guardado en la memoria.")
        return

    if comando == "/memoria":
        recuerdos, _ = obtener_memoria(chat_id, limite=1)
        enviar_texto(
            chat_id,
            "Tus recuerdos guardados:\n" +
            (recuerdos or "Todavía no hay recuerdos.")
        )
        return

    if comando == "/olvidar":
        with db_lock:
            con = conectar_db()
            con.execute(
                "DELETE FROM recuerdos WHERE chat_id = ?",
                (str(chat_id),)
            )
            con.commit()
            con.close()
        enviar_texto(
            chat_id,
            "Borré los recuerdos guardados. "
            "El historial de conversación se conserva."
        )
        return

    mercado = detectar_mercado(texto)

    if comando == "/oro":
        mercado = "ORO"
    elif comando == "/btc":
        mercado = "BTC"
    elif comando == "/mercados":
        mercado = "AMBOS"

    if mercado:
        mercados = ["ORO", "BTC"] if mercado == "AMBOS" else [mercado]

        for m in mercados:
            try:
                enviar_texto(chat_id, f"Analizando {m}. Esperá un momento...")
                archivo, informe = analizar(m)
                enviar_grafico(chat_id, archivo, informe)
                guardar_mensaje(chat_id, "user", texto)
                guardar_mensaje(chat_id, "assistant", informe)
            except Exception as e:
                logging.exception("Error analizando %s", m)
                enviar_texto(
                    chat_id,
                    f"No pude analizar {m}.\n"
                    f"Detalle: {str(e)[:700]}"
                )
        return

    respuesta = preguntar(chat_id, texto)
    enviar_texto(chat_id, respuesta)


# ---------------- WEBHOOK DE TELEGRAM ----------------

@app.route("/", methods=["GET"])
def inicio():
    return jsonify({
        "app": "CHALI IA",
        "estado": "online",
        "mercados": ["ORO XAU/USD", "BTC/USDT"],
        "timeframe": TIMEFRAME
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return jsonify({
            "mensaje": "Webhook de CHALI IA activo"
        }), 200

    if TELEGRAM_WEBHOOK_SECRET:
        recibido = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )
        if recibido != TELEGRAM_WEBHOOK_SECRET:
            return jsonify({"error": "no autorizado"}), 403

    update = request.get_json(silent=True) or {}

    mensaje = (
        update.get("message")
        or update.get("edited_message")
    )

    if not mensaje:
        return jsonify({"ok": True, "ignorado": True}), 200

    chat = mensaje.get("chat", {})
    chat_id = chat.get("id")
    texto = mensaje.get("text", "")

    if chat_id is None or not texto:
        return jsonify({"ok": True, "ignorado": True}), 200

    # Si se configura el ID administrador, limita el acceso.
    if TELEGRAM_ADMIN_ID and str(chat_id) != TELEGRAM_ADMIN_ID:
        logging.warning("Chat no autorizado: %s", chat_id)
        return jsonify({"ok": True, "ignorado": True}), 200

    try:
        procesar_mensaje(chat_id, texto)
    except Exception:
        logging.exception("Error procesando mensaje Telegram")

    return jsonify({"ok": True}), 200


# ---------------- CONFIGURAR WEBHOOK AUTOMATICAMENTE ----------------

def configurar_webhook():
    if not TELEGRAM_TOKEN:
        logging.warning("TELEGRAM_TOKEN no configurado")
        return

    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url:
        logging.warning(
            "RENDER_EXTERNAL_URL no está disponible; "
            "configurá el webhook manualmente."
        )
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN + "/setWebhook"
    )

    datos = {"url": base_url + "/webhook"}

    if TELEGRAM_WEBHOOK_SECRET:
        datos["secret_token"] = TELEGRAM_WEBHOOK_SECRET

    try:
        r = session.post(url, data=datos, timeout=20)
        logging.info("Configurar webhook: %s", r.text[:500])
    except Exception:
        logging.exception("No se pudo configurar el webhook")


if __name__ == "__main__":
    configurar_webhook()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
else:
    # Gunicorn importa app.py; configuramos el webhook al arrancar.
    configurar_webhook()

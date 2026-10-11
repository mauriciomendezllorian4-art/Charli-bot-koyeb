import os
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

client = Groq(api_key=GROQ_API_KEY)
MODELO_GROQ = "llama-3.3-70b-versatile" # CORREGIDO - el viejo 8b ya no existe

def get_btc_data():
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=100"
        r = requests.get(url, timeout=10).json()
        df = pd.DataFrame(r, columns=["open_time","open","high","low","close","vol","close_time","qav","trades","taker_base","taker_quote","ignore"])
        df["close"] = df["close"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        return df
    except:
        return None

def analisis_v20(symbol="BTC/USD"):
    df = get_btc_data()
    if df is None:
        return None, "Error obteniendo datos"

    precio = df["close"].iloc[-1]
    ema20 = df["close"].ewm(span=20).mean().iloc[-1]
    ema50 = df["close"].ewm(span=50).mean().iloc[-1]

    # Logica V20 Charli
    tendencia = "COMPRA" if precio > ema20 and ema20 > ema50 else "VENTA"
    sl = precio * 0.995 if tendencia == "COMPRA" else precio * 1.005
    tp = precio * 1.008 if tendencia == "COMPRA" else precio * 0.992
    ent = precio

    # Grafico
    plt.figure(figsize=(8,4))
    plt.plot(df["close"].tail(50).values, label="Precio", linewidth=2)
    plt.plot(df["close"].ewm(span=20).mean().tail(50).values, label="EMA20")
    plt.title(f"CHALI V20 {symbol} {tendencia} - Precio {precio:.2f}")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig("/tmp/chart.png")
    plt.close()

    texto = f"🤖 CHALI V20 {symbol} {tendencia}\nPrecio {precio:.2f}\nENT {ent:.2f} SL {sl:.2f} TP {tp:.2f}\nLote 0.02 Saldo $1000"
    return "/tmp/chart.png", texto

def consulta_groq(pregunta):
    try:
        completion = client.chat.completions.create(
            model=MODELO_GROQ,
            messages=[
                {"role": "system", "content": "Sos Charli, experto en trading de ORO y BTC. Hablas como argentino, directo, trader rentable. Estrategia V20: EMA20/50, entradas en retroceso, SL 0.5%, TP 0.8%, gestión 0.02 lote por cada 1000 USD. Das consejos claros."},
                {"role": "user", "content": pregunta}
            ],
            temperature=0.7,
            max_tokens=800
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"Lag groq Error code: {e}"

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

def send_photo(chat_id, photo_path, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(photo_path, 'rb') as f:
        requests.post(url, data={"chat_id": chat_id, "caption": caption}, files={"photo": f})

@app.route("/")
def home():
    return "Charli V20 Bot Live!"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "").lower()

        if "/v20" in text or "btc" in text and "/v20" in text:
            path, txt = analisis_v20("BTC/USD")
            if path:
                send_photo(chat_id, path, txt)
            else:
                send_message(chat_id, txt)
        elif "/oro" in text or "oro" in text and "v20" in text:
            path, txt = analisis_v20("ORO/USD")
            if path:
                send_photo(chat_id, path, txt)
            else:
                send_message(chat_id, txt)
        elif "/script" in text:
            script = """// @version=5
strategy("CHALI V20", overlay=true)
ema20 = ta.ema(close, 20)
ema50 = ta.ema(close, 50)
long = close > ema20 and ema20 > ema50
short = close < ema20 and ema20 < ema50
strategy.entry("Compra", strategy.long, when=long)
strategy.entry("Venta", strategy.short, when=short)
plot(ema20, color=color.yellow)
plot(ema50, color=color.orange)
"""
            send_message(chat_id, f"📜 Pine Script V20:\n{script}")
        else:
            # Cualquier otra pregunta va a Groq
            resp = consulta_groq(data["message"].get("text",""))
            send_message(chat_id, resp)

    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

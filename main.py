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
# MODELO NUEVO QUE SI FUNCIONA - Groq borro los llama
MODELO_GROQ = "openai/gpt-oss-20b"

def get_btc_data():
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=100"
        r = requests.get(url, timeout=10).json()
        df = pd.DataFrame(r, columns=["open_time","open","high","low","close","vol","close_time","qav","trades","taker_base","taker_quote","ignore"])
        df["close"] = df["close"].astype(float)
        return df
    except:
        return None

def analisis_v20(symbol="BTC/USD"):
    df = get_btc_data()
    if df is None:
        return None, "Error Binance"

    precio = df["close"].iloc[-1]
    ema20 = df["close"].ewm(span=20).mean().iloc[-1]
    ema50 = df["close"].ewm(span=50).mean().iloc[-1]

    tendencia = "COMPRA" if precio > ema20 else "VENTA"
    sl = precio * 0.995 if tendencia == "COMPRA" else precio * 1.005
    tp = precio * 1.008 if tendencia == "COMPRA" else precio * 0.992

    plt.figure(figsize=(8,4))
    plt.plot(df["close"].tail(50).values, label="Precio", linewidth=2)
    plt.plot(df["close"].ewm(span=20).mean().tail(50).values, label="EMA20")
    plt.title(f"CHALI V20 {symbol} {tendencia} - {precio:.2f}")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig("/tmp/chart.png")
    plt.close()

    texto = f"🤖 CHALI V20 {symbol} {tendencia}\nPrecio {precio:.2f}\nENT {precio:.2f} SL {sl:.2f} TP {tp:.2f}\nLote 0.02 Saldo $1000"
    return "/tmp/chart.png", texto

def consulta_groq(pregunta):
    try:
        completion = client.chat.completions.create(
            model=MODELO_GROQ,
            messages=[
                {"role": "system", "content": "Sos Charli, experto en trading ORO y BTC. Argentino, directo, trader rentable. Estrategia V20: EMA20/50, SL 0.5% TP 0.8%"},
                {"role": "user", "content": pregunta}
            ],
            temperature=0.7,
            max_tokens=800
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"Error groq: {e}"

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

def send_photo(chat_id, photo_path, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(photo_path, 'rb') as f:
        requests.post(url, data={"chat_id": chat_id, "caption": caption}, files={"photo": f})

@app.route("/")
def home():
    return "Charli V20 Live!"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text_user = data["message"].get("text", "")
        text_low = text_user.lower()

        if "/v20" in text_low:
            path, txt = analisis_v20("BTC/USD")
            send_photo(chat_id, path, txt) if path else send_message(chat_id, txt)
        elif "/oro" in text_low:
            path, txt = analisis_v20("ORO/USD")
            send_photo(chat_id, path, txt) if path else send_message(chat_id, txt)
        elif "/v21" in text_low:
            path, txt = analisis_v20("BTC/USD")
            send_photo(chat_id, path, txt) if path else send_message(chat_id, txt)
        elif "/script" in text_low:
            send_message(chat_id, "Pine V20: ema20 = ta.ema(close,20) ema50=ta.ema(close,50)")
        else:
            resp = consulta_groq(text_user)
            send_message(chat_id, resp)
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

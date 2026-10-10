import os
import requests
from flask import Flask, request
from groq import Groq
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_KEY = os.environ.get("GROQ_API_KEY")
TD_KEY = os.environ.get("TWELVEDATA_KEY")
API_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}"
client = Groq(api_key=GROQ_KEY)

ALERTAS = {}

def get_precio_oro():
    try:
        url = f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={TD_KEY}"
        return requests.get(url, timeout=10).json().get("price")
    except: return None

def get_historico_full(ticker="XAU/USD"):
    # trae OHLC para velas estilo TradingView
    try:
        url = f"https://api.twelvedata.com/time_series?symbol={ticker}&interval=1day&outputsize=30&apikey={TD_KEY}"
        r = requests.get(url, timeout=15).json()
        return r.get("values", [])[::-1]
    except: return []

def get_historico(ticker):
    vals = get_historico_full(ticker)
    if vals:
        fechas = [x["datetime"][5:] for x in vals]
        precios = [float(x["close"]) for x in vals]
        return fechas, precios
    return None, None

def get_dolares():
    try:
        r = requests.get("https://dolarapi.com/v1/dolares", timeout=10).json()
        return {x["casa"]: x for x in r}
    except: return None

def calcular_rsi(precios, per=14):
    if len(precios) < per+1: return 50
    gains = losses = 0
    for i in range(1, per+1):
        diff = precios[-i] - precios[-i-1]
        if diff > 0: gains += diff
        else: losses -= diff
    if losses == 0: return 100
    rs = gains/losses
    return 100 - (100/(1+rs))

def preguntar_a_ia(mensaje):
    resp = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[
            {"role": "system", "content": "Sos Charli, trader experto oro y dolar argentino. Corto, argentino."},
            {"role": "user", "content": mensaje}
        ]
    )
    return resp.choices[0].message.content

def mandar_grafico_tradingview(chat_id, ticker="XAU/USD"):
    vals = get_historico_full(ticker)
    if not vals:
        requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":"No pude traer velas, limite TwelveData."})
        return

    fechas = [v["datetime"][5:] for v in vals]
    closes = [float(v["close"]) for v in vals]
    opens = [float(v["open"]) for v in vals]

    # Grafico estilo TradingView
    plt.figure(figsize=(12,6))
    plt.style.use('dark_background')
    # velas simplificadas con color verde/rojo
    for i in range(len(vals)):
        color = '#26a69a' if closes[i] >= opens[i] else '#ef5350'
        plt.plot([i,i],[float(vals[i]["low"]), float(vals[i]["high"])], color=color, linewidth=1)
        plt.plot([i,i],[opens[i], closes[i]], color=color, linewidth=4)

    plt.xticks(range(len(fechas)), fechas, rotation=45)
    plt.title(f"{ticker} - TradingView Style - {closes[-1]} USD", color='white')
    plt.grid(alpha=0.15)
    plt.tight_layout()
    plt.savefig("/tmp/tv.png", facecolor='#131722')
    plt.close()

    link_tv = f"https://www.tradingview.com/chart/?symbol=OANDA%3A{ticker.replace('/','')}" if "XAU" in ticker else "https://www.tradingview.com/symbols/USDARS/"

    with open("/tmp/tv.png","rb") as foto:
        requests.post(f"{API_TELEGRAM}/sendPhoto",
            data={"chat_id": chat_id, "caption": f"📈 {ticker} {closes[-1]} USD\n🔗 Abrir en TradingView: {link_tv}\nRSI: {calcular_rsi(closes):.1f}"},
            files={"photo": foto})

@app.route("/")
def home(): return "Charli PRO TV vivo"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    try:
        if "message" in data and "text" in data["message"]:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"]["text"]
            tl = text.lower()

            # TRADINGVIEW
            if "tradingview" in tl or tl.startswith("/tv") or tl == "tv":
                if "dolar" in tl:
                    mandar_grafico_tradingview(chat_id, "USD/ARS")
                else:
                    mandar_grafico_tradingview(chat_id, "XAU/USD")
                return "ok",200

            if "grafico" in tl:
                if "dolar" in tl:
                    f,p = get_historico("USD/ARS")
                    if p:
                        plt.figure(figsize=(10,5)); plt.plot(f,p); plt.savefig("/tmp/g.png"); plt.close()
                        with open("/tmp/g.png","rb") as foto: requests.post(f"{API_TELEGRAM}/sendPhoto", data={"chat_id":chat_id}, files={"photo":foto})
                else:
                    mandar_grafico_tradingview(chat_id, "XAU/USD")
                return "ok",200

            if "/dolar" in tl or "/usd" in tl:
                d = get_dolares()
                txt = "\n".join([f"{k.upper()}: ${v['venta']}" for k,v in d.items()]) if d else "Error dolar"
                requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":txt})
                return "ok",200

            if "/oro" in tl or tl.startswith("oro"):
                p = get_precio_oro()
                r = preguntar_a_ia(f"Oro {p} USD. Explica en 3 lineas.") if p else "No pude traer oro"
                requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":r})
                return "ok",200

            if "/analisis" in tl:
                f,p = get_historico("XAU/USD")
                rsi = calcular_rsi(p) if p else 50
                r = preguntar_a_ia(f"Analisis oro RSI {rsi:.1f} precios {p[-5:]}")
                requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":r})
                return "ok",200

            if "/alerta" in tl:
                try:
                    precio = float(tl.replace("/alerta","").strip().split()[0])
                    ALERTAS[chat_id]=precio
                    r=f"Alerta puesta en {precio} USD"
                except: r="Usa: /alerta 4200"
                requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":r})
                return "ok",200

            r = preguntar_a_ia(text)
            requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":r})

    except Exception as e:
        print(f"ERROR: {e}")
    return "ok",200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
          
            
              
          
              
           
                
        
          
    
       
  
  

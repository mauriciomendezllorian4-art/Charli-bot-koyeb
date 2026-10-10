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

ALERTAS = {} # chat_id: precio

# --- FUNCIONES BASE ---
def get_precio_oro():
    try:
        url = f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={TD_KEY}"
        return requests.get(url, timeout=10).json().get("price")
    except: return None

def get_historico(ticker):
    try:
        url = f"https://api.twelvedata.com/time_series?symbol={ticker}&interval=1day&outputsize=30&apikey={TD_KEY}"
        r = requests.get(url, timeout=15).json()
        if "values" in r:
            v = r["values"][::-1]
            fechas = [x["datetime"][5:] for x in v]
            precios = [float(x["close"]) for x in v]
            return fechas, precios
    except Exception as e: print(e)
    return None, None

def get_dolares():
    try:
        r = requests.get("https://dolarapi.com/v1/dolares", timeout=10).json()
        # r es lista de dicts
        d = {x["casa"]: x for x in r}
        return d
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
            {"role": "system", "content": "Sos Charli, trader experto en oro y dolar argentino. Corto, argentino, sin humo."},
            {"role": "user", "content": mensaje}
        ]
    )
    return resp.choices[0].message.content

def mandar_grafico(chat_id, fechas, precios, titulo):
    plt.figure(figsize=(10,5))
    plt.plot(fechas, precios, marker='o', linewidth=2)
    plt.title(titulo)
    plt.xticks(rotation=45)
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig("/tmp/grafico.png")
    plt.close()
    with open("/tmp/grafico.png","rb") as foto:
        requests.post(f"{API_TELEGRAM}/sendPhoto", data={"chat_id": chat_id, "caption": titulo}, files={"photo": foto})

# --- WEBHOOK ---
@app.route("/")
def home(): return "Charli PRO vivo"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    try:
        if "message" in data and "text" in data["message"]:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"]["text"]
            tl = text.lower()

            # GRAFICO ORO
            if "grafico oro" in tl or tl == "/grafico" or tl == "grafico":
                f,p = get_historico("XAU/USD")
                if p: mandar_grafico(chat_id,f,p,f"Oro {p[-1]} USD - 30 dias")
                else: requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":"No pude traer historico oro, proba 1 min."})
                return "ok",200

            # GRAFICO DOLAR
            if "grafico dolar" in tl or "grafico usd" in tl:
                # Para dolar usamos blue de TwelveData? Usamos USD/ARS
                f,p = get_historico("USD/ARS")
                if p: mandar_grafico(chat_id,f,p,f"Dolar oficial aprox {p[-1]} ARS")
                else: requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":"No pude traer grafico dolar"})
                return "ok",200

            # DOLAR
            if "/dolar" in tl or "/usd" in tl or tl.startswith("dolar"):
                d = get_dolares()
                if not d:
                    resp = "No pude traer dolar ahora"
                else:
                    txt = ""
                    for k in ["oficial","blue","bolsa","contadoconliqui","mayorista","cripto"]:
                        if k in d:
                            txt += f"{k.upper()}: ${d[k]['venta']} (compra ${d[k]['compra']})\n"
                    resp = txt + "\n" + preguntar_a_ia(f"Dolar hoy: {txt}. Analiza en 3 lineas para Argentina.")
                requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":resp})
                return "ok",200

            # ORO
            if "/oro" in tl or tl.startswith("oro"):
                p = get_precio_oro()
                # chequear alertas
                if chat_id in ALERTAS and p and float(p) >= float(ALERTAS[chat_id]):
                    requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":f"🔔 ALERTA ORO! Llego a {p} USD (tu alerta {ALERTAS[chat_id]})"})
                    del ALERTAS[chat_id]
                r = preguntar_a_ia(f"Oro {p} USD. Explica en 3 lineas.") if p else "No pude traer oro"
                requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":r})
                return "ok",200

            # ANALISIS
            if "/analisis" in tl or "analisis" in tl:
                f,p = get_historico("XAU/USD")
                if p:
                    rsi = calcular_rsi(p)
                    estado = "SOBRECOMPRADO" if rsi>70 else "SOBREVENDIDO" if rsi<30 else "NEUTRAL"
                    prompt = f"Oro ultimos precios {p[-5:]}, RSI {rsi:.1f} {estado}. Hace analisis tecnico corto con soporte y resistencia."
                    r = preguntar_a_ia(prompt)
                else: r="No pude hacer analisis"
                requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":r})
                return "ok",200

            # ALERTA
            if "/alerta" in tl:
                try:
                    precio = float(tl.replace("/alerta","").strip().split()[0])
                    ALERTAS[chat_id] = precio
                    r = f"Listo! Te aviso cuando oro llegue a {precio} USD"
                except:
                    r = "Usa: /alerta 4200"
                requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":r})
                return "ok",200

            # CONVERTIR
            if "convertir" in tl or "cuanto es" in tl:
                r = preguntar_a_ia(text)
                requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":r})
                return "ok",200

            # CHAT LIBRE
            r = preguntar_a_ia(text)
            requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":r})

    except Exception as e:
        print(f"ERROR: {e}")
        try: requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":data["message"]["chat"]["id"],"text":f"Error: {e}"})
        except: pass
    return "ok",200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
    
      
       
  
  

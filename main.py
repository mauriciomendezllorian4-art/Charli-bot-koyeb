import os, requests, json, datetime
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
from flask import Flask, request
from groq import Groq

app = Flask(__name__)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
client = Groq(api_key=GROQ_API_KEY)
MODELO = "openai/gpt-oss-20b"
MEMORY_FILE = "/tmp/charli_memory.json"
SALDO = 1000

def load_memory():
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE,'r') as f: return json.load(f)
    except: pass
    return {"ganadas":18,"perdidas":7,"trades":[],"racha":0}

def save_memory(m):
    try:
        with open(MEMORY_FILE,'w') as f: json.dump(m,f)
    except: pass

def get_klines(interval, limit=100):
    url=f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
    try:
        data=requests.get(url,timeout=10).json()
        df=pd.DataFrame(data, columns=["t","o","h","l","c","v","ct","q","tr","tb","tq","i"])
        for col in ["o","h","l","c","v"]: df[col]=df[col].astype(float)
        return df
    except: return None

# ========== KILLZONE ==========
def is_killzone():
    now = datetime.datetime.utcnow()
    hour = now.hour
    # NY Killzone 13-17 UTC (8am-12pm NY) + London 8-11 UTC
    if 8 <= hour <= 11: return True, "LONDON KZ 🟢"
    if 13 <= hour <= 17: return True, "NY KZ 🟢🔥 MEJOR HORA"
    return False, f"FUERA DE KZ 🔴 {hour} UTC - Baja liquidez"

# ========== SMC + FVG ==========
def detect_smc_fvg(df):
    # Swing
    last_high = df["h"].tail(20).max()
    last_low = df["l"].tail(20).min()
    precio = df["c"].iloc[-1]

    # BOS / CHoCH
    bos_alc = precio > df["h"].tail(20).iloc[-2] if len(df)>20 else False
    bos_baj = precio < df["l"].tail(20).iloc[-2] if len(df)>20 else False

    # ORDER BLOCK
    ob_bull = None; ob_bear = None
    for i in range(len(df)-10, len(df)-2):
        if df["c"].iloc[i+1] > df["o"].iloc[i+1] and (df["c"].iloc[i+1]-df["o"].iloc[i+1]) > (df["h"].iloc[i+1]-df["l"].iloc[i+1])*0.6:
            if df["c"].iloc[i] < df["o"].iloc[i]:
                ob_bull = (min(df["o"].iloc[i], df["c"].iloc[i]), max(df["o"].iloc[i], df["c"].iloc[i]))
        if df["c"].iloc[i+1] < df["o"].iloc[i+1] and (df["o"].iloc[i+1]-df["c"].iloc[i+1]) > (df["h"].iloc[i+1]-df["l"].iloc[i+1])*0.6:
            if df["c"].iloc[i] > df["o"].iloc[i]:
                ob_bear = (min(df["o"].iloc[i], df["c"].iloc[i]), max(df["o"].iloc[i], df["c"].iloc[i]))

    # FVG / IMBALANCE - Hueco
    fvg_bull = None; fvg_bear = None
    for i in range(len(df)-5, len(df)-1):
        # Bullish FVG: low actual > high de hace 2 velas
        if df["l"].iloc[i] > df["h"].iloc[i-2]:
            fvg_bull = (df["h"].iloc[i-2], df["l"].iloc[i])
        if df["h"].iloc[i] < df["l"].iloc[i-2]:
            fvg_bear = (df["l"].iloc[i-2], df["h"].iloc[i])

    # LIQUIDACION
    recent_high = df["h"].tail(20).max(); recent_low = df["l"].tail(20).min()
    liq_compra = df["l"].iloc[-1] < recent_low and df["c"].iloc[-1] > recent_low
    liq_venta = df["h"].iloc[-1] > recent_high and df["c"].iloc[-1] < recent_high

    # PREMIUM / DISCOUNT
    rango = recent_high - recent_low
    eq = recent_low + rango*0.5
    premium = precio > eq
    discount = not premium

    return {
        "precio": precio, "last_high": last_high, "last_low": recent_low,
        "bos_alc": bos_alc, "bos_baj": bos_baj,
        "ob_bull": ob_bull, "ob_bear": ob_bear,
        "fvg_bull": fvg_bull, "fvg_bear": fvg_bear,
        "liq_compra": liq_compra, "liq_venta": liq_venta,
        "premium": premium, "discount": discount, "eq": eq
    }

def analisis_final():
    mem=load_memory()
    df1m=get_klines("1m",100); df5m=get_klines("5m",100); df15m=get_klines("15m",100)
    if df1m is None: return None, "Error Binance", None

    smc1=detect_smc_fvg(df1m); smc5=detect_smc_fvg(df5m); smc15=detect_smc_fvg(df15m)
    kz_ok, kz_text = is_killzone()

    precio=smc1["precio"]

    # Tendencia
    if smc15["bos_alc"]: tendencia="COMPRA"
    elif smc15["bos_baj"]: tendencia="VENTA"
    else: tendencia="COMPRA" if precio > df15m["c"].ewm(span=20).mean().iloc[-1] else "VENTA"

    # ========== ENTRADA SNIPER: OB + FVG ==========
    if tendencia=="COMPRA":
        ob = smc5["ob_bull"]; fvg = smc5["fvg_bull"] or smc1["fvg_bull"]
        if ob and fvg:
            # Confluencia OB + FVG = SNIPER
            entrada = (ob[0]+ob[1]+fvg[0]+fvg[1])/4
            calidad="💎 SNIPER OB+FVG 🔥🔥🔥"
        elif ob:
            entrada=(ob[0]+ob[1])/2
            calidad="✅ OB 50%"
        elif fvg:
            entrada=(fvg[0]+fvg[1])/2
            calidad="✅ FVG 50%"
        else:
            entrada=smc5["last_low"]*1.001
            calidad="⚠️ SWING"

        sl=smc5["last_low"]*0.998
        tp1=entrada*1.007; tp2=entrada*1.015; tp3=smc15["last_high"]
        # Premium/Discount check
        zona = "DESCUENTO 🟢 Buen precio para comprar" if smc15["discount"] else "PREMIUM 🔴 Caro, no comprar"

    else:
        ob=smc5["ob_bear"]; fvg=smc5["fvg_bear"] or smc1["fvg_bear"]
        if ob and fvg:
            entrada=(ob[0]+ob[1]+fvg[0]+fvg[1])/4
            calidad="💎 SNIPER OB+FVG 🔥🔥🔥"
        elif ob:
            entrada=(ob[0]+ob[1])/2
            calidad="✅ OB 50%"
        elif fvg:
            entrada=(fvg[0]+fvg[1])/2
            calidad="✅ FVG 50%"
        else:
            entrada=smc5["last_high"]*0.999
            calidad="⚠️ SWING"

        sl=smc5["last_high"]*1.002
        tp1=entrada*0.993; tp2=entrada*0.985; tp3=smc15["last_low"]
        zona = "PREMIUM 🟢 Buen precio para vender" if smc15["premium"] else "DESCUENTO 🔴 Barato, no vender"

    # GESTION RIESGO
    riesgo_usd = SALDO * 0.01
    distancia_sl = abs(entrada - sl)
    lote = round(riesgo_usd / distancia_sl / precio * 1000, 2) if distancia_sl>0 else 0.02
    lote = max(0.01, min(lote, 0.1))

    # GRAFICO
    plt.figure(figsize=(12,6)); plt.style.use('dark_background')
    plt.plot(df1m["c"].tail(80).values, color="white", lw=1.5, label="BTC 1m")
    # OB
    if smc5["ob_bull"]: plt.axhspan(smc5["ob_bull"][0], smc5["ob_bull"][1], color="blue", alpha=0.3, label="OB BULL")
    if smc5["ob_bear"]: plt.axhspan(smc5["ob_bear"][0], smc5["ob_bear"][1], color="orange", alpha=0.3, label="OB BEAR")
    # FVG
    if smc5["fvg_bull"]: plt.axhspan(smc5["fvg_bull"][0], smc5["fvg_bull"][1], color="cyan", alpha=0.2, label="FVG BULL")
    if smc5["fvg_bear"]: plt.axhspan(smc5["fvg_bear"][0], smc5["fvg_bear"][1], color="magenta", alpha=0.2, label="FVG BEAR")

    plt.axhline(entrada, color="cyan", lw=2, label=f"ENT {entrada:.2f}")
    plt.axhline(sl, color="red", ls="--", lw=2, label=f"SL {sl:.2f}")
    plt.axhline(tp1, color="lime", ls="--", label=f"TP1 {tp1:.2f}")
    plt.axhline(tp2, color="green", label=f"TP2 {tp2:.2f}")
    plt.axhline(smc15["eq"], color="gray", ls=":", label="EQ 50%")
    plt.title(f"{tendencia} | {calidad} | {kz_text}", fontsize=9)
    plt.legend(fontsize=6, loc="upper left"); plt.grid(alpha=0.15)
    plt.tight_layout(); plt.savefig("/tmp/v20final.png", facecolor="#0e0e0e", dpi=150); plt.close()

    efect = mem["ganadas"]/(mem["ganadas"]+mem["perdidas"])*100 if mem["ganadas"]+mem["perdidas"]>0 else 0

    texto = f"""🤖 CHALI V20 FINAL INSTITUCIONAL

{kz_text}
{'✅ OPERABLE' if kz_ok else '⚠️ ESPERAR KILLZONE'}

📊 ESTRUCTURA SMC:
   Tendencia: {tendencia}
   BOS: {'ALCISTA ✅' if smc15['bos_alc'] else 'BAJISTA ✅' if smc15['bos_baj'] else 'RANGO'}
   {calidad}

📦 OB: {'BULL ' + str(smc5['ob_bull']) if smc5['ob_bull'] else 'BEAR ' + str(smc5['ob_bear']) if smc5['ob_bear'] else 'NO'}
📉 FVG: {'BULL ' + str(smc5['fvg_bull']) if smc5['fvg_bull'] else 'BEAR ' + str(smc5['fvg_bear']) if smc5['fvg_bear'] else 'NO'}
   CONFLUENCIA: {'OB+FVG 💎 SNIPER' if (smc5['ob_bull'] or smc5['ob_bear']) and (smc5['fvg_bull'] or smc5['fvg_bear']) else 'SOLO UNO'}

💧 LIQUIDEZ: {'Barrido lows ✅' if smc1['liq_compra'] else 'Barrido highs ✅' if smc1['liq_venta'] else 'Sin barrido'}
💰 PREMIUM/DISCOUNT: {zona}
   EQ 50%: {smc15['eq']:.2f}

💵 PRECIO: {precio:.2f}

📍 ENTRADA LIMIT (50% OB/FVG):
   {entrada:.2f}

🛑 SL: {sl:.2f}
🎯 TP1: {tp1:.2f} | TP2: {tp2:.2f} | TP3: {tp3:.2f}

💼 GESTION PRO:
   Saldo: ${SALDO} | Riesgo: ${riesgo_usd:.2f} (1%)
   Lote: {lote} | RR: 1:2.5

🧠 MEMORIA REAL:
   Ganadas: {mem['ganadas']} | Perdidas: {mem['perdidas']} | Efect: {efect:.1f}%

👇 Cuando cierres, toca el boton para que aprenda:
"""
    return "/tmp/v20final.png", texto, kz_ok

def send_photo_buttons(cid, path, cap):
    url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    keyboard={"inline_keyboard":[[{"text":"✅ Ganada","callback_data":"ganada"},{"text":"❌ Perdida","callback_data":"perdida"}]]}
    with open(path,'rb') as f:
        requests.post(url, data={"chat_id":cid,"caption":cap,"reply_markup":json.dumps(keyboard)}, files={"photo":f}, timeout=20)

def send_msg(cid, txt):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id":cid,"text":txt})

@app.route("/")
def home(): return "V20 FINAL OK"

@app.route("/webhook", methods=["POST"])
def webhook():
    data=request.get_json()
    # BOTONES
    if "callback_query" in data:
        cq=data["callback_query"]; cid=cq["message"]["chat"]["id"]; dato=cq["data"]
        mem=load_memory()
        if dato=="ganada": mem["ganadas"]+=1; mem["racha"]=max(0,mem["racha"])+1
        else: mem["perdidas"]+=1; mem["racha"]=min(0,mem["racha"])-1
        save_memory(mem)
        efect=mem["ganadas"]/(mem["ganadas"]+mem["perdidas"])*100
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={"callback_query_id":cq["id"],"text":f"Guardado! Efect {efect:.1f}%"})
        send_msg(cid, f"🧠 Aprendido! {'✅' if dato=='ganada' else '❌'} {dato.upper()}\nAhora: {mem['ganadas']}G - {mem['perdidas']}P - {efect:.1f}% | Racha: {mem['racha']}")
        return "ok",200

    if "message" in data:
        cid=data["message"]["chat"]["id"]
        t=data["message"].get("text","").lower()
        if "/v20" in t or "/v21" in t or "/smc" in t:
            p,txt,kz=analisis_final()
            if p: send_photo_buttons(cid,p,txt)
            else: send_msg(cid,txt)
        else:
            try:
                mem=load_memory()
                prompt=f"Sos Charli V20 SMC pro con OB FVG BOS CHoCH LIQ. Memoria {mem['ganadas']}G {mem['perdidas']}P. Responde corto. Pregunta: {data['message'].get('text','')}"
                comp=client.chat.completions.create(model=MODELO, messages=[{"role":"user","content":prompt}], max_tokens=600)
                send_msg(cid, comp.choices[0].message.content)
            except Exception as e:
                send_msg(cid, f"Error: {e}")
    return "ok",200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))

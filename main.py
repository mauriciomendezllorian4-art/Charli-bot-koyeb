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

def is_killzone():
    now = datetime.datetime.utcnow(); hour = now.hour
    if 8 <= hour <= 11: return True, "LONDON KZ 🟢"
    if 13 <= hour <= 17: return True, "NY KZ 🟢🔥"
    return False, f"FUERA KZ {hour} UTC"

def detect_smc_fvg(df):
    last_high = df["h"].tail(20).max(); last_low = df["l"].tail(20).min(); precio = df["c"].iloc[-1]
    bos_alc = precio > df["h"].tail(20).iloc[-2] if len(df)>20 else False
    bos_baj = precio < df["l"].tail(20).iloc[-2] if len(df)>20 else False
    ob_bull = None; ob_bear = None
    for i in range(len(df)-10, len(df)-2):
        if df["c"].iloc[i+1] > df["o"].iloc[i+1] and (df["c"].iloc[i+1]-df["o"].iloc[i+1]) > (df["h"].iloc[i+1]-df["l"].iloc[i+1])*0.6:
            if df["c"].iloc[i] < df["o"].iloc[i]: ob_bull = (min(df["o"].iloc[i], df["c"].iloc[i]), max(df["o"].iloc[i], df["c"].iloc[i]))
        if df["c"].iloc[i+1] < df["o"].iloc[i+1] and (df["o"].iloc[i+1]-df["c"].iloc[i+1]) > (df["h"].iloc[i+1]-df["l"].iloc[i+1])*0.6:
            if df["c"].iloc[i] > df["o"].iloc[i]: ob_bear = (min(df["o"].iloc[i], df["c"].iloc[i]), max(df["o"].iloc[i], df["c"].iloc[i]))
    fvg_bull = None; fvg_bear = None
    for i in range(len(df)-5, len(df)-1):
        if df["l"].iloc[i] > df["h"].iloc[i-2]: fvg_bull = (df["h"].iloc[i-2], df["l"].iloc[i])
        if df["h"].iloc[i] < df["l"].iloc[i-2]: fvg_bear = (df["l"].iloc[i-2], df["h"].iloc[i])
    recent_high = df["h"].tail(20).max(); recent_low = df["l"].tail(20).min()
    liq_compra = df["l"].iloc[-1] < recent_low and df["c"].iloc[-1] > recent_low
    liq_venta = df["h"].iloc[-1] > recent_high and df["c"].iloc[-1] < recent_high
    rango = recent_high - recent_low; eq = recent_low + rango*0.5; premium = precio > eq
    return {"precio":precio,"last_high":last_high,"last_low":recent_low,"bos_alc":bos_alc,"bos_baj":bos_baj,"ob_bull":ob_bull,"ob_bear":ob_bear,"fvg_bull":fvg_bull,"fvg_bear":fvg_bear,"liq_compra":liq_compra,"liq_venta":liq_venta,"premium":premium,"discount":not premium,"eq":eq}

def analisis_final():
    mem=load_memory()
    df1m=get_klines("1m",100); df5m=get_klines("5m",100); df15m=get_klines("15m",100)
    if df1m is None: return None, "Error Binance", None
    smc1=detect_smc_fvg(df1m); smc5=detect_smc_fvg(df5m); smc15=detect_smc_fvg(df15m)
    kz_ok, kz_text = is_killzone()
    precio=smc1["precio"]
    if smc15["bos_alc"]: tendencia="COMPRA"
    elif smc15["bos_baj"]: tendencia="VENTA"
    else: tendencia="COMPRA" if precio > df15m["c"].ewm(span=20).mean().iloc[-1] else "VENTA"
    if tendencia=="COMPRA":
        ob = smc5["ob_bull"]; fvg = smc5["fvg_bull"] or smc1["fvg_bull"]
        if ob and fvg: entrada=(ob[0]+ob[1]+fvg[0]+fvg[1])/4; calidad="💎 SNIPER OB+FVG 🔥"
        elif ob: entrada=(ob[0]+ob[1])/2; calidad="✅ OB 50%"
        elif fvg: entrada=(fvg[0]+fvg[1])/2; calidad="✅ FVG 50%"
        else: entrada=smc5["last_low"]*1.001; calidad="⚠️ SWING"
        sl=smc5["last_low"]*0.998; tp1=entrada*1.007; tp2=entrada*1.015; tp3=smc15["last_high"]
        zona="DESCUENTO 🟢" if smc15["discount"] else "PREMIUM 🔴"
    else:
        ob=smc5["ob_bear"]; fvg=smc5["fvg_bear"] or smc1["fvg_bear"]
        if ob and fvg: entrada=(ob[0]+ob[1]+fvg[0]+fvg[1])/4; calidad="💎 SNIPER OB+FVG 🔥"
        elif ob: entrada=(ob[0]+ob[1])/2; calidad="✅ OB 50%"
        elif fvg: entrada=(fvg[0]+fvg[1])/2; calidad="✅ FVG 50%"
        else: entrada=smc5["last_high"]*0.999; calidad="⚠️ SWING"
        sl=smc5["last_high"]*1.002; tp1=entrada*0.993; tp2=entrada*0.985; tp3=smc15["last_low"]
        zona="PREMIUM 🟢" if smc15["premium"] else "DESCUENTO 🔴"
    riesgo_usd=SALDO*0.01; distancia_sl=abs(entrada-sl); lote=round(riesgo_usd/distancia_sl/precio*1000,2) if distancia_sl>0 else 0.02; lote=max(0.01,min(lote,0.1))
    plt.figure(figsize=(12,6)); plt.style.use('dark_background')
    plt.plot(df1m["c"].tail(80).values, color="white", lw=1.5, label="BTC 1m")
    if smc5["ob_bull"]: plt.axhspan(smc5["ob_bull"][0], smc5["ob_bull"][1], color="blue", alpha=0.3, label="OB BULL")
    if smc5["ob_bear"]: plt.axhspan(smc5["ob_bear"][0], smc5["ob_bear"][1], color="orange", alpha=0.3, label="OB BEAR")
    if smc5["fvg_bull"]: plt.axhspan(smc5["fvg_bull"][0], smc5["fvg_bull"][1], color="cyan", alpha=0.2, label="FVG BULL")
    if smc5["fvg_bear"]: plt.axhspan(smc5["fvg_bear"][0], smc5["fvg_bear"][1], color="magenta", alpha=0.2, label="FVG BEAR")
    plt.axhline(entrada, color="cyan", lw=2, label=f"ENT {entrada:.2f}"); plt.axhline(sl, color="red", ls="--", lw=2, label=f"SL {sl:.2f}")
    plt.axhline(tp1, color="lime", ls="--", label=f"TP1 {tp1:.2f}"); plt.axhline(tp2, color="green", label=f"TP2 {tp2:.2f}")
    plt.axhline(smc15["eq"], color="gray", ls=":", label="EQ 50%")
    plt.title(f"{tendencia} | {calidad} | {kz_text}", fontsize=9)
    plt.legend(fontsize=6, loc="upper left"); plt.grid(alpha=0.15)
    plt.tight_layout(); plt.savefig("/tmp/v20final.png", facecolor="#0e0e0e", dpi=150); plt.close()
    efect=mem["ganadas"]/(mem["ganadas"]+mem["perdidas"])*100 if mem["ganadas"]+mem["perdidas"]>0 else 0
    texto=f"""🤖 CHALI V20 SMC FINAL

{kz_text} {'✅ OPERABLE' if kz_ok else '⚠️ ESPERAR'}
📊 {tendencia} | {calidad}
📦 OB: {smc5['ob_bull'] or smc5['ob_bear'] or 'NO'} | FVG: {smc5['fvg_bull'] or smc5['fvg_bear'] or 'NO'}
💧 Liq: {'Lows ✅' if smc1['liq_compra'] else 'Highs ✅' if smc1['liq_venta'] else 'No'} | {zona} EQ {smc15['eq']:.2f}

💵 Precio: {precio:.2f}
📍 ENT (50% OB/FVG): {entrada:.2f}
🛑 SL: {sl:.2f}
🎯 TP1: {tp1:.2f} TP2: {tp2:.2f} TP3: {tp3:.2f}

💼 Saldo ${SALDO} Riesgo ${riesgo_usd:.2f} Lote {lote}
🧠 {mem['ganadas']}G {mem['perdidas']}P {efect:.1f}%
"""
    return "/tmp/v20final.png", texto, kz_ok

def send_photo_buttons(cid, path, cap):
    url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    keyboard={"inline_keyboard":[[{"text":"✅ Ganada","callback_data":"ganada"},{"text":"❌ Perdida","callback_data":"perdida"}]]}
    with open(path,'rb') as f:
        requests.post(url, data={"chat_id":cid,"caption":cap,"reply_markup":json.dumps(keyboard)}, files={"photo":f}, timeout=20)

def send_msg(cid, txt):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id":cid,"text":txt})

# ESTRATEGIA FIJA PARA QUE NO LA INVENTE
TEXTO_ESTRATEGIA = """**CHALI V20 SMC ESTRATEGIA REAL - TRADING**

**Conceptos SMC que usa el bot:**

- **OB (Order Block):** Ultima vela bajista antes de un impulso alcista fuerte (o al reves). Es donde los bancos ponen ordenes. Entramos al 50% del OB.

- **FVG (Fair Value Gap / Imbalance):** Hueco que deja el precio cuando se mueve fuerte. El precio siempre vuelve a tapar ese hueco. Si FVG + OB coinciden = ENTRADA SNIPER 💎

- **BOS (Break of Structure):** Cuando el precio rompe el ultimo maximo/minimo. Confirma tendencia.

- **CHoCH (Change of Character):** Cuando rompe en contra de la tendencia, indica cambio de tendencia.

- **Liquidacion:** Cuando el precio hace una mecha por arriba/debajo de un maximo/minimo para agarrar los stops y luego se da vuelta. Entramos despues de la liquidacion.

- **Premium/Discount:** Si el precio esta arriba del 50% del rango del dia es PREMIUM (caro para comprar). Si esta abajo es DISCOUNT (barato).

**Como opera:**
1. Espera Killzone NY/London
2. Espera BOS + OB + FVG
3. Entrada LIMIT al 50% del OB/FVG, no a mercado
4. SL debajo de la estructura
5. TP1 0.7% TP2 1.5% TP3 liquidez opuesta
6. Gestion 1% por operacion

Usa /v20 para señal.
"""

@app.route("/")
def home(): return "V20 FINAL FIX"

@app.route("/webhook", methods=["POST"])
def webhook():
    data=request.get_json()
    if "callback_query" in data:
        cq=data["callback_query"]; cid=cq["message"]["chat"]["id"]; dato=cq["data"]
        mem=load_memory()
        if dato=="ganada": mem["ganadas"]+=1
        else: mem["perdidas"]+=1
        save_memory(mem)
        efect=mem["ganadas"]/(mem["ganadas"]+mem["perdidas"])*100
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={"callback_query_id":cq["id"],"text":f"Guardado! {efect:.1f}%"})
        send_msg(cid, f"🧠 Guardado {dato} | {mem['ganadas']}G {mem['perdidas']}P {efect:.1f}%")
        return "ok",200

    if "message" in data:
        cid=data["message"]["chat"]["id"]
        text_raw=data["message"].get("text","")
        t=text_raw.lower()

        # FIX PARA QUE NO SE VAYA A GAMER
        if "estrategia" in t:
            send_msg(cid, TEXTO_ESTRATEGIA)
            return "ok",200

        if "/v20" in t or "/v21" in t or "/smc" in t or "btc" in t or "oro" in t:
            p,txt,kz=analisis_final()
            if p: send_photo_buttons(cid,p,txt)
            else: send_msg(cid,txt)
        else:
            # PROMPT BLINDADO ANTI-GAMER
            try:
                system_prompt = """Sos Charli V20, trader profesional SMC INSTITUCIONAL.
                DEFINICIONES OBLIGATORIAS:
                - OB = Order Block de trading, ultima vela contraria antes del impulso. NO es bloqueo de juegos.
                - FVG = Fair Value Gap / Imbalance / Hueco de liquidez en trading. NO es movilidad.
                - BOS = Break of Structure, rompimiento de maximo/minimo en trading.
                - CHoCH = Change of Character, cambio de tendencia en trading.
                - Liquidacion = Barrido de liquidez, mecha que toma stops.
                PROHIBIDO hablar de videojuegos, enemigos, curaciones, buffs, memoria RAM, 18GB. Si te preguntan por estrategia, explica trading SMC.
                Siempre das ENT, SL, TP con precios."""
                comp=client.chat.completions.create(model=MODELO, messages=[{"role":"system","content":system_prompt},{"role":"user","content":text_raw}], max_tokens=700)
                send_msg(cid, comp.choices[0].message.content)
            except Exception as e:
                send_msg(cid, f"Error: {e}")
    return "ok",200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))

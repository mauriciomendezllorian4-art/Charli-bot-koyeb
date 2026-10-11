import os, json, requests, sqlite3
import pandas as pd, numpy as np
from flask import Flask, request
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime

app = Flask(__name__)
TOKEN = os.environ.get("TELEGRAM_TOKEN")
API_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}"
DB = "charli.db"
SECRET = os.environ.get("TV_SECRET","chali123")

def init_db():
    con=sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, chat_id TEXT, entrada REAL, salida REAL, pnl REAL, resultado TEXT, fecha TEXT, simbolo TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS usuarios (chat_id TEXT PRIMARY KEY, posicion TEXT, notas TEXT, saldo REAL DEFAULT 1000)")
    con.commit(); con.close()
init_db()

def get_saldo(chat_id):
    con=sqlite3.connect(DB); cur=con.cursor()
    cur.execute("SELECT saldo FROM usuarios WHERE chat_id=?", (str(chat_id),))
    r=cur.fetchone(); con.close()
    return r[0] if r and r[0] else 1000

def guardar_saldo(chat_id, saldo):
    con=sqlite3.connect(DB); cur=con.cursor()
    cur.execute("INSERT INTO usuarios (chat_id,saldo) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET saldo=?", (str(chat_id), saldo, saldo))
    con.commit(); con.close()

def add_trade(chat_id, entrada, salida, simbolo, resultado):
    pnl=abs(salida-entrada) if resultado=="GANADA" else -abs(salida-entrada)
    con=sqlite3.connect(DB); cur=con.cursor()
    cur.execute("INSERT INTO trades (chat_id,entrada,salida,pnl,resultado,fecha,simbolo) VALUES (?,?,?,?,?,datetime('now'),?)",(str(chat_id),entrada,salida,pnl,resultado,simbolo))
    con.commit(); con.close()

def get_noticias_rojas():
    try:
        r=requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10).json()
        ahora=datetime.utcnow()
        alertas=[]
        for n in r:
            if n.get("impact")=="High" and n.get("currency") in ["USD"]:
                try:
                    fecha=datetime.strptime(n["date"]+" "+n.get("time",""), "%Y-%m-%d %I:%M%p")
                    diff=(fecha-ahora).total_seconds()/3600
                    if -0.5 <= diff <= 2:
                        alertas.append(f"🔴 {n['title']} en {diff:.1f}h")
                except: pass
        return alertas
    except: return []

def chali_v16_analisis(ticker="BTC/USD"):
    try:
        if "BTC" in ticker:
            r=requests.get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=200", timeout=10).json()
            df=pd.DataFrame(r, columns=["ot","open","high","low","close","vol","ct","q","n","tb","tq","ig"])
            df=df.astype({"open":float,"high":float,"low":float,"close":float})
        else:
            TD_KEY=os.environ.get("TWELVEDATA_KEY")
            j=requests.get(f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=1day&outputsize=100&apikey={TD_KEY}", timeout=15).json()
            df=pd.DataFrame(j.get("values",[])[::-1]).astype({"open":float,"high":float,"low":float,"close":float})
        df["tr"]=np.maximum(df["high"]-df["low"], np.maximum(abs(df["high"]-df["close"].shift(1)), abs(df["low"]-df["close"].shift(1))))
        df["atr"]=df["tr"].rolling(20).mean()
        df["conv"]=(df["high"].rolling(9).max()+df["low"].rolling(9).min())/2
        df["base"]=(df["high"].rolling(26).max()+df["low"].rolling(26).min())/2
        df["leadA"]=((df["conv"]+df["base"])/2).shift(26)
        df["leadB"]=((df["high"].rolling(52).max()+df["low"].rolling(52).min())/2).shift(26)
        df["cloudTop"]=df[["leadA","leadB"]].max(axis=1)
        df["cloudBot"]=df[["leadA","leadB"]].min(axis=1)
        close=df.iloc[-1]["close"]; atr=df.iloc[-1]["atr"]
        aboveCloud=close>df.iloc[-1]["cloudTop"] if not pd.isna(df.iloc[-1]["cloudTop"]) else True
        belowCloud=close<df.iloc[-1]["cloudBot"] if not pd.isna(df.iloc[-1]["cloudBot"]) else True
        vE=vSL=vTP2=None; senial="ESPERAR"
        if close > df["high"].iloc[-20:-1].max() and aboveCloud:
            vE=close; vSL=close-atr*2.0; vTP2=close+atr*3.0; senial="BOS ALCISTA + NUBE - COMPRA"
        elif close < df["low"].iloc[-20:-1].min() and belowCloud:
            vE=close; vSL=close+atr*2.0; vTP2=close-atr*3.0; senial="BOS BAJISTA + NUBE - VENTA"
        return {"ticker":ticker, "close":close, "atr":atr, "senial":senial, "entrada":vE, "sl":vSL, "tp2":vTP2, "df":df}
    except Exception as e:
        print(e); return None

def calcular_lotaje(chat_id, analisis):
    saldo=get_saldo(chat_id); sl_dist=abs(analisis["entrada"]-analisis["sl"]) if analisis["entrada"] else analisis["atr"]*2
    lote_normal=(saldo*0.01)/sl_dist
    if "XAU" in analisis["ticker"] or "GOLD" in analisis["ticker"]: lote_normal=lote_normal/100
    lote_hmr=lote_normal/3
    return round(max(0.01,min(lote_normal,0.5)),2), round(max(0.01,min(lote_hmr,0.15)),2), saldo

def mandar_grafico_chali(chat_id, analisis):
    df=analisis["df"].tail(80)
    plt.figure(figsize=(12,6)); plt.style.use('dark_background')
    plt.plot(df["close"].values, color='white', linewidth=1.5)
    plt.plot(df["cloudTop"].values, color='green', alpha=0.25)
    plt.plot(df["cloudBot"].values, color='red', alpha=0.25)
    if analisis["entrada"]:
        plt.axhline(analisis["entrada"], color='yellow', linestyle='--')
        plt.axhline(analisis["sl"], color='red', linestyle='--')
        plt.axhline(analisis["tp2"], color='cyan', linestyle='--')
    plt.title(f"{analisis['ticker']} {analisis['senial']}", color='white'); plt.grid(alpha=0.2); plt.tight_layout()
    plt.savefig("/tmp/chali.png", facecolor='#131722', dpi=150); plt.close()
    lote_normal, lote_hmr, saldo=calcular_lotaje(chat_id, analisis)
    noticias=get_noticias_rojas()
    alerta="⚠️ HMR: "+"\n".join(noticias) if noticias else "✅ Sin noticias rojas"
    caption=f"🤖 CHALI V16 {analisis['ticker']}\n{analisis['senial']}\nPrecio: {analisis['close']:.2f}\n\nENT: {analisis['entrada']}\nSL: {analisis['sl']}\nTP2: {analisis['tp2']}\n\n💰 Saldo ${saldo}\n📦 Normal {lote_normal} | HMR {lote_hmr}\n\n{alerta}"
    keyboard={"inline_keyboard": [[{"text":"✅ GANÉ","callback_data":f"gane_{analisis['entrada']:.2f}_{analisis['ticker']}"},{"text":"❌ PERDÍ","callback_data":f"perdi_{analisis['entrada']:.2f}_{analisis['ticker']}"}]]}
    with open("/tmp/chali.png","rb") as f:
        requests.post(f"{API_TELEGRAM}/sendPhoto", data={"chat_id":chat_id, "caption":caption, "reply_markup":json.dumps(keyboard)}, files={"photo":f})

@app.route("/tv_webhook", methods=["POST"])
def tv_webhook():
    data=request.get_json(force=True, silent=True) or {}
    if data.get("secret") and data.get("secret")!=SECRET: return "forbidden",403
    noticias=get_noticias_rojas()
    con=sqlite3.connect(DB); cur=con.cursor(); cur.execute("SELECT chat_id FROM usuarios"); usuarios=cur.fetchall(); con.close()
    if noticias:
        for (chat_id,) in usuarios:
            requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":"⛔ Señal bloqueada por noticia roja:\n"+"\n".join(noticias)})
        return {"status":"bloqueado"},200
    for (chat_id,) in usuarios:
        a=chali_v16_analisis("BTC/USD" if "BTC" in data.get("ticker","BTCUSDT") else "XAU/USD")
        if a: mandar_grafico_chali(chat_id, a)
    return {"status":"ok"},200

@app.route("/")
def home(): return "Chali V16 HMR listo"

@app.route("/webhook", methods=["POST"])
def webhook():
    data=request.get_json()
    try:
        if "callback_query" in data:
            cb=data["callback_query"]; chat_id=cb["message"]["chat"]["id"]; d=cb["data"]
            if d.startswith("gane_"):
                _, ent, tick = d.split("_",2)
                add_trade(chat_id, float(ent), float(ent)*1.01, tick, "GANADA")
                requests.post(f"{API_TELEGRAM}/answerCallbackQuery", json={"callback_query_id":cb["id"], "text":"GANADA ✅"})
            else:
                _, ent, tick = d.split("_",2)
                add_trade(chat_id, float(ent), float(ent)*0.99, tick, "PERDIDA")
                requests.post(f"{API_TELEGRAM}/answerCallbackQuery", json={"callback_query_id":cb["id"], "text":"PERDIDA ❌"})
            return "ok",200
        if "message" in data and "text" in data["message"]:
            chat_id=data["message"]["chat"]["id"]; text=data["message"]["text"]; tl=text.lower()
            if tl.startswith("/saldo"):
                try:
                    s=float(text.split()[1]); guardar_saldo(chat_id, s)
                    requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":f"Saldo ${s} guardado"})
                except:
                    requests.post(f"{API_TELEGRAM}/sendMessage", json={"chat_id":chat_id,"text":f"Usa /saldo 500"})
                return "ok",200
            if "/v16" in tl or "/btc" in tl or "/chali" in tl:
                a=chali_v16_analisis("BTC/USD")
                if a: mandar_grafico_chali(chat_id, a)
                return "ok",200
    except Exception as e: print(e)
    return "ok",200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=10000)

rom flask import Flask, request
import os, json, time, threading, requests

app = Flask(_name_)
TG = os.environ.get("TELEGRAM_TOKEN")
GROQ = os.environ.get("GROQ_API_KEY")
TD = os.environ.get("TWELVEDATA_KEY")
API = f"https://api.telegram.org/bot{TG}"
ARCHIVO = "memoria.json"
historial = {}
cache_bt = {}
TFS = {"M5": "5min", "M15": "15min", "M30": "30min",
       "H1": "1h", "H4": "4h", "D1": "1day"}
MULT_SL, RR, COSTO_R = 1.5, 2.0, 0.1

SISTEMA = ("Sos Charli, mentor y analista de trading. Hablás en español "
           "rioplatense, claro y directo. Siempre marcás el riesgo. Nunca "
           "prometés ganancias. Recomendás arriesgar como máximo 1% por operación.")

def cargar():
    try:
        with open(ARCHIVO) as f:
            return json.load(f)
    except Exception:
        return []

def guardar(lista):
    with open(ARCHIVO, "w") as f:
        json.dump(lista, f)

def groq(sistema, mensajes):
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ}"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "system", "content": sistema}] + mensajes},
            timeout=30)
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print("Error IA:", e)
        return "Tuve un problema con la IA, probá de nuevo."

def sistema_con_lecciones():
    lec = cargar()
    s = SISTEMA
    if lec:
        s += "\nCosas que aprendiste:\n- " + "\n- ".join(lec)
    return s

# ---------- indicadores ----------
def ema_s(v, n):
    k = 2 / (n + 1)
    out = [v[0]]
    for x in v[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out

def rsi_s(c, n=14):
    out = [50.0] * len(c)
    if len(c) <= n:
        return out
    g = [max(c[i] - c[i-1], 0) for i in range(1, len(c))]
    p = [max(c[i-1] - c[i], 0) for i in range(1, len(c))]
    ag, ap = sum(g[:n]) / n, sum(p[:n]) / n
    for i in range(n, len(c)):
        if i > n:
            ag = (ag * (n - 1) + g[i-1]) / n
            ap = (ap * (n - 1) + p[i-1]) / n
        out[i] = 100.0 if ap == 0 else 100 - 100 / (1 + ag / ap)
    return out

def atr_s(h, l, c, n=14):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
                          for i in range(1, len(c))]
    out = [tr[0]] * len(c)
    if len(c) <= n:
        return out
    a = sum(tr[1:n+1]) / n
    for i in range(n, len(c)):
        if i > n:
            a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out

def indicadores(h, l, c):
    return ema_s(c, 20), ema_s(c, 50), ema_s(c, 200), rsi_s(c), atr_s(h, l, c)

def lado_en(i, c, e20, e50, e200, rs, at):
    if i < 200:
        return None
    if abs(e20[i] - e50[i]) < 0.3 * at[i]:
        return None
    if e20[i] > e50[i] and c[i] > e200[i] and 50 < rs[i] < 70:
        return 1
    if e20[i] < e50[i] and c[i] < e200[i] and 30 < rs[i] < 50:
        return -1
    return None

# ---------- datos ----------
def traer(simbolo, tf, n=5000):
    par = simbolo[:3] + "/" + simbolo[3:]
    try:
        r = requests.get("https://api.twelvedata.com/time_series",
                         params={"symbol": par, "interval": TFS[tf],
                                 "outputsize": n, "apikey": TD},
                         timeout=30).json()
    except Exception as e:
        print("Error datos:", e)
        return None
    if "values" not in r:
        print("Twelve Data:", r)
        return None
    v = list(reversed(r["values"]))
    return ([float(x["open"]) for x in v], [float(x["high"]) for x in v],
            [float(x["low"]) for x in v], [float(x["close"]) for x in v])

# ---------- backtest ----------
def simular(o, h, l, c):
    e20, e50, e200, rs, at = indicadores(h, l, c)
    n = len(c)
    trades = []
    i = 200
    while i < n - 1:
        s = lado_en(i, c, e20, e50, e200, rs, at)
        if s and lado_en(i-1, c, e20, e50, e200, rs, at) != s:
            ent = o[i+1]
            riesgo = MULT_SL * at[i]
            if riesgo <= 0:
                i += 1
                continue
            sl = ent - s * riesgo
            tp = ent + s * riesgo * RR
            res, j = None, i + 1
            while j < n:
                if s == 1:
                    toca_sl, toca_tp = l[j] <= sl, h[j] >= tp
                else:
                    toca_sl, toca_tp = h[j] >= sl, l[j] <= tp
                if toca_sl:
                    res = -1.0
                    break
                if toca_tp:
                    res = RR
                    break
                j += 1
            if res is None:
                break
            trades.append((i, res - COSTO_R))
            i = j + 1
        else:
            i += 1
    return trades

def stats(rs_):
    n = len(rs_)
    if n == 0:
        return {"n": 0, "wr": 0, "exp": 0, "pf": 0, "dd": 0}
    gan = sum(x for x in rs_ if x > 0)
    per = -sum(x for x in rs_ if x < 0)
    eq = pico = dd = 0
    for x in rs_:
        eq += x
        pico = max(pico, eq)
        dd = max(dd, pico - eq)
    return {"n": n, "wr": round(100 * sum(1 for x in rs_ if x > 0) / n, 1),
            "exp": round(sum(rs_) / n, 2),
            "pf": round(gan / per, 2) if per > 0 else 99,
            "dd": round(dd, 1)}

def backtest(simbolo, tf):
    k = (simbolo, tf)
    if k in cache_bt and time.time() - cache_bt[k]["t"] < 6 * 3600:
        return cache_bt[k]
    d = traer(simbolo, tf)
    if not d:
        return None
    o, h, l, c = d
    tr = simular(o, h, l, c)
    corte = int(len(c) * 0.7)
    res = {"t": time.time(), "velas": len(c),
           "todo": stats([r for _, r in tr]),
           "ins": stats([r for i, r in tr if i < corte]),
           "oos": stats([r for i, r in tr if i >= corte])}
    cache_bt[k] = res
    return res

def aprobado(res):
    t, o_ = res["todo"], res["oos"]
    return (t["n"] >= 30 and t["exp"] > 0 and t["pf"] >= 1.2
            and o_["n"] >= 10 and o_["exp"] > 0)

def linea(nombre, s):
    return (f"{nombre}: {s['n']} ops | acierto {s['wr']}% | "
            f"prom {s['exp']}R | PF {s['pf']} | DD {s['dd']}R")

def texto_bt(simbolo, tf, res):
    ok = "APROBADA" if aprobado(res) else "NO APROBADA"
    return (f"Backtest {simbolo} {tf} ({res['velas']} velas)\n"
            f"SL 1.5xATR, TP 1:2, costo 0.1R\n"
            f"{linea('Todo', res['todo'])}\n"
            f"{linea('Ajuste 70%', res['ins'])}\n"
            f"{linea('Prueba 30%', res['oos'])}\n"
            f"Resultado: {ok}")

# ---------- señal ----------
def senal(simbolo, tf):
    bt = backtest(simbolo, tf)
    if not bt:
        return "No pude traer precios. Revisá el símbolo (ej: XAUUSD) y la key."
    if not aprobado(bt):
        return ("NO OPERAR: la estrategia no tiene ventaja comprobada en "
                f"{simbolo} {tf}.\n\n" + texto_bt(simbolo, tf, bt))
    d = traer(simbolo, tf, 250)
    if not d:
        return "No pude traer los precios actuales."
    o, h, l, c = d
    e20, e50, e200, rs, at = indicadores(h, l, c)
    i = len(c) - 1
    s = lado_en(i, c, e20, e50, e200, rs, at)
    if not s or lado_en(i-1, c, e20, e50, e200, rs, at) == s:
        return (f"{simbolo} {tf}: la estrategia está aprobada pero ahora no hay "
                f"setup nuevo. Precio {round(c[i], 5)}, RSI {round(rs[i], 1)}. "
                "Esperá.")
    dec = 5 if c[i] < 10 else 2
    p, riesgo = c[i], MULT_SL * at[i]
    a = {"par": simbolo, "tf": tf, "lado": "COMPRA" if s == 1 else "VENTA",
         "entrada": round(p, dec), "sl": round(p - s * riesgo, dec),
         "tp1": round(p + s * riesgo * 1.5, dec),
         "tp2": round(p + s * riesgo * RR, dec),
         "rsi": round(rs[i], 1), "ema20": round(e20[i], dec),
         "ema50": round(e50[i], dec), "ema200": round(e200[i], dec)}
    base = (f"{simbolo} {tf} - posible {a['lado']}\n"
            f"Entrada: {a['entrada']}\nSL: {a['sl']}\n"
            f"TP1: {a['tp1']} (1:1.5)\nTP2: {a['tp2']} (1:2)\n\n"
            + linea("Backtest", bt["todo"]))
    pedido = (f"Datos reales: {json.dumps(a)}. Sin cambiar ningún número, "
              "explicá en 4 líneas por qué tiene sentido, qué la invalida y "
              "cómo cuidar el riesgo.")
    return base + "\n\n" + groq(sistema_con_lecciones(),
                                [{"role": "user", "content": pedido}])

def enviar(chat_id, texto):
    try:
        requests.post(f"{API}/sendMessage",
                      json={"chat_id": chat_id, "text": texto[:4000]}, timeout=10)
    except Exception as e:
        print("Error enviar:", e)

def procesar(chat_id, t):
    try:
        partes = t.split()
        cmd = partes[0].lower() if partes else ""
        sim = partes[1].upper() if len(partes) > 1 else "XAUUSD"
        tf = partes[2].upper() if len(partes) > 2 else "H1"
        if cmd in ("/senal", "/backtest") and tf not in TFS:
            enviar(chat_id, "Temporalidad válida: M5, M15, M30, H1, H4, D1")
        elif cmd == "/senal":
            enviar(chat_id, "Analizando, un momento...")
            enviar(chat_id, senal(sim, tf))
        elif cmd == "/backtest":
            enviar(chat_id, "Corriendo backtest, un momento...")
            bt = backtest(sim, tf)
            enviar(chat_id, texto_bt(sim, tf, bt) if bt else
                   "No pude traer los datos. Revisá símbolo y key.")
        elif cmd == "/backtestall":
            enviar(chat_id, "Corriendo backtest de 8 combinaciones, tarda 1 o 2 minutos...")
            filas = []
            for s_ in ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]:
                for f_ in ["H1", "H4"]:
                    cacheado = (s_, f_) in cache_bt
                    bt = backtest(s_, f_)
                    if not cacheado:
                        time.sleep(8)
                    if not bt:
                        filas.append(f"{s_} {f_}: sin datos")
                    else:
                        m = "OK" if aprobado(bt) else "NO"
                        t_ = bt["todo"]
                        filas.append(f"{m} {s_} {f_}: {t_['n']} ops, "
                                     f"prom {t_['exp']}R, PF {t_['pf']}")
            enviar(chat_id, "Resumen:\n" + "\n".join(filas))
        elif cmd == "/aprende":
            lista = cargar()
            lista.append(t[len("/aprende"):].strip())
            guardar(lista)
            enviar(chat_id, "Anotado, lo voy a recordar.")
        elif cmd == "/olvidar":
            guardar([])
            enviar(chat_id, "Listo, borré lo aprendido.")
        else:
            h = historial.setdefault(chat_id, [])
            h.append({"role": "user", "content": t})
            del h[:-20]
            resp = groq(sistema_con_lecciones(), h)
            h.append({"role": "assistant", "content": resp})
            enviar(chat_id, resp)
    except Exception as e:
        print("Error:", e)
        enviar(chat_id, "Algo falló, probá de nuevo en un minuto.")

@app.route('/')
def home():
    return "CHALI V17 SALTA - ONLINE", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(silent=True) or {}
    msg = data.get("message")
    if msg and "text" in msg:
        threading.Thread(target=procesar,
                         args=(msg["chat"]["id"], msg["text"].strip()),
                         daemon=True).start()
    return "OK", 200

if _name_ == "_main_":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
    
    

            
       
  

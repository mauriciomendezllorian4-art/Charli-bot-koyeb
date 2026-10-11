# ============================================================
# CHALI IA v2 - ORO + BTC
# Telegram + Groq + SQLite + análisis multi-timeframe (estructura/SMC)
# + seguimiento automático de señales + backtest + filtros de riesgo
# ============================================================
# Variables de entorno (Render > Environment):
#   TELEGRAM_TOKEN, GROQ_API_KEY, TWELVEDATA_API_KEY   (obligatorias)
#   TELEGRAM_ADMIN_ID     tu chat id (varios separados por coma). OBLIGATORIA
#   TELEGRAM_WEBHOOK_SECRET   (opcional)
#   GROQ_MODEL            (opcional; si no existe se elige uno disponible)
#   ACCOUNT_BALANCE, RISK_PCT   (opcional; para calcular tamaño de posición)
#   COSTO_ORO, COSTO_BTC  (opcional; spread+comisión en precio, p.ej. 0.40 / 15)
#   TZ_OFFSET_HORAS       (opcional; Argentina = -3)
#   AUTO_SCAN=1           (opcional; escanea cada 15 min y te avisa)
#   DATABASE_PATH         (opcional; /var/data/chali.db si hay disco)
# ============================================================

import os
import re
import time
import json
import sqlite3
import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests
import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from flask import Flask, request, jsonify

try:
    import fcntl
except ImportError:  # Windows (solo para pruebas locales)
    fcntl = None

# ---------------- CONFIGURACION ----------------

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


def env(nombre, defecto=""):
    return os.getenv(nombre, defecto).strip()


def env_float(nombre, defecto):
    try:
        return float(env(nombre, str(defecto)))
    except ValueError:
        return float(defecto)


TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
GROQ_API_KEY = env("GROQ_API_KEY")
TWELVEDATA_API_KEY = env("TWELVEDATA_API_KEY")
GROQ_MODEL = env("GROQ_MODEL")
WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET")
ADMIN_IDS = [x.strip() for x in env("TELEGRAM_ADMIN_ID").split(",") if x.strip()]

BALANCE = env_float("ACCOUNT_BALANCE", 0)
RISK_PCT = env_float("RISK_PCT", 1)
TZ_OFFSET = int(env_float("TZ_OFFSET_HORAS", -3))
AUTO_SCAN = env("AUTO_SCAN") == "1"

COSTO = {
    "ORO": env_float("COSTO_ORO", 0.40),
    "BTC": env_float("COSTO_BTC", 15.0),
}

MERCADOS = {
    "ORO": {"nombre": "ORO XAU/USD"},
    "BTC": {"nombre": "BTC/USDT"},
}

CHART_DIR = "/tmp"
UMBRAL_SCORE = 6      # puntaje mínimo (de 9) para emitir señal
RR1, RR2 = 1.5, 3.0   # TP1 y TP2 en múltiplos de R
R_TP1 = 0.5 * RR1     # cierra 50% en TP1
R_TP2 = R_TP1 + 0.5 * RR2

DATABASE_PATH = env("DATABASE_PATH", "/var/data/chali.db")
if not os.path.isdir(os.path.dirname(DATABASE_PATH) or "."):
    DATABASE_PATH = "/tmp/chali.db"
    logging.warning(
        "Sin disco persistente: la base se guarda en /tmp y se borra "
        "en cada reinicio/deploy."
    )

session = requests.Session()
db_lock = threading.RLock()
ejecutor = ThreadPoolExecutor(max_workers=4)
updates_vistos = deque(maxlen=300)
ULTIMO_INFORME = {}


# ---------------- UTILIDADES ----------------

def limpiar(texto):
    """Quita claves/tokens de cualquier texto antes de loguear o enviar."""
    texto = str(texto)
    for secreto in (TELEGRAM_TOKEN, GROQ_API_KEY, TWELVEDATA_API_KEY):
        if secreto:
            texto = texto.replace(secreto, "***")
    return re.sub(r"(apikey|api_key|token)=[^&\s]+", r"\1=***", texto,
                  flags=re.I)


def ahora_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return pd.Timestamp(dt).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S") \
        if pd.Timestamp(dt).tzinfo else \
        pd.Timestamp(dt).strftime("%Y-%m-%dT%H:%M:%S")


def hora_local(ts):
    return (pd.Timestamp(ts) + timedelta(hours=TZ_OFFSET)).strftime(
        "%d/%m %H:%M")


def fmt(x):
    return "N/D" if x is None else f"{x:,.2f}"


# ---------------- BASE DE DATOS ----------------

def db(sql, params=(), uno=False, todos=False):
    with db_lock:
        con = sqlite3.connect(DATABASE_PATH, timeout=20)
        try:
            con.execute("PRAGMA busy_timeout=20000")
            cur = con.execute(sql, params)
            res = None
            if uno:
                res = cur.fetchone()
            elif todos:
                res = cur.fetchall()
            con.commit()
            return res if (uno or todos) else cur.lastrowid
        finally:
            con.close()


def iniciar_db():
    db("""CREATE TABLE IF NOT EXISTS mensajes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, rol TEXT,
        contenido TEXT, fecha TEXT)""")
    db("""CREATE TABLE IF NOT EXISTS recuerdos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, recuerdo TEXT,
        fecha TEXT)""")
    db("""CREATE TABLE IF NOT EXISTS senales (
        id INTEGER PRIMARY KEY AUTOINCREMENT, mercado TEXT, direccion INTEGER,
        entrada REAL, sl REAL, tp1 REAL, tp2 REAL, score INTEGER,
        razones TEXT, fecha TEXT, creada TEXT,
        estado TEXT DEFAULT 'ABIERTA', tp1_hit INTEGER DEFAULT 0,
        r REAL, fecha_cierre TEXT, chat_id TEXT)""")
    db("""CREATE TABLE IF NOT EXISTS noticias (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT, texto TEXT)""")


iniciar_db()


def guardar_mensaje(chat_id, rol, contenido):
    try:
        db("INSERT INTO mensajes (chat_id, rol, contenido, fecha) "
           "VALUES (?,?,?,?)",
           (str(chat_id), rol, str(contenido)[:12000], iso(ahora_utc())))
    except Exception:
        logging.exception("Error guardando mensaje")


def obtener_memoria(chat_id, limite=12):
    recuerdos = db("SELECT recuerdo FROM recuerdos WHERE chat_id=? "
                   "ORDER BY id DESC LIMIT 20", (str(chat_id),), todos=True)
    mensajes = db("SELECT rol, contenido FROM mensajes WHERE chat_id=? "
                  "ORDER BY id DESC LIMIT ?", (str(chat_id), limite),
                  todos=True)
    texto = "\n".join("- " + r[0] for r in reversed(recuerdos))
    hist = [{"role": r, "content": c} for r, c in reversed(mensajes)
            if r in ("user", "assistant")]
    return texto, hist


# ---------------- TELEGRAM ----------------

def telegram_api(metodo, datos=None, archivos=None, timeout=30):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN")
    r = session.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{metodo}",
        data=datos, files=archivos, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"Telegram {r.status_code}: {limpiar(r.text[:300])}")
    return r.json()


def enviar_texto(chat_id, texto):
    texto = str(texto)
    for i in range(0, len(texto), 3900):
        telegram_api("sendMessage",
                     {"chat_id": str(chat_id), "text": texto[i:i + 3900]})


def enviar_grafico(chat_id, ruta, informe):
    if ruta and os.path.exists(ruta):
        with open(ruta, "rb") as foto:
            telegram_api("sendPhoto",
                         datos={"chat_id": str(chat_id),
                                "caption": informe[:1000]},
                         archivos={"photo": foto})
        if len(informe) > 1000:
            enviar_texto(chat_id, informe[1000:])
    else:
        enviar_texto(chat_id, informe)


# ---------------- DATOS DE MERCADO ----------------
# Se descarga SOLO 15m y se arman 1h y 4h por resample (1 llamada por análisis).

BINANCE_HOSTS = [
    "https://api.binance.com",
    "https://data-api.binance.vision",
    "https://api.binance.us",
]


def _binance_get(params):
    ultimo = None
    for host in BINANCE_HOSTS:
        try:
            r = session.get(host + "/api/v3/klines", params=params,
                            timeout=20)
            if r.ok:
                return r.json()
            ultimo = f"{host} -> {r.status_code}"
        except requests.RequestException as e:
            ultimo = f"{host} -> {limpiar(e)[:100]}"
    raise RuntimeError("Binance no respondió: " + str(ultimo))


def obtener_btc(n):
    filas, fin = [], None
    while len(filas) < n:
        lote = min(1000, n - len(filas))
        params = {"symbol": "BTCUSDT", "interval": "15m", "limit": lote}
        if fin:
            params["endTime"] = fin
        datos = _binance_get(params)
        if not isinstance(datos, list) or not datos:
            break
        filas = datos + filas
        fin = datos[0][0] - 1
        if len(datos) < lote:
            break
    if len(filas) < 300:
        raise RuntimeError("Binance devolvió pocas velas")
    df = pd.DataFrame(filas, columns=[
        "time", "open", "high", "low", "close", "volume", "ct", "qv",
        "trades", "tb", "tq", "ig"])
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["time", "open", "high", "low", "close", "volume"]]


def obtener_oro(n):
    if not TWELVEDATA_API_KEY:
        raise RuntimeError("Falta TWELVEDATA_API_KEY")
    r = session.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "XAU/USD", "interval": "15min",   # <- "15min"
                "outputsize": min(n, 5000), "timezone": "UTC",
                "apikey": TWELVEDATA_API_KEY, "format": "JSON",
                "order": "ASC"},
        timeout=30)
    try:
        datos = r.json()
    except ValueError:
        datos = {}
    if r.status_code != 200 or "values" not in datos:
        msg = datos.get("message") or r.text[:200]
        raise RuntimeError(f"Twelve Data {r.status_code}: {limpiar(msg)}")
    df = pd.DataFrame(datos["values"])
    df["time"] = pd.to_datetime(df["datetime"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce") \
        if "volume" in df.columns else 0.0
    return df[["time", "open", "high", "low", "close", "volume"]]


_cache = {}


def obtener_velas(mercado, n=1500, ttl=90):
    clave = (mercado, n)
    c = _cache.get(clave)
    if c and time.time() - c[0] < ttl:
        return c[1].copy()
    df = obtener_oro(n) if mercado == "ORO" else obtener_btc(n)
    df = (df.dropna(subset=["open", "high", "low", "close"])
            .drop_duplicates("time").sort_values("time")
            .reset_index(drop=True))
    _cache[clave] = (time.time(), df)
    return df.copy()


def resamplear(df, regla):
    x = df.set_index("time").resample(regla).agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}).dropna(subset=["close"])
    return x.reset_index()


# ---------------- INDICADORES Y ESTRUCTURA ----------------

def indicadores(df):
    df = df.copy()
    c = df["close"]
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    df["ema200"] = c.ewm(span=200, adjust=False).mean()
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    p = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = g / p.replace(0, np.nan)
    df["rsi"] = (100 - 100 / (1 + rs)).fillna(50)
    pc = c.shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return df


def sesgo(d):
    u = d.iloc[-1]
    if u["ema20"] > u["ema50"] and u["close"] > u["ema50"]:
        return 1
    if u["ema20"] < u["ema50"] and u["close"] < u["ema50"]:
        return -1
    return 0


def pivotes(df, n=3):
    h, l = df["high"].values, df["low"].values
    altos, bajos = [], []
    for i in range(n, len(df) - n):
        if h[i] > h[i - n:i].max() and h[i] >= h[i + 1:i + n + 1].max():
            altos.append((i, float(h[i])))
        if l[i] < l[i - n:i].min() and l[i] <= l[i + 1:i + n + 1].min():
            bajos.append((i, float(l[i])))
    return altos, bajos


def primera_ruptura(df, desde, nivel, arriba):
    cl = df["close"].values
    for k in range(desde + 1, len(cl)):
        if (cl[k] > nivel) if arriba else (cl[k] < nivel):
            return k
    return None


def buscar_ob(df, k, d):
    for j in range(k - 1, max(k - 16, -1), -1):
        o, c = df["open"].iat[j], df["close"].iat[j]
        if (d == 1 and c < o) or (d == -1 and c > o):
            return (float(df["low"].iat[j]), float(df["high"].iat[j]))
    return None


def detectar_fvg(df, ventana=40):
    h, l = df["high"].values, df["low"].values
    n = len(df)
    up = dn = None
    for i in range(max(2, n - ventana), n):
        if l[i] > h[i - 2] and not (l[i + 1:] <= h[i - 2]).any():
            up = (float(h[i - 2]), float(l[i]))
        if h[i] < l[i - 2] and not (h[i + 1:] >= l[i - 2]).any():
            dn = (float(h[i]), float(l[i - 2]))
    return up, dn


def estructura(df):
    n = len(df)
    fvg_up, fvg_dn = detectar_fvg(df)
    res = {"tendencia": "RANGO", "ruptura": None, "tipo": None,
           "sw_h": None, "sw_l": None, "ob": None,
           "fvg_up": fvg_up, "fvg_dn": fvg_dn,
           "liq_sup": float(df["high"].tail(96).max()),
           "liq_inf": float(df["low"].tail(96).min())}
    altos, bajos = pivotes(df)
    if len(altos) < 2 or len(bajos) < 2:
        return res
    (ia, sh), (ib, sl) = altos[-1], bajos[-1]
    hh, hl = sh > altos[-2][1], sl > bajos[-2][1]
    if hh and hl:
        res["tendencia"] = "ALCISTA"
    elif not hh and not hl:
        res["tendencia"] = "BAJISTA"
    res["sw_h"], res["sw_l"] = sh, sl
    cierre = float(df["close"].iat[-1])
    ku = primera_ruptura(df, ia, sh, True)
    kd = primera_ruptura(df, ib, sl, False)
    cand = []
    if ku is not None and n - 1 - ku <= 5 and cierre > sh:
        cand.append((ku, 1))
    if kd is not None and n - 1 - kd <= 5 and cierre < sl:
        cand.append((kd, -1))
    if cand:
        k, d = max(cand)
        res["ruptura"] = "ALCISTA" if d == 1 else "BAJISTA"
        t = res["tendencia"]
        a_favor = (t == "ALCISTA" and d == 1) or \
                  (t == "BAJISTA" and d == -1) or t == "RANGO"
        res["tipo"] = "BOS" if a_favor else "CHOCH"
        res["ob"] = buscar_ob(df, k, d)
    return res


def sesion(ts):
    h = pd.Timestamp(ts).hour
    if 7 <= h < 12:
        return "LONDRES"
    if 12 <= h < 16:
        return "NY (solape)"
    if 16 <= h < 21:
        return "NY"
    if 0 <= h < 7:
        return "ASIA"
    return "FUERA DE HORARIO"


def mercado_abierto(mercado, ts, live):
    ts = pd.Timestamp(ts)
    if live and (ahora_utc() - ts.to_pydatetime()) > timedelta(minutes=45):
        return False
    if mercado == "ORO":
        wd, h = ts.weekday(), ts.hour
        if wd == 5 or (wd == 4 and h >= 21) or (wd == 6 and h < 22):
            return False
    return True


def noticia_cercana(minutos=30):
    ahora = ahora_utc()
    desde = iso(ahora - timedelta(minutes=minutos))
    hasta = iso(ahora + timedelta(minutes=minutos))
    f = db("SELECT texto FROM noticias WHERE fecha BETWEEN ? AND ? LIMIT 1",
           (desde, hasta), uno=True)
    return f[0] if f else None


# ---------------- MOTOR DE SEÑALES ----------------

def generar_senal(df15, mercado, live=True):
    """Función pura sobre velas de 15m. Se usa igual en vivo y en backtest."""
    df = indicadores(df15)
    if len(df) < 300:
        raise ValueError("Pocas velas para analizar")
    h1 = indicadores(resamplear(df15, "1h"))
    h4 = indicadores(resamplear(df15, "4h"))
    ult = df.iloc[-1]
    ts = ult["time"]
    precio, atr, rsi = float(ult["close"]), float(ult["atr"]), float(ult["rsi"])
    if not np.isfinite(atr) or atr <= 0:
        raise ValueError("ATR inválido")

    est = estructura(df)
    b4, b1 = sesgo(h4), sesgo(h1)
    ses = sesion(ts)

    # ---- puntaje de confluencia (máx. 9) ----
    d = {"ALCISTA": 1, "BAJISTA": -1}.get(est["ruptura"], 0)
    score, razones = 0, []
    if d != 0:
        score += 2
        razones.append(f"{est['tipo']} {est['ruptura'].lower()} en 15m")
        for nombre, b in (("4h", b4), ("1h", b1)):
            if b == d:
                score += 2
                razones.append(f"sesgo {nombre} a favor")
            elif b == -d:
                score -= 2
                razones.append(f"sesgo {nombre} en contra")
        if (d == 1 and 45 <= rsi <= 70) or (d == -1 and 30 <= rsi <= 55):
            score += 1
            razones.append("RSI en zona sana")
        elif (d == 1 and rsi > 75) or (d == -1 and rsi < 25):
            score -= 1
            razones.append("RSI extendido")
        zonas = [est["ob"], est["fvg_up"] if d == 1 else est["fvg_dn"]]
        if any(z and z[0] - atr <= precio <= z[1] + atr for z in zonas):
            score += 1
            razones.append("cerca de OB/FVG a favor")
        if ses in ("LONDRES", "NY (solape)", "NY"):
            score += 1
            razones.append(f"sesión {ses}")

    # ---- filtros de riesgo ----
    nota = None
    if not mercado_abierto(mercado, ts, live):
        nota = "Mercado cerrado o datos desactualizados"
    elif live:
        n = noticia_cercana()
        if n:
            nota = f"Noticia de alto impacto cerca: {n}"
        else:
            hoy = iso(ahora_utc().replace(hour=0, minute=0, second=0))
            stops = db("SELECT COUNT(*) FROM senales WHERE mercado=? AND "
                       "estado='SL' AND fecha_cierre>=?", (mercado, hoy),
                       uno=True)[0]
            if stops >= 2:
                nota = "Límite diario: 2 stops hoy en este mercado"
        if not nota:
            ab = db("SELECT id,direccion,entrada,sl,tp1 FROM senales WHERE "
                    "mercado=? AND estado IN ('ABIERTA','TP1') "
                    "ORDER BY id DESC LIMIT 1", (mercado,), uno=True)
            if ab:
                lado = "COMPRA" if ab[1] == 1 else "VENTA"
                nota = (f"Ya hay una señal abierta #{ab[0]} ({lado}, entrada "
                        f"{fmt(ab[2])}, SL {fmt(ab[3])}, TP1 {fmt(ab[4])})")

    s = {"mercado": mercado, "ts": ts, "precio": precio, "atr": atr,
         "rsi": rsi, "b4": b4, "b1": b1, "est": est, "sesion": ses,
         "score": score, "razones": razones, "direccion": 0,
         "senal": "ESPERAR", "entrada": None, "sl": None, "tp1": None,
         "tp2": None, "nota": nota, "df": df}

    if nota:
        return s
    if d == 0:
        s["nota"] = "Sin ruptura de estructura reciente en 15m"
        return s
    if score < UMBRAL_SCORE:
        s["nota"] = f"Confluencia insuficiente ({score}/9, mínimo {UMBRAL_SCORE})"
        return s

    # ---- niveles: SL estructural acotado por ATR ----
    if d == 1:
        base = (est["sw_l"] if est["sw_l"] is not None else precio - 1.5 * atr)
        sl = base - 0.2 * atr
        dist = precio - sl
    else:
        base = (est["sw_h"] if est["sw_h"] is not None else precio + 1.5 * atr)
        sl = base + 0.2 * atr
        dist = sl - precio
    if dist > 3 * atr:
        s["nota"] = "SL estructural demasiado lejos (> 3 ATR): sin entrada"
        return s
    if dist < 1 * atr:
        dist = 1 * atr
        sl = precio - d * dist
    s.update({"direccion": d, "senal": "COMPRA" if d == 1 else "VENTA",
              "entrada": precio, "sl": sl,
              "tp1": precio + d * RR1 * dist, "tp2": precio + d * RR2 * dist,
              "nota": None})
    return s


# ---------------- SIMULACION DE OPERACIONES ----------------

def simular(direccion, entrada, sl, tp1, tp2, velas, tp1_hit=False):
    """Recorre velas posteriores. Devuelve (estado, r_bruto, tp1_hit, cerrada,
    indice_de_cierre). Si SL y TP caen en la misma vela, gana el SL."""
    H, L = velas["high"].values, velas["low"].values
    if direccion == -1:
        entrada, sl, tp1, tp2 = -entrada, -sl, -tp1, -tp2
        H, L = -L, -H
    for k in range(len(H)):
        if not tp1_hit:
            if L[k] <= sl:
                return "SL", -1.0, False, True, k
            if H[k] >= tp1:
                tp1_hit = True
        else:
            if L[k] <= entrada:
                return "TP1+BE", R_TP1, True, True, k
            if H[k] >= tp2:
                return "TP2", R_TP2, True, True, k
    return ("TP1" if tp1_hit else "ABIERTA"), None, tp1_hit, False, None


# ---------------- SEGUIMIENTO AUTOMATICO DE SEÑALES ----------------

def guardar_senal(s, chat_id):
    return db("INSERT INTO senales (mercado,direccion,entrada,sl,tp1,tp2,"
              "score,razones,fecha,creada,chat_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (s["mercado"], s["direccion"], s["entrada"], s["sl"], s["tp1"],
               s["tp2"], s["score"], "; ".join(s["razones"]), iso(s["ts"]),
               iso(ahora_utc()), str(chat_id)))


def revisar_senales():
    filas = db("SELECT id,mercado,direccion,entrada,sl,tp1,tp2,fecha,creada,"
               "tp1_hit,chat_id FROM senales WHERE estado IN ('ABIERTA','TP1')",
               todos=True)
    if not filas:
        return
    datos = {}
    for (sid, merc, d, ent, sl, tp1, tp2, fecha, creada, hit, chat) in filas:
        try:
            if merc not in datos:
                datos[merc] = obtener_velas(merc, 600, ttl=120)
            df = datos[merc]
            velas = df[df["time"] > pd.Timestamp(fecha, tz="UTC")]
            estado, r, hit2, cerrada, _ = simular(d, ent, sl, tp1, tp2, velas,
                                                  bool(hit))
            riesgo = abs(ent - sl)
            expirada = (ahora_utc() - pd.Timestamp(creada, tz="UTC")
                        .to_pydatetime()) > timedelta(hours=48)
            if not cerrada and expirada:
                ultimo = float(df["close"].iat[-1])
                flot = (ultimo - ent) * d / riesgo
                r = R_TP1 if hit2 else max(-1.0, min(flot, RR1))
                estado, cerrada = "EXPIRADA", True
            if cerrada:
                r_neto = r - COSTO[merc] / riesgo
                db("UPDATE senales SET estado=?, r=?, tp1_hit=?, "
                   "fecha_cierre=? WHERE id=?",
                   (estado, r_neto, int(hit2), iso(ahora_utc()), sid))
                try:
                    enviar_texto(chat, f"Señal #{sid} {merc} cerrada: {estado} "
                                       f"({r_neto:+.2f}R neto de costos)")
                except Exception:
                    logging.exception("No pude avisar el cierre")
            elif hit2 != bool(hit):
                db("UPDATE senales SET estado='TP1', tp1_hit=1 WHERE id=?",
                   (sid,))
                try:
                    enviar_texto(chat, f"Señal #{sid} {merc}: TP1 alcanzado. "
                                       "Cerrá 50% y mové el SL a la entrada.")
                except Exception:
                    logging.exception("No pude avisar TP1")
        except Exception as e:
            logging.error("Error revisando señal %s: %s", sid, limpiar(e))


# ---------------- ESTADISTICAS Y BACKTEST ----------------

def stats_texto(rs, titulo):
    if not rs:
        return f"{titulo}: sin operaciones cerradas."
    rs = np.array(rs, dtype=float)
    gan, per = rs[rs > 0], rs[rs <= 0]
    pf = gan.sum() / abs(per.sum()) if per.sum() < 0 else float("inf")
    curva = np.cumsum(rs)
    dd = (np.maximum.accumulate(curva) - curva).max()
    return (f"{titulo}\n"
            f"Operaciones: {len(rs)}\n"
            f"Ganadoras: {len(gan)} ({len(gan) / len(rs) * 100:.0f}%)\n"
            f"R promedio: {rs.mean():+.2f}\n"
            f"Profit factor: {'inf' if pf == float('inf') else f'{pf:.2f}'}\n"
            f"R total: {rs.sum():+.1f}\n"
            f"Drawdown máx: {dd:.1f}R")


def texto_estadisticas():
    limite = iso(ahora_utc() - timedelta(days=90))
    partes = []
    for m in MERCADOS:
        rs = [x[0] for x in db(
            "SELECT r FROM senales WHERE mercado=? AND r IS NOT NULL AND "
            "fecha>=?", (m, limite), todos=True)]
        partes.append(stats_texto(rs, f"{m} (últimos 90 días)"))
    ab = db("SELECT COUNT(*) FROM senales WHERE estado IN ('ABIERTA','TP1')",
            uno=True)[0]
    partes.append(f"Señales abiertas: {ab}")
    partes.append("Los R están netos de costos. Con pocas operaciones los "
                  "números no son concluyentes.")
    return "\n\n".join(partes)


def backtest(mercado, n=3000, ventana=900, paso=4):
    df = obtener_velas(mercado, n, ttl=0)
    resultados, i = [], ventana
    while i < len(df) - 2:
        sub = df.iloc[max(0, i - ventana):i + 1]
        try:
            s = generar_senal(sub, mercado, live=False)
        except Exception:
            i += paso
            continue
        if s["direccion"] == 0:
            i += paso
            continue
        _, r, _, cerrada, k = simular(s["direccion"], s["entrada"], s["sl"],
                                      s["tp1"], s["tp2"], df.iloc[i + 1:])
        if not cerrada:
            break
        riesgo = abs(s["entrada"] - s["sl"])
        resultados.append(r - COSTO[mercado] / riesgo)
        i += k + 2
    desde, hasta = df["time"].iat[ventana], df["time"].iat[-1]
    cab = (f"BACKTEST {mercado} 15m\nPeríodo: {desde:%d/%m/%Y} a "
           f"{hasta:%d/%m/%Y}\nCostos: {COSTO[mercado]} por operación\n")
    return (cab + stats_texto(resultados, "Resultado") +
            "\n\nOjo: evaluación cada hora, SL gana si toca TP y SL en la "
            "misma vela, sin noticias. Es un período corto: sirve para "
            "descartar ideas malas, no para confirmar que funciona.")


# ---------------- GRAFICO E INFORME ----------------

def tamano(mercado, entrada, sl):
    if BALANCE <= 0:
        return None
    riesgo = BALANCE * RISK_PCT / 100
    u = riesgo / abs(entrada - sl)
    if mercado == "ORO":
        return (f"Riesgo {RISK_PCT:g}% = ${riesgo:,.2f} -> aprox {u:.2f} oz "
                f"({u / 100:.3f} lotes). Verificá el contrato de tu broker.")
    return f"Riesgo {RISK_PCT:g}% = ${riesgo:,.2f} -> aprox {u:.4f} BTC."


def grafico(s, mercado):
    g = s["df"].tail(90).reset_index(drop=True)
    x = g.index
    sube = (g["close"] >= g["open"]).values
    col = np.where(sube, "#26a69a", "#ef5350")
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.vlines(x, g["low"], g["high"], colors=col, linewidth=1)
    alto = (g["close"] - g["open"]).abs().clip(lower=s["atr"] * 0.03)
    ax.bar(x, alto, bottom=g[["open", "close"]].min(axis=1), width=0.6,
           color=col)
    ax.plot(x, g["ema20"], linewidth=1, label="EMA20", color="#1e88e5")
    ax.plot(x, g["ema50"], linewidth=1, label="EMA50", color="#fb8c00")
    est = s["est"]
    for nivel, txt in ((est["sw_h"], "swing high"), (est["sw_l"], "swing low")):
        if nivel:
            ax.axhline(nivel, color="grey", linestyle="--", linewidth=0.8)
            ax.text(len(g) - 1, nivel, " " + txt, fontsize=7, color="grey",
                    va="bottom", ha="right")
    if est["ob"]:
        ax.axhspan(est["ob"][0], est["ob"][1], color="#8e24aa", alpha=0.15,
                   label="Order Block")
    for z, etq in ((est["fvg_up"], "FVG alcista"), (est["fvg_dn"], "FVG bajista")):
        if z:
            ax.axhspan(z[0], z[1], color="#00acc1", alpha=0.10, label=etq)
    if s["direccion"] != 0:
        for nivel, etq, c in ((s["entrada"], "Entrada", "black"),
                              (s["sl"], "SL", "red"), (s["tp1"], "TP1", "green"),
                              (s["tp2"], "TP2", "darkgreen")):
            ax.axhline(nivel, color=c, linestyle=":", linewidth=1.2)
            ax.text(0, nivel, f"{etq} {nivel:,.2f} ", fontsize=8, color=c,
                    va="bottom")
    idx = np.linspace(0, len(g) - 1, 8, dtype=int)
    ax.set_xticks(idx)
    ax.set_xticklabels([hora_local(g["time"].iat[i]) for i in idx],
                       rotation=30, fontsize=8)
    ax.set_title(f"CHALI IA | {MERCADOS[mercado]['nombre']} | 15m | "
                 f"{s['senal']}")
    ax.grid(True, alpha=0.2)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    ruta = os.path.join(CHART_DIR, f"chali_{mercado.lower()}.png")
    fig.savefig(ruta, dpi=110)
    plt.close(fig)
    return ruta


def armar_informe(s, mercado):
    est = s["est"]
    sb = {1: "ALCISTA", -1: "BAJISTA", 0: "NEUTRO"}
    L = [f"CHALI IA - {MERCADOS[mercado]['nombre']} | 15m",
         f"Vela: {hora_local(s['ts'])} (hora local) | Sesión: {s['sesion']}",
         f"Precio: {fmt(s['precio'])}", ""]
    if s["direccion"] != 0:
        dist = abs(s["entrada"] - s["sl"])
        L += [f"SEÑAL: {s['senal']} (puntaje {s['score']}/9)",
              f"Entrada ref: {fmt(s['entrada'])}",
              f"SL: {fmt(s['sl'])} ({dist:,.2f} pts)",
              f"TP1: {fmt(s['tp1'])} ({RR1:g}R, cerrar 50% y SL a entrada)",
              f"TP2: {fmt(s['tp2'])} ({RR2:g}R)",
              "Motivos: " + "; ".join(s["razones"])]
        t = tamano(mercado, s["entrada"], s["sl"])
        if t:
            L.append(t)
    else:
        L += ["SEÑAL: ESPERAR", f"Motivo: {s['nota']}",
              f"Confluencia: {s['score']}/9"]
    L += ["",
          f"Sesgo 4h: {sb[s['b4']]} | 1h: {sb[s['b1']]}",
          f"Estructura 15m: {est['tendencia']} | Ruptura: "
          f"{(est['tipo'] + ' ' + est['ruptura'].lower()) if est['ruptura'] else 'ninguna'}",
          f"RSI {s['rsi']:.1f} | ATR {fmt(s['atr'])}",
          f"OB: {fmt(est['ob'][0]) + ' - ' + fmt(est['ob'][1]) if est['ob'] else 'no detectado'}",
          f"Liquidez 24h: sup {fmt(est['liq_sup'])} / inf {fmt(est['liq_inf'])}",
          "",
          "Análisis técnico automático, no garantiza resultados. Verificá "
          "spread, comisiones y precio de tu broker. Operá solo con riesgo "
          "que puedas perder."]
    return "\n".join(L)


# ---------------- GROQ (IA) ----------------

PREFERIDOS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile",
              "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
EXCLUIR = ("whisper", "guard", "tts", "orpheus", "playai", "distil", "embed")
_modelo = {"id": None, "t": 0.0}


def elegir_modelo(forzar=False):
    if _modelo["id"] and not forzar and time.time() - _modelo["t"] < 3600:
        return _modelo["id"]
    r = session.get("https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    timeout=20)
    if not r.ok:
        raise RuntimeError(f"Groq /models {r.status_code}")
    ids = [m["id"] for m in r.json().get("data", [])
           if m.get("active", True)]
    for c in ([GROQ_MODEL] if GROQ_MODEL else []) + PREFERIDOS:
        if c in ids:
            elegido = c
            break
    else:
        validos = [i for i in ids if not any(e in i.lower() for e in EXCLUIR)]
        if not validos:
            raise RuntimeError("Groq no tiene modelos de chat disponibles")
        elegido = validos[0]
    if elegido != _modelo["id"]:
        logging.info("Modelo Groq elegido: %s", elegido)
    _modelo.update(id=elegido, t=time.time())
    return elegido


def llamar_groq(mensajes, max_tokens=700, temperature=0.3, reintento=True):
    if not GROQ_API_KEY:
        raise RuntimeError("Falta GROQ_API_KEY")
    modelo = elegir_modelo()
    r = session.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": modelo, "messages": mensajes,
              "temperature": temperature, "max_tokens": max_tokens},
        timeout=60)
    if not r.ok:
        cuerpo = limpiar(r.text[:400])
        if reintento and ("model" in cuerpo.lower()) and r.status_code in (400, 404):
            elegir_modelo(forzar=True)
            return llamar_groq(mensajes, max_tokens, temperature, False)
        raise RuntimeError(f"Groq {r.status_code}: {cuerpo}")
    txt = r.json()["choices"][0]["message"].get("content") or ""
    return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()


def comentario_ia(s, mercado):
    est = s["est"]
    ctx = {"mercado": MERCADOS[mercado]["nombre"], "precio": s["precio"],
           "senal": s["senal"], "motivo_espera": s["nota"],
           "score": f"{s['score']}/9", "sesgo_4h": s["b4"], "sesgo_1h": s["b1"],
           "estructura": est["tendencia"], "ruptura": est["ruptura"],
           "tipo": est["tipo"], "rsi": round(s["rsi"], 1),
           "atr": round(s["atr"], 2), "sesion": s["sesion"],
           "entrada": s["entrada"], "sl": s["sl"], "tp1": s["tp1"],
           "tp2": s["tp2"], "ob": est["ob"], "swing_high": est["sw_h"],
           "swing_low": est["sw_l"], "liq_sup_24h": est["liq_sup"],
           "liq_inf_24h": est["liq_inf"]}
    sistema = (
        "Sos CHALI IA, analista de trading de oro y BTC. Recibís datos YA "
        "calculados por el sistema. No inventes ni cambies niveles, precios "
        "ni indicadores. Respondé en español rioplatense, máximo 110 "
        "palabras, sin listas largas: 1) lectura del mercado, 2) escenario "
        "alcista y bajista con su condición, 3) qué invalida la idea, "
        "4) riesgo a vigilar. Si la señal es ESPERAR, explicá qué falta para "
        "que haya setup. Nunca prometas resultados.")
    return llamar_groq(
        [{"role": "system", "content": sistema},
         {"role": "user", "content": json.dumps(ctx, default=str)}],
        max_tokens=500)


def preguntar(chat_id, pregunta):
    recuerdos, hist = obtener_memoria(chat_id)
    ultimos = "\n\n".join(f"[{m}] {t[:900]}" for m, t in ULTIMO_INFORME.items())
    sistema = (
        "Sos CHALI IA, analista profesional de trading (oro y BTC) y "
        "asistente general, en español rioplatense. Razonás paso a paso, "
        "distinguís hechos de hipótesis y marcás riesgos. No garantizás "
        "resultados ni inventás precios actuales: para precios usá solo los "
        "análisis recientes de abajo o pedí que usen /oro o /btc. No "
        "afirmes que te entrenás solo: tu memoria son los recuerdos "
        "guardados y las estadísticas de señales.\n\n"
        "RECUERDOS:\n" + (recuerdos or "ninguno") +
        "\n\nÚLTIMOS ANÁLISIS:\n" + (ultimos or "ninguno"))
    msgs = [{"role": "system", "content": sistema}] + hist[-10:] + \
           [{"role": "user", "content": pregunta}]
    try:
        r = llamar_groq(msgs, max_tokens=900, temperature=0.4)
    except Exception as e:
        logging.error("Groq: %s", limpiar(e))
        return f"No pude consultar la IA ({limpiar(e)[:200]})."
    guardar_mensaje(chat_id, "user", pregunta)
    guardar_mensaje(chat_id, "assistant", r)
    return r


# ---------------- ANALISIS COMPLETO ----------------

def analizar(mercado, chat_id):
    df = obtener_velas(mercado, 1500)
    s = generar_senal(df, mercado, live=True)
    ruta = grafico(s, mercado)
    informe = armar_informe(s, mercado)
    if s["direccion"] != 0:
        sid = guardar_senal(s, chat_id)
        informe += f"\n\nSeñal #{sid} registrada: la sigo y te aviso TP/SL."
    try:
        informe += "\n\nLectura de CHALI:\n" + comentario_ia(s, mercado)
    except Exception as e:
        logging.error("Comentario IA: %s", limpiar(e))
    ULTIMO_INFORME[mercado] = informe
    return ruta, informe, s


# ---------------- COMANDOS ----------------

def detectar_mercados(texto):
    t = texto.lower()
    oro = bool(re.search(r"\b(oro|xau|xauusd|gold)\b", t))
    btc = bool(re.search(r"\b(btc|bitcoin|btcusdt)\b", t))
    ambos = bool(re.search(r"(ambos|los dos|los mercados|todo el mercado)", t))
    pide = bool(re.search(
        r"(gr[aá]fic|chart|vela|analiz|an[aá]lisis|se[nñ]al|entrada|operaci|"
        r"\bsl\b|\btp\b|liquidez|order block|\bbos\b|choch|precio|cotiz)", t))
    if not pide:
        return []
    if ambos or (oro and btc):
        return ["ORO", "BTC"]
    return ["ORO"] if oro else ["BTC"] if btc else []


AYUDA = (
    "CHALI IA\n\n"
    "/oro  /btc  /mercados - análisis con gráfico\n"
    "/abiertas - señales en seguimiento\n"
    "/estadisticas - resultados reales de mis señales\n"
    "/backtest oro|btc - prueba la estrategia con historial\n"
    "/noticia AAAA-MM-DD HH:MM texto - bloquea señales ±30 min "
    "(hora local)\n"
    "/noticias - ver noticias cargadas\n"
    "/recordar texto  /memoria  /olvidar\n"
    "/id - ver tu chat id\n\n"
    "También podés escribir: 'analizá el oro' o preguntarme lo que quieras.")


def procesar_mensaje(chat_id, texto):
    texto = (texto or "").strip()
    if not texto:
        return
    partes = texto.split(maxsplit=1)
    cmd = partes[0].split("@")[0].lower()
    arg = partes[1].strip() if len(partes) > 1 else ""

    if cmd == "/id":
        enviar_texto(chat_id, f"Tu chat id es: {chat_id}")
        return
    if str(chat_id) not in ADMIN_IDS:
        if not ADMIN_IDS:
            enviar_texto(chat_id, f"Bot sin configurar. Tu chat id es "
                                  f"{chat_id}. Cargalo en Render como "
                                  "TELEGRAM_ADMIN_ID y reiniciá.")
        return

    try:
        revisar_senales()
    except Exception:
        logging.exception("revisar_senales")

    if cmd in ("/start", "/ayuda", "/help"):
        enviar_texto(chat_id, AYUDA)
        return
    if cmd == "/recordar":
        if not arg:
            enviar_texto(chat_id, "Usá: /recordar texto")
            return
        db("INSERT INTO recuerdos (chat_id,recuerdo,fecha) VALUES (?,?,?)",
           (str(chat_id), arg[:2000], iso(ahora_utc())))
        enviar_texto(chat_id, "Guardado.")
        return
    if cmd == "/memoria":
        rec, _ = obtener_memoria(chat_id, 1)
        enviar_texto(chat_id, "Recuerdos:\n" + (rec or "ninguno"))
        return
    if cmd == "/olvidar":
        db("DELETE FROM recuerdos WHERE chat_id=?", (str(chat_id),))
        enviar_texto(chat_id, "Recuerdos borrados.")
        return
    if cmd == "/estadisticas":
        enviar_texto(chat_id, texto_estadisticas())
        return
    if cmd == "/abiertas":
        f = db("SELECT id,mercado,direccion,entrada,sl,tp1,tp2,estado FROM "
               "senales WHERE estado IN ('ABIERTA','TP1') ORDER BY id",
               todos=True)
        enviar_texto(chat_id, "\n".join(
            f"#{i} {m} {'COMPRA' if d == 1 else 'VENTA'} [{e}] entrada "
            f"{fmt(en)} SL {fmt(sl)} TP1 {fmt(t1)} TP2 {fmt(t2)}"
            for i, m, d, en, sl, t1, t2, e in f) or "No hay señales abiertas.")
        return
    if cmd == "/noticia":
        m = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s+(.+)", arg)
        if not m:
            enviar_texto(chat_id, "Usá: /noticia 2026-10-14 09:30 CPI USA "
                                  "(hora local)")
            return
        local = pd.Timestamp(f"{m.group(1)} {m.group(2)}")
        utc = local - timedelta(hours=TZ_OFFSET)
        db("INSERT INTO noticias (fecha,texto) VALUES (?,?)",
           (iso(utc), m.group(3)[:200]))
        enviar_texto(chat_id, "Noticia cargada. Bloqueo señales ±30 min.")
        return
    if cmd == "/noticias":
        f = db("SELECT fecha,texto FROM noticias WHERE fecha>=? ORDER BY fecha",
               (iso(ahora_utc() - timedelta(hours=1)),), todos=True)
        enviar_texto(chat_id, "\n".join(
            f"{hora_local(pd.Timestamp(a))}  {b}" for a, b in f)
            or "No hay noticias próximas cargadas.")
        return
    if cmd == "/backtest":
        m = arg.upper()
        if m not in MERCADOS:
            enviar_texto(chat_id, "Usá: /backtest oro  o  /backtest btc")
            return
        enviar_texto(chat_id, f"Corriendo backtest de {m}. Puede tardar "
                              "unos minutos...")
        try:
            enviar_texto(chat_id, backtest(m))
        except Exception as e:
            enviar_texto(chat_id, f"Falló el backtest: {limpiar(e)[:300]}")
        return

    mercados = {"/oro": ["ORO"], "/btc": ["BTC"],
                "/mercados": ["ORO", "BTC"]}.get(cmd) or \
        ([] if cmd.startswith("/") else detectar_mercados(texto))
    if mercados:
        for m in mercados:
            try:
                enviar_texto(chat_id, f"Analizando {m}...")
                ruta, informe, _ = analizar(m, chat_id)
                enviar_grafico(chat_id, ruta, informe)
            except Exception as e:
                logging.error("Análisis %s: %s", m, limpiar(e))
                enviar_texto(chat_id, f"No pude analizar {m}: "
                                      f"{limpiar(e)[:300]}")
        return

    enviar_texto(chat_id, preguntar(chat_id, texto))


def manejar(chat_id, texto):
    try:
        procesar_mensaje(chat_id, texto)
    except Exception as e:
        logging.exception("Error procesando mensaje")
        try:
            enviar_texto(chat_id, "Error interno: " + limpiar(e)[:200])
        except Exception:
            pass


# ---------------- WEBHOOK ----------------

@app.route("/", methods=["GET"])
def inicio():
    return jsonify({"app": "CHALI IA", "estado": "online"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return jsonify({"mensaje": "Webhook activo"}), 200
    if WEBHOOK_SECRET and request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        return jsonify({"error": "no autorizado"}), 403

    upd = request.get_json(silent=True) or {}
    uid = upd.get("update_id")
    if uid is not None:
        if uid in updates_vistos:
            return jsonify({"ok": True, "duplicado": True}), 200
        updates_vistos.append(uid)

    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return jsonify({"ok": True}), 200
    chat_id = (msg.get("chat") or {}).get("id")
    texto = msg.get("text", "")
    if chat_id is None or not texto:
        return jsonify({"ok": True}), 200

    # Responde 200 enseguida; el trabajo pesado va en segundo plano.
    ejecutor.submit(manejar, chat_id, texto)
    return jsonify({"ok": True}), 200


# ---------------- SEGUNDO PLANO ----------------

def bucle_fondo():
    ultimo_scan = 0.0
    while True:
        try:
            revisar_senales()
        except Exception:
            logging.exception("Error en seguimiento")
        if AUTO_SCAN and ADMIN_IDS and time.time() - ultimo_scan >= 900:
            ultimo_scan = time.time()
            for m in MERCADOS:
                try:
                    ruta, informe, s = analizar(m, ADMIN_IDS[0])
                    if s["direccion"] != 0:
                        for a in ADMIN_IDS:
                            enviar_grafico(a, ruta, "SEÑAL AUTOMÁTICA\n\n" +
                                           informe)
                except Exception as e:
                    logging.error("Auto-scan %s: %s", m, limpiar(e))
        time.sleep(300)


_lock_fd = None


def iniciar_fondo():
    """Un solo worker de gunicorn corre el seguimiento (lock de archivo)."""
    global _lock_fd
    if fcntl is not None:
        try:
            _lock_fd = open("/tmp/chali_fondo.lock", "w")
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
    threading.Thread(target=bucle_fondo, daemon=True).start()


def configurar_webhook():
    base = env("RENDER_EXTERNAL_URL").rstrip("/")
    if not TELEGRAM_TOKEN or not base:
        logging.warning("Sin TELEGRAM_TOKEN o RENDER_EXTERNAL_URL: "
                        "configurá el webhook manualmente.")
        return
    datos = {"url": base + "/webhook"}
    if WEBHOOK_SECRET:
        datos["secret_token"] = WEBHOOK_SECRET
    try:
        r = session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/"
                         "setWebhook", data=datos, timeout=20)
        logging.info("setWebhook: %s", limpiar(r.text[:300]))
    except Exception as e:
        logging.error("setWebhook falló: %s", limpiar(e))


def arrancar():
    if not ADMIN_IDS:
        logging.warning("TELEGRAM_ADMIN_ID vacío: el bot solo responde /id")
    threading.Thread(target=configurar_webhook, daemon=True).start()
    iniciar_fondo()


arrancar()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

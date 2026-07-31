"""
XAUUSD Scalping Bot — High Probability Edition
================================================
Strategi: Multi-indikator confluence scoring (EMA + RSI + MACD + ATR)
  - EMA 9 & 21 crossover  → konfirmasi tren
  - EMA 50               → filter tren besar
  - RSI (14)             → konfirmasi momentum, hindari overbought/oversold
  - MACD (12, 26, 9)     → histogram konfirmasi arah momentum
  - ATR (14)             → SL dinamis sesuai volatilitas
  - SINYAL hanya keluar jika skor >= 3 dari 4 kondisi terpenuhi

Deploy : Railway (gunicorn, 1 worker + threads)
Data   : Twelve Data API (https://twelvedata.com)
"""

import os
import time
import threading
import requests
from flask import Flask, render_template_string, request, redirect, url_for
import pytz
from datetime import datetime

app = Flask(__name__)

lock = threading.Lock()

config = {
    "TELEGRAM_BOT_TOKEN":  os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID":    os.environ.get("TELEGRAM_CHAT_ID", ""),
    "TWELVE_DATA_API_KEY": os.environ.get("TWELVE_DATA_API_KEY", ""),
    "TIMEFRAME":    "5min",
    "RISK_REWARD":  "1:2",
    "LOT_SIZE":     0.1,
    "ATR_MULT":     1.5,
    "MIN_SCORE":    3,
    "IS_RUNNING":   False,
}

bot_stats = {
    "total_signals":    0,
    "win_count":        0,
    "loss_count":       0,
    "win_rate":         "0.0%",
    "total_pnl_pips":   0.0,
    "total_pnl":        "+0.0 pips",
    "avg_pnl":          "+0.0 pips / trade",
    "last_check":       "Belum pernah",
    "last_error":       "",
    "last_signal_type": None,
    "current_price":    None,
    "indicators":       {},
    "last_score":       0,
    "last_conditions":  {},
    "pending_signals":  [],
    "signal_history":   [],
}


# ══════════════════════════════════════════════════════
# KALKULASI INDIKATOR
# ══════════════════════════════════════════════════════

def calc_ema(closes, period):
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_g  = sum(gains[-period:])  / period
    avg_l  = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - (100.0 / (1 + avg_g / avg_l))


def calc_macd(closes, fast=12, slow=26, signal=9):
    ema_f = calc_ema(closes, fast)
    ema_s = calc_ema(closes, slow)
    if not ema_f or not ema_s:
        return 0.0, 0.0, 0.0
    offset   = len(ema_f) - len(ema_s)
    macd_arr = [ema_f[i + offset] - ema_s[i] for i in range(len(ema_s))]
    sig_arr  = calc_ema(macd_arr, signal)
    if not sig_arr:
        return 0.0, 0.0, 0.0
    hist = macd_arr[-1] - sig_arr[-1]
    return macd_arr[-1], sig_arr[-1], hist


def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 3.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1]),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period


# ══════════════════════════════════════════════════════
# ANALISIS SINYAL
# ══════════════════════════════════════════════════════

def analyze_signal(candles):
    data   = list(reversed(candles))
    closes = [float(v["close"]) for v in data]
    highs  = [float(v["high"])  for v in data]
    lows   = [float(v["low"])   for v in data]

    ema9  = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    rsi   = calc_rsi(closes, 14)
    macd_line, signal_line, histogram = calc_macd(closes, 12, 26, 9)
    atr   = calc_atr(highs, lows, closes, 14)

    if len(ema9) < 2 or len(ema21) < 2 or not ema50:
        return None

    e9_curr  = ema9[-1]
    e21_curr = ema21[-1]
    e50_curr = ema50[-1]
    price    = closes[-1]

    indic = {
        "price":     round(price,     2),
        "ema9":      round(e9_curr,   2),
        "ema21":     round(e21_curr,  2),
        "ema50":     round(e50_curr,  2),
        "rsi":       round(rsi,       1),
        "macd":      round(macd_line, 4),
        "macd_hist": round(histogram, 4),
        "atr":       round(atr,       2),
    }

    atr_mult = float(config.get("ATR_MULT", 1.5))
    rr_map   = {"1:1": 1, "1:2": 2, "1:3": 3}
    rr       = rr_map.get(config.get("RISK_REWARD", "1:2"), 2)
    sl_dist  = round(atr * atr_mult, 2)
    tp_dist  = round(sl_dist * rr,   2)
    entry    = round(price, 2)

    # ── Kondisi BUY ──
    buy_conds = {}
    buy_conds[f"EMA9 ({indic['ema9']}) > EMA21 ({indic['ema21']})"] = e9_curr > e21_curr
    buy_conds[f"Harga ({entry}) > EMA50 ({indic['ema50']})"]         = price > e50_curr
    buy_conds[f"RSI ({indic['rsi']}) antara 45-65"]                  = 45 <= rsi <= 65
    buy_conds[f"MACD Hist ({indic['macd_hist']}) > 0"]               = histogram > 0
    buy_score = sum(buy_conds.values())

    # ── Kondisi SELL ──
    sell_conds = {}
    sell_conds[f"EMA9 ({indic['ema9']}) < EMA21 ({indic['ema21']})"] = e9_curr < e21_curr
    sell_conds[f"Harga ({entry}) < EMA50 ({indic['ema50']})"]         = price < e50_curr
    sell_conds[f"RSI ({indic['rsi']}) antara 35-55"]                  = 35 <= rsi <= 55
    sell_conds[f"MACD Hist ({indic['macd_hist']}) < 0"]               = histogram < 0
    sell_score = sum(sell_conds.values())

    min_score = int(config.get("MIN_SCORE", 3))

    if buy_score >= min_score and buy_score >= sell_score:
        return {
            "type": "BUY", "score": buy_score, "max_score": 4,
            "conditions": buy_conds, "entry": entry,
            "sl": round(entry - sl_dist, 2), "tp": round(entry + tp_dist, 2),
            "sl_dist": sl_dist, "tp_dist": tp_dist, "indicators": indic,
        }

    if sell_score >= min_score and sell_score > buy_score:
        return {
            "type": "SELL", "score": sell_score, "max_score": 4,
            "conditions": sell_conds, "entry": entry,
            "sl": round(entry + sl_dist, 2), "tp": round(entry - tp_dist, 2),
            "sl_dist": sl_dist, "tp_dist": tp_dist, "indicators": indic,
        }

    return None


# ══════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════

def send_telegram(message):
    token   = config.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = config.get("TELEGRAM_CHAT_ID",   "").strip()
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


def _recalc_stats():
    total_closed = bot_stats["win_count"] + bot_stats["loss_count"]
    if total_closed > 0:
        wr  = bot_stats["win_count"] / total_closed * 100
        avg = bot_stats["total_pnl_pips"] / total_closed
        bot_stats["win_rate"] = f"{wr:.1f}%"
        sign = "+" if avg >= 0 else ""
        bot_stats["avg_pnl"] = f"{sign}{avg:.1f} pips / trade"
    pnl  = bot_stats["total_pnl_pips"]
    sign = "+" if pnl >= 0 else ""
    bot_stats["total_pnl"] = f"{sign}{pnl:.1f} pips"


def _resolve_pending(current_price):
    still_open = []
    messages   = []

    for sig in bot_stats["pending_signals"]:
        hit_tp = hit_sl = False
        if sig["type"] == "BUY":
            hit_tp = current_price >= sig["tp"]
            hit_sl = current_price <= sig["sl"]
        else:
            hit_tp = current_price <= sig["tp"]
            hit_sl = current_price >= sig["sl"]

        if hit_tp or hit_sl:
            pnl_pips = sig.get("tp_dist", sig["sl_dist"]) if hit_tp else -sig["sl_dist"]
            sig["status"]      = "WIN" if hit_tp else "LOSS"
            sig["pnl_pips"]    = round(pnl_pips, 2)
            sig["close_price"] = round(current_price, 2)

            bot_stats["total_pnl_pips"] += pnl_pips
            if hit_tp:
                bot_stats["win_count"]  += 1
            else:
                bot_stats["loss_count"] += 1

            bot_stats["signal_history"].insert(0, sig)
            if len(bot_stats["signal_history"]) > 50:
                bot_stats["signal_history"] = bot_stats["signal_history"][:50]

            emoji  = "✅" if hit_tp else "❌"
            result = "PROFIT" if hit_tp else "LOSS"
            sign   = "+" if pnl_pips >= 0 else ""
            messages.append(
                f"{emoji} *SIGNAL CLOSED — {result}*\n\n"
                f"Pair: XAUUSD | Tipe: *{sig['type']}*\n"
                f"Entry: `{sig['entry']}` → Close: `{sig['close_price']}`\n"
                f"PnL: `{sign}{pnl_pips:.1f} pips`\n"
                f"Lot: `{config.get('LOT_SIZE', 0.1)}` | RR: `{config.get('RISK_REWARD', '1:2')}`\n"
                f"Skor sinyal: `{sig.get('score', '?')}/4`"
            )
        else:
            still_open.append(sig)

    bot_stats["pending_signals"] = still_open
    _recalc_stats()
    return messages


# ══════════════════════════════════════════════════════
# BACKGROUND WORKER THREAD
# ══════════════════════════════════════════════════════

def trading_bot_worker():
    while True:
        outbox = []

        if config.get("IS_RUNNING"):
            api_key = config.get("TWELVE_DATA_API_KEY", "").strip()
            tf      = config.get("TIMEFRAME", "5min")

            if not api_key:
                with lock:
                    bot_stats["last_error"] = "⚠️ API Key Twelve Data belum diisi."
            else:
                try:
                    url = (
                        "https://api.twelvedata.com/time_series"
                        f"?symbol=XAU/USD&interval={tf}"
                        f"&outputsize=100&apikey={api_key}"
                    )
                    res = requests.get(url, timeout=15).json()

                    if "values" in res and len(res["values"]) >= 60:
                        candles   = res["values"]
                        close_now = float(candles[0]["close"])
                        now_wib   = datetime.now(
                            pytz.timezone("Asia/Jakarta")
                        ).strftime("%Y-%m-%d %H:%M:%S")

                        signal = analyze_signal(candles)

                        with lock:
                            bot_stats["last_check"]    = now_wib
                            bot_stats["last_error"]    = ""
                            bot_stats["current_price"] = round(close_now, 2)

                            if signal:
                                bot_stats["indicators"]      = signal["indicators"]
                                bot_stats["last_score"]      = signal["score"]
                                bot_stats["last_conditions"] = signal["conditions"]

                            outbox.extend(_resolve_pending(close_now))

                            if signal:
                                sig_type = signal["type"]
                                if sig_type != bot_stats["last_signal_type"]:
                                    bot_stats["last_signal_type"] = sig_type

                                    new_sig = {
                                        "time":        now_wib,
                                        "tf":          tf,
                                        "type":        sig_type,
                                        "entry":       signal["entry"],
                                        "sl":          signal["sl"],
                                        "tp":          signal["tp"],
                                        "sl_dist":     signal["sl_dist"],
                                        "tp_dist":     signal["tp_dist"],
                                        "score":       signal["score"],
                                        "conditions":  signal["conditions"],
                                        "indicators":  signal["indicators"],
                                        "status":      "OPEN",
                                        "pnl_pips":    None,
                                        "close_price": None,
                                    }
                                    bot_stats["pending_signals"].append(new_sig)
                                    if len(bot_stats["pending_signals"]) > 50:
                                        bot_stats["pending_signals"] = bot_stats["pending_signals"][-50:]
                                    bot_stats["total_signals"] += 1

                                    stars      = "⭐" * signal["score"]
                                    emoji      = "🟢" if sig_type == "BUY" else "🔴"
                                    indic      = signal["indicators"]
                                    conds_text = "\n".join(
                                        f"  {'✅' if v else '❌'} {k}"
                                        for k, v in signal["conditions"].items()
                                    )
                                    outbox.append(
                                        f"🚨 *XAUUSD SIGNAL — {sig_type}* {stars}\n\n"
                                        f"{emoji} Tipe: *{sig_type}*\n"
                                        f"📊 Skor: *{signal['score']}/4*\n\n"
                                        f"📍 Entry: `{signal['entry']}`\n"
                                        f"🛡 Stop Loss: `{signal['sl']}` ({signal['sl_dist']} pips)\n"
                                        f"🎯 Take Profit: `{signal['tp']}` ({signal['tp_dist']} pips)\n"
                                        f"📦 Lot: `{config.get('LOT_SIZE', 0.1)}` | "
                                        f"RR: `{config.get('RISK_REWARD', '1:2')}`\n\n"
                                        f"📈 Indikator:\n"
                                        f"  EMA9: `{indic['ema9']}` | EMA21: `{indic['ema21']}` | EMA50: `{indic['ema50']}`\n"
                                        f"  RSI: `{indic['rsi']}` | MACD Hist: `{indic['macd_hist']}` | ATR: `{indic['atr']}`\n\n"
                                        f"✅ Kondisi:\n{conds_text}\n\n"
                                        f"⏱ {now_wib} WIB | TF: {tf}"
                                    )

                    elif "message" in res:
                        with lock:
                            bot_stats["last_error"] = f"⚠️ API Error: {res.get('message', 'Unknown')}"
                    elif "values" in res and len(res["values"]) < 60:
                        with lock:
                            bot_stats["last_error"] = (
                                f"⚠️ Data tidak cukup ({len(res['values'])} candle, butuh >= 60)."
                            )
                    else:
                        with lock:
                            bot_stats["last_error"] = "⚠️ Respons API tidak valid."

                except requests.exceptions.Timeout:
                    with lock:
                        bot_stats["last_error"] = "⚠️ Timeout — tidak dapat menghubungi Twelve Data."
                except requests.exceptions.ConnectionError:
                    with lock:
                        bot_stats["last_error"] = "⚠️ Koneksi gagal — periksa jaringan Railway."
                except Exception as e:
                    with lock:
                        bot_stats["last_error"] = f"⚠️ Error: {e}"

        for msg in outbox:
            send_telegram(msg)

        intervals = {"1min": 60, "5min": 300, "15min": 900}
        time.sleep(intervals.get(config.get("TIMEFRAME", "5min"), 300))


_worker = threading.Thread(target=trading_bot_worker, daemon=True, name="BotWorker")
_worker.start()


# ══════════════════════════════════════════════════════
# HTML DASHBOARD
# ══════════════════════════════════════════════════════

HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>XAUUSD Bot</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh;padding:16px}
.wrap{max-width:1020px;margin:auto}
.hdr{text-align:center;padding:22px 0 18px;border-bottom:1px solid #21262d;margin-bottom:20px}
.hdr h1{font-size:24px;color:#f0a500;letter-spacing:1px}
.hdr small{font-size:11px;color:#484f58;margin-top:4px;display:block}
.err{background:#3d1414;border:1px solid #da3633;border-radius:6px;padding:10px 14px;margin-bottom:16px;font-size:13px;color:#f85149}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px;margin-bottom:18px}
.card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:14px;text-align:center}
.lbl{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.val{font-size:22px;font-weight:700;color:#f0f6fc}
.g{color:#3fb950}.r{color:#f85149}.y{color:#f0a500}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700}
.b-run{background:#1a4731;color:#3fb950;border:1px solid #238636}
.b-stp{background:#3d1414;color:#f85149;border:1px solid #da3633}
.b-opn{background:#1c2d41;color:#58a6ff;border:1px solid #1f6feb}
.b-win{background:#1a4731;color:#3fb950;border:1px solid #238636}
.b-los{background:#3d1414;color:#f85149;border:1px solid #da3633}
.score-bar{display:flex;align-items:center;gap:8px;margin-bottom:18px;background:#161b22;border:1px solid #21262d;border-radius:8px;padding:14px 16px;flex-wrap:wrap}
.score-bar .slbl{font-size:11px;color:#8b949e;margin-right:4px}
.dot{width:20px;height:20px;border-radius:50%;border:2px solid #30363d;display:inline-block}
.dot.on{background:#3fb950;border-color:#238636}
.dot.off{background:#21262d;border-color:#30363d}
.conds{margin-top:10px;width:100%}
.cond-row{display:flex;align-items:center;gap:6px;font-size:12px;margin-bottom:4px;color:#c9d1d9}
.indbar{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:12px 16px;margin-bottom:18px}
.indbar .title{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px}
.inds{display:flex;flex-wrap:wrap;gap:12px}
.ind{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:8px 12px;min-width:100px}
.ind .ik{font-size:10px;color:#8b949e;margin-bottom:3px}
.ind .iv{font-size:15px;font-weight:700;color:#f0f6fc}
.pbar{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:12px 16px;margin-bottom:18px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.plbl{font-size:11px;color:#8b949e}
.pval{font-size:24px;font-weight:700;color:#f0a500}
.pmeta{font-size:11px;color:#6e7681;text-align:right}
.fcard{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:20px;margin-bottom:20px}
.fcard h3{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:14px}
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.field{display:flex;flex-direction:column;gap:5px}
.field label{font-size:11px;color:#8b949e}
.field input,.field select{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px 10px;color:#c9d1d9;font-size:13px;width:100%}
.field input:focus,.field select:focus{outline:none;border-color:#f0a500}
.bgrp{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap}
.btn{flex:1;min-width:110px;padding:9px 16px;border:none;border-radius:6px;font-weight:700;font-size:13px;cursor:pointer;transition:opacity .15s}
.btn:hover{opacity:.85}
.btn-s{background:#1f6feb;color:#fff}
.btn-go{background:#238636;color:#fff}
.btn-stop{background:#da3633;color:#fff}
.btn-rst{background:#333;color:#aaa}
.stitle{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px}
.tbl-wrap{overflow-x:auto;margin-bottom:20px}
table{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #21262d;border-radius:8px;overflow:hidden;font-size:13px;min-width:520px}
th{background:#1c2128;color:#8b949e;padding:10px 12px;text-align:left;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.4px}
td{padding:9px 12px;border-top:1px solid #21262d}
tr:hover td{background:#1c2128}
.buy{color:#3fb950;font-weight:700}.sell{color:#f85149;font-weight:700}
.pp{color:#3fb950}.np{color:#f85149}
.empty td{text-align:center;color:#484f58;padding:20px}
.foot{text-align:center;font-size:10px;color:#484f58;padding:10px 0 20px}
</style>
</head>
<body>
<div class="wrap">

<div class="hdr">
  <h1>⚡ XAUUSD Scalping Bot</h1>
  <small>Multi-indikator confluence · EMA + RSI + MACD + ATR · Auto-refresh 30 detik</small>
</div>

{% if stats.last_error %}
<div class="err">{{ stats.last_error }}</div>
{% endif %}

<div class="cards">
  <div class="card">
    <div class="lbl">Status Bot</div>
    <div class="val" style="font-size:14px;margin-top:4px">
      <span class="badge {% if running %}b-run{% else %}b-stp{% endif %}">
        {% if running %}● RUNNING{% else %}● STOPPED{% endif %}
      </span>
    </div>
  </div>
  <div class="card">
    <div class="lbl">Total Sinyal</div>
    <div class="val y">{{ stats.total_signals }}</div>
  </div>
  <div class="card">
    <div class="lbl">Win / Loss</div>
    <div class="val" style="font-size:18px">
      <span class="g">{{ stats.win_count }}</span>
      <span style="color:#484f58"> / </span>
      <span class="r">{{ stats.loss_count }}</span>
    </div>
  </div>
  <div class="card">
    <div class="lbl">Win Rate</div>
    <div class="val {% if stats.win_count > stats.loss_count %}g{% elif stats.loss_count > stats.win_count %}r{% endif %}">
      {{ stats.win_rate }}
    </div>
  </div>
  <div class="card">
    <div class="lbl">Total PnL</div>
    <div class="val {% if '-' in stats.total_pnl %}r{% elif stats.total_pnl != '+0.0 pips' %}g{% endif %}">
      {{ stats.total_pnl }}
    </div>
  </div>
  <div class="card">
    <div class="lbl">Avg PnL / Trade</div>
    <div class="val" style="font-size:14px;margin-top:4px">{{ stats.avg_pnl }}</div>
  </div>
</div>

<div class="pbar">
  <div>
    <div class="plbl">Harga Terakhir XAUUSD</div>
    <div class="pval">{% if stats.current_price %}{{ stats.current_price }}{% else %}—{% endif %}</div>
  </div>
  <div class="pmeta">
    Pengecekan: {{ stats.last_check }}<br>
    Open: {{ stats.pending_signals | length }} · Closed: {{ stats.signal_history | length }}
  </div>
</div>

{% if stats.indicators %}
<div class="indbar">
  <div class="title">📈 Nilai Indikator Terakhir</div>
  <div class="inds">
    {% set ind = stats.indicators %}
    <div class="ind"><div class="ik">EMA 9</div><div class="iv">{{ ind.ema9 }}</div></div>
    <div class="ind"><div class="ik">EMA 21</div><div class="iv">{{ ind.ema21 }}</div></div>
    <div class="ind"><div class="ik">EMA 50</div><div class="iv">{{ ind.ema50 }}</div></div>
    <div class="ind"><div class="ik">RSI (14)</div>
      <div class="iv {% if ind.rsi > 65 %}r{% elif ind.rsi < 35 %}g{% else %}y{% endif %}">{{ ind.rsi }}</div>
    </div>
    <div class="ind"><div class="ik">MACD Hist</div>
      <div class="iv {% if ind.macd_hist > 0 %}g{% elif ind.macd_hist < 0 %}r{% endif %}">{{ ind.macd_hist }}</div>
    </div>
    <div class="ind"><div class="ik">ATR (14)</div><div class="iv">{{ ind.atr }}</div></div>
  </div>
</div>
{% endif %}

{% if stats.last_score %}
<div class="score-bar">
  <div>
    <span class="slbl">Skor Kondisi Terakhir:</span>
    <strong style="color:#f0a500">{{ stats.last_score }} / 4</strong> &nbsp;
    {% for i in range(4) %}
      <span class="dot {% if i < stats.last_score %}on{% else %}off{% endif %}"></span>
    {% endfor %}
  </div>
  {% if stats.last_conditions %}
  <div class="conds">
    {% for cond, met in stats.last_conditions.items() %}
    <div class="cond-row">
      <span>{% if met %}✅{% else %}❌{% endif %}</span>
      <span>{{ cond }}</span>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>
{% endif %}

<div class="fcard">
  <h3>⚙️ Pengaturan API & Bot</h3>
  <form method="POST" action="/update">
    <div class="fgrid">
      <div class="field">
        <label>Telegram Bot Token</label>
        <input type="password" name="telegram_token" value="{{ cfg.TELEGRAM_BOT_TOKEN }}" placeholder="Dari @BotFather">
      </div>
      <div class="field">
        <label>Telegram Chat ID</label>
        <input type="text" name="telegram_chat_id" value="{{ cfg.TELEGRAM_CHAT_ID }}" placeholder="-100xxxxxxxxxx">
      </div>
      <div class="field">
        <label>Twelve Data API Key</label>
        <input type="password" name="twelve_data_key" value="{{ cfg.TWELVE_DATA_API_KEY }}" placeholder="Dari twelvedata.com">
      </div>
      <div class="field">
        <label>Timeframe</label>
        <select name="timeframe">
          <option value="1min"  {% if cfg.TIMEFRAME=='1min'  %}selected{% endif %}>1 Menit</option>
          <option value="5min"  {% if cfg.TIMEFRAME=='5min'  %}selected{% endif %}>5 Menit</option>
          <option value="15min" {% if cfg.TIMEFRAME=='15min' %}selected{% endif %}>15 Menit</option>
        </select>
      </div>
      <div class="field">
        <label>Lot Size</label>
        <input type="text" name="lot_size" value="{{ cfg.LOT_SIZE }}" placeholder="0.01 – 100">
      </div>
      <div class="field">
        <label>ATR Multiplier (SL = ATR x mult)</label>
        <input type="text" name="atr_mult" value="{{ cfg.ATR_MULT }}" placeholder="1.0 – 3.0">
      </div>
      <div class="field">
        <label>Risk : Reward</label>
        <select name="risk_reward">
          <option value="1:1" {% if cfg.RISK_REWARD=='1:1' %}selected{% endif %}>1 : 1</option>
          <option value="1:2" {% if cfg.RISK_REWARD=='1:2' %}selected{% endif %}>1 : 2</option>
          <option value="1:3" {% if cfg.RISK_REWARD=='1:3' %}selected{% endif %}>1 : 3</option>
        </select>
      </div>
      <div class="field">
        <label>Minimum Skor Sinyal (1-4)</label>
        <select name="min_score">
          <option value="2" {% if cfg.MIN_SCORE==2 %}selected{% endif %}>2 dari 4 kondisi</option>
          <option value="3" {% if cfg.MIN_SCORE==3 %}selected{% endif %}>3 dari 4 kondisi (disarankan)</option>
          <option value="4" {% if cfg.MIN_SCORE==4 %}selected{% endif %}>4 dari 4 kondisi (ketat)</option>
        </select>
      </div>
    </div>
    <div class="bgrp">
      <button class="btn btn-s"    type="submit" name="action" value="save">💾 Simpan</button>
      {% if running %}
      <button class="btn btn-stop" type="submit" name="action" value="stop">⏹ Stop Bot</button>
      {% else %}
      <button class="btn btn-go"   type="submit" name="action" value="start">▶ Start Bot</button>
      {% endif %}
      <button class="btn btn-rst"  type="submit" name="action" value="reset"
              onclick="return confirm('Reset semua statistik dan riwayat sinyal?')">
        🔄 Reset Stats
      </button>
    </div>
  </form>
</div>

<div class="stitle">🔵 Sinyal Aktif — Menunggu TP / SL ({{ stats.pending_signals | length }})</div>
<div class="tbl-wrap">
  <table>
    <thead>
      <tr><th>Waktu</th><th>TF</th><th>Tipe</th><th>Skor</th><th>Entry</th><th>SL</th><th>TP</th><th>Status</th></tr>
    </thead>
    <tbody>
    {% for s in stats.pending_signals | reverse %}
    <tr>
      <td>{{ s.time }}</td>
      <td>{{ s.tf }}</td>
      <td class="{% if s.type=='BUY' %}buy{% else %}sell{% endif %}">{{ s.type }}</td>
      <td><strong style="color:#f0a500">{{ s.score }}/4</strong></td>
      <td>{{ s.entry }}</td><td>{{ s.sl }}</td><td>{{ s.tp }}</td>
      <td><span class="badge b-opn">{{ s.status }}</span></td>
    </tr>
    {% else %}
    <tr class="empty"><td colspan="8">Belum ada sinyal aktif. Klik ▶ Start Bot untuk mulai.</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>

<div class="stitle">📋 Riwayat Sinyal Closed ({{ stats.signal_history | length }})</div>
<div class="tbl-wrap">
  <table>
    <thead>
      <tr><th>Waktu</th><th>TF</th><th>Tipe</th><th>Skor</th><th>Entry</th><th>Close</th><th>SL</th><th>TP</th><th>PnL (pips)</th><th>Hasil</th></tr>
    </thead>
    <tbody>
    {% for s in stats.signal_history %}
    <tr>
      <td>{{ s.time }}</td>
      <td>{{ s.tf }}</td>
      <td class="{% if s.type=='BUY' %}buy{% else %}sell{% endif %}">{{ s.type }}</td>
      <td><strong style="color:#f0a500">{{ s.get('score','?') }}/4</strong></td>
      <td>{{ s.entry }}</td>
      <td>{{ s.close_price if s.close_price else '—' }}</td>
      <td>{{ s.sl }}</td><td>{{ s.tp }}</td>
      <td class="{% if s.pnl_pips and s.pnl_pips >= 0 %}pp{% else %}np{% endif %}">
        {% if s.pnl_pips is not none %}{{ '+' if s.pnl_pips >= 0 else '' }}{{ s.pnl_pips }}{% else %}—{% endif %}
      </td>
      <td>
        {% if 'WIN' in s.status %}<span class="badge b-win">WIN ✅</span>
        {% elif 'LOSS' in s.status %}<span class="badge b-los">LOSS ❌</span>
        {% else %}<span class="badge b-opn">{{ s.status }}</span>{% endif %}
      </td>
    </tr>
    {% else %}
    <tr class="empty"><td colspan="10">Belum ada riwayat sinyal closed.</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>

<div class="foot">XAUUSD Scalping Bot · EMA + RSI + MACD + ATR · Data: Twelve Data API</div>
</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════

@app.route("/")
def index():
    with lock:
        snap = {
            "total_signals":   bot_stats["total_signals"],
            "win_count":       bot_stats["win_count"],
            "loss_count":      bot_stats["loss_count"],
            "win_rate":        bot_stats["win_rate"],
            "total_pnl":       bot_stats["total_pnl"],
            "avg_pnl":         bot_stats["avg_pnl"],
            "last_check":      bot_stats["last_check"],
            "last_error":      bot_stats["last_error"],
            "current_price":   bot_stats["current_price"],
            "indicators":      dict(bot_stats["indicators"]),
            "last_score":      bot_stats["last_score"],
            "last_conditions": dict(bot_stats["last_conditions"]),
            "pending_signals": list(bot_stats["pending_signals"]),
            "signal_history":  list(bot_stats["signal_history"]),
        }
    return render_template_string(HTML, cfg=config, running=config["IS_RUNNING"], stats=snap)


@app.route("/update", methods=["POST"])
def update():
    config["TELEGRAM_BOT_TOKEN"]  = request.form.get("telegram_token",  "").strip()
    config["TELEGRAM_CHAT_ID"]    = request.form.get("telegram_chat_id", "").strip()
    config["TWELVE_DATA_API_KEY"] = request.form.get("twelve_data_key",  "").strip()
    config["TIMEFRAME"]           = request.form.get("timeframe",   "5min")
    config["RISK_REWARD"]         = request.form.get("risk_reward", "1:2")

    try:
        config["LOT_SIZE"] = max(0.01, min(float(request.form.get("lot_size",  "0.1")), 100.0))
    except ValueError:
        pass
    try:
        config["ATR_MULT"] = max(0.5, min(float(request.form.get("atr_mult", "1.5")), 5.0))
    except ValueError:
        pass
    try:
        config["MIN_SCORE"] = max(1, min(int(request.form.get("min_score", "3")), 4))
    except ValueError:
        pass

    action = request.form.get("action")
    if action == "start":
        config["IS_RUNNING"] = True
    elif action == "stop":
        config["IS_RUNNING"] = False
    elif action == "reset":
        with lock:
            bot_stats.update({
                "total_signals": 0, "win_count": 0, "loss_count": 0,
                "win_rate": "0.0%", "total_pnl_pips": 0.0,
                "total_pnl": "+0.0 pips", "avg_pnl": "+0.0 pips / trade",
                "last_signal_type": None, "last_score": 0,
                "last_conditions": {}, "indicators": {},
                "pending_signals": [], "signal_history": [],
            })

    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)

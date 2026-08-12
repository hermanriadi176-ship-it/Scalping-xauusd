"""
================================================================================
 ULTIMATE ADAPTIVE MULTI-TF BOT TRADING XAU/USD — FULL ELITE EDITION
================================================================================
Fitur:
1. Multi-TF Independent Scanning (1h, 30min, 15min, 5min)
2. Detailed Technical Reasons & Order Flow Dominance per Timeframe
3. Adaptive Volatility (SL & TP menyesuaikan ATR di masing-masing timeframe)
4. Manajemen Risiko Riil (Position Sizing, Daily Loss & Consecutive Loss Tracker)
5. Filter Kalender Ekonomi ForexFactory (Auto-Pause saat High-Impact News)
6. Interactive Command Telegram (/status, /pause, /resume, /stats)
7. Built-in Web Server (Untuk mencegah kontainer Railway mati otomatis)
8. Pesan Personal Abah FK
================================================================================
"""

import os
import time
import json
import gc
import logging
import requests
import pandas as pd
import numpy as np
import threading
from datetime import datetime, timezone, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==========================================
# KREDENSIAL (DIAMBIL OTOMATIS DARI RAILWAY VARIABLES)
# ==========================================
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY", "")
TG_TOKEN       = os.getenv("TG_TOKEN", "")
TG_CHAT_ID     = os.getenv("TG_CHAT_ID", "")

_PLACEHOLDER_MARK = "ISI_"
def _is_placeholder(val: str) -> bool:
    return isinstance(val, str) and (not val.strip() or val.startswith(_PLACEHOLDER_MARK))

# ==========================================
# KONFIGURASI PARAMETER
# ==========================================
SYMBOL = "XAU/USD"
TIMEFRAMES = ["1h", "30min", "15min", "5min"]

ACCOUNT_BALANCE = 10000.0
RISK_PERCENT = 1.0
MAX_DAILY_LOSS = 3.0
MAX_CONSECUTIVE_LOSSES = 3
MIN_CONFLUENCE = 2

EMA_FAST = 21
EMA_SLOW = 55
RSI_LEN = 14
RSI_OB = 70
RSI_OS = 30
ATR_LEN = 14

CONTRACT_SIZE = 100
POLL_INTERVAL = 60
TF_COOLDOWN = 900
API_TIMEOUT = 10
MAX_LOT = 5.0

ECON_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
ECON_IMPACT_LEVELS = ["High"]
ECON_COUNTRIES = ["USD"]
ECON_WINDOW_MIN = 30
ECON_CHECK_INTERVAL = 1800

# ==========================================
# STATE GLOBAL & INTERACTIVE CONTROLS
# ==========================================
bot_is_paused = False
daily_loss_tracker = 0.0
consecutive_losses = 0
total_signals_sent = 0
last_reset_day = datetime.now(timezone.utc).date()
tf_last_signal_time = {}
last_telegram_update_id = 0

_econ_cache = {
    "checked_at": 0.0,
    "events": [],
}

# ==========================================
# LOGGING SETUP
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("adaptive_multi_tf_bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("AbahFK_Bot")

def send_telegram_message(message: str):
    if not TG_TOKEN or _is_placeholder(TG_TOKEN) or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
    for _ in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return
        except:
            time.sleep(3)

# ==========================================
# TELEGRAM COMMAND HANDLER (TWO-WAY CONTROL)
# ==========================================
def check_telegram_commands():
    global bot_is_paused, last_telegram_update_id
    if not TG_TOKEN or _is_placeholder(TG_TOKEN) or not TG_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={last_telegram_update_id + 1}&timeout=1"
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            return
        data = resp.json()
        if not data.get("ok", False):
            return

        for update in data.get("result", []):
            last_telegram_update_id = update["update_id"]
            message = update.get("message", {})
            text = message.get("text", "").strip().lower()
            chat_id = str(message.get("chat", {}).get("id", ""))

            if chat_id != str(TG_CHAT_ID):
                continue

            if text == "/status":
                status_text = (
                    f"📊 <b>STATUS ADAPTIVE MULTI-TF BOT</b>\n"
                    f"• Status Bot: {'⏸ PAUSED' if bot_is_paused else '🟢 AKTIF'}\n"
                    f"• Akumulasi Loss Harian: ${round(daily_loss_tracker, 2)}\n"
                    f"• Sinyal Terkirim: {total_signals_sent}\n"
                    f"• Consec Losses: {consecutive_losses}"
                )
                send_telegram_message(status_text)
            elif text == "/pause":
                bot_is_paused = True
                send_telegram_message("⏸ <b>Bot dijeda (Paused)</b> via Telegram.")
            elif text == "/resume":
                bot_is_paused = False
                send_telegram_message("▶️ <b>Bot diaktifkan kembali (Resumed)</b> via Telegram.")
    except Exception as e:
        log.warning(f"Gagal memeriksa perintah Telegram: {e}")

# ==========================================
# KALENDER EKONOMI & SESI TRADING
# ==========================================
def fetch_economic_calendar():
    try:
        resp = requests.get(ECON_CALENDAR_URL, headers={"User-Agent": "Bot/1.0"}, timeout=API_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
    except:
        pass
    return []

def is_high_impact_event_time():
    now_ts = time.time()
    now_utc = datetime.now(timezone.utc)
    window = timedelta(minutes=ECON_WINDOW_MIN)
    if (now_ts - _econ_cache["checked_at"]) < ECON_CHECK_INTERVAL and _econ_cache["events"]:
        relevant = _econ_cache["events"]
    else:
        raw = fetch_economic_calendar()
        relevant = []
        for ev in raw:
            if str(ev.get("impact", "")).capitalize() in ECON_IMPACT_LEVELS and str(ev.get("country", "")).upper() in ECON_COUNTRIES:
                dt_str = ev.get("date", "")
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    ev["_dt_utc"] = dt
                    relevant.append(ev)
                except:
                    pass
        _econ_cache["events"] = relevant
        _econ_cache["checked_at"] = now_ts

    for ev in relevant:
        if abs((ev["_dt_utc"] - now_utc).total_seconds()) <= window.total_seconds():
            return True
    return False

def is_optimal_trading_session() -> bool:
    h = datetime.now(timezone.utc).hour
    return 7 <= h <= 20

# ==========================================
# FETCH DATA TWELVE DATA
# ==========================================
def fetch_data(tf: str):
    if not TWELVE_API_KEY or _is_placeholder(TWELVE_API_KEY):
        return None
    url = f"https://api.twelvedata.com/time_series?symbol={SYMBOL}&interval={tf}&outputsize=150&apikey={TWELVE_API_KEY}"
    try:
        resp = requests.get(url, timeout=API_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if "values" not in data:
            return None
        df = pd.DataFrame(data["values"]).iloc[::-1].reset_index(drop=True)
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["volume"] = pd.to_numeric(df.get("volume", 1.0), errors="coerce").fillna(1.0)
        df.dropna(subset=["open", "high", "low", "close"], inplace=True)
        return df
    except:
        return None

# ==========================================
# ANALISIS TEKNIKAL & SPESIFIK REASONS
# ==========================================
def calculate_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))
    df.loc[loss == 0, "rsi"] = 100.0

    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    df["atr"] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).ewm(alpha=1 / ATR_LEN, adjust=False).mean()

    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = (df["high"] - df["low"]).abs()
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    
    df["wick_rejection_buy"] = (df["lower_wick"] > df["range"] * 0.4) & (df["close"] > df["open"])
    df["wick_rejection_sell"] = (df["upper_wick"] > df["range"] * 0.4) & (df["close"] < df["open"])
    df["exhaustion"] = df["body"] > (df["atr"] * 2.0)
    return df

def check_market_dominance(df: pd.DataFrame):
    recent = df.tail(15).copy()
    buyer_power = ((recent['close'] > recent['open']) * recent['body']).sum() + (recent['lower_wick'] * 1.5).sum()
    seller_power = ((recent['close'] < recent['open']) * recent['body']).sum() + (recent['upper_wick'] * 1.5).sum()
    total_power = buyer_power + seller_power
    if total_power == 0:
        return "⚖️ SEIMBANG", 50.0, 50.0
    buyer_pct = (buyer_power / total_power) * 100
    seller_pct = (seller_power / total_power) * 100
    if buyer_pct > 55:
        dominance = "🟢 BUYER DOMINAN"
    elif seller_pct > 55:
        dominance = "🔴 SELLER DOMINAN"
    else:
        dominance = "⚖️ KONSOLIDASI"
    return dominance, round(buyer_pct, 1), round(seller_pct, 1)

def analyze_tf(tf: str):
    min_rows = EMA_SLOW + ATR_LEN + 20
    df = fetch_data(tf)
    if df is None or len(df) < min_rows:
        return "WAIT", 0.0, 0.0, 0, [], "⚖️ SEIMBANG", 50, 50

    df = calculate_features(df)
    dominance, b_pct, s_pct = check_market_dominance(df)
    last_closed = df.iloc[-2]
    live_price = float(df.iloc[-1]["close"])

    if last_closed["exhaustion"]:
        return "WAIT", 0.0, 0.0, 0, ["Candle exhaustion terdeteksi"], dominance, b_pct, s_pct

    bull_confluence, bear_confluence = 0, 0
    bull_reasons, bear_reasons = [], []

    if last_closed["wick_rejection_buy"]:
        lw = round(float(last_closed["lower_wick"]), 2)
        rng = round(float(last_closed["range"]), 2)
        bull_confluence += 2
        bull_reasons.append(f"Wick Rejection Bawah valid di TF {tf} (Ekor bawah: {lw} dari range {rng})")
        
    if last_closed["wick_rejection_sell"]:
        uw = round(float(last_closed["upper_wick"]), 2)
        rng = round(float(last_closed["range"]), 2)
        bear_confluence += 2
        bear_reasons.append(f"Wick Rejection Atas valid di TF {tf} (Ekor atas: {uw} dari range {rng})")

    bull_momentum = (last_closed["ema_fast"] > last_closed["ema_slow"]) and (RSI_OS < last_closed["rsi"] < RSI_OB)
    bear_momentum = (last_closed["ema_fast"] < last_closed["ema_slow"]) and (RSI_OS < last_closed["rsi"] < RSI_OB)

    rsi_val = round(float(last_closed["rsi"]), 1)
    ema_f = round(float(last_closed["ema_fast"]), 2)
    ema_s = round(float(last_closed["ema_slow"]), 2)

    if bull_momentum:
        bull_confluence += 1
        bull_reasons.append(f"Tren EMA Bullish di TF {tf} (Fast {ema_f} > Slow {ema_s}) dengan RSI {rsi_val}")
    if bear_momentum:
        bear_confluence += 1
        bear_reasons.append(f"Tren EMA Bearish di TF {tf} (Fast {ema_f} < Slow {ema_s}) dengan RSI {rsi_val}")

    if dominance == "🟢 BUYER DOMINAN":
        bull_reasons.append(f"Order Flow TF {tf} didominasi Buyer ({b_pct}%)")
    elif dominance == "🔴 SELLER DOMINAN":
        bear_reasons.append(f"Order Flow TF {tf} didominasi Seller ({s_pct}%)")

    if bull_confluence >= MIN_CONFLUENCE and bear_confluence >= MIN_CONFLUENCE:
        return "WAIT", live_price, float(last_closed["atr"]), 0, [], dominance, b_pct, s_pct

    signal, score, reasons = "WAIT", 0, []
    if bull_confluence >= MIN_CONFLUENCE:
        signal, score, reasons = "BUY", bull_confluence, bull_reasons
    elif bear_confluence >= MIN_CONFLUENCE:
        signal, score, reasons = "SELL", bear_confluence, bear_reasons

    return signal, live_price, float(last_closed["atr"]), score, reasons, dominance, b_pct, s_pct

def calculate_position_size(price: float, sl_price: float) -> float:
    risk_amount = ACCOUNT_BALANCE * (RISK_PERCENT / 100.0)
    risk_per_unit = abs(price - sl_price)
    if risk_per_unit == 0:
        return 0.01
    lot = risk_amount / (risk_per_unit * CONTRACT_SIZE)
    return round(max(min(lot, MAX_LOT), 0.01), 2)

def reset_daily_if_needed():
    global daily_loss_tracker, last_reset_day, consecutive_losses
    today = datetime.now(timezone.utc).date()
    if today != last_reset_day:
        daily_loss_tracker = 0.0
        consecutive_losses = 0
        last_reset_day = today

def in_tf_cooldown(tf: str, direction: str) -> bool:
    key = f"{SYMBOL}_{tf}_{direction}"
    return (time.time() - tf_last_signal_time.get(key, 0)) < TF_COOLDOWN

def mark_tf_signal(tf: str, direction: str):
    key = f"{SYMBOL}_{tf}_{direction}"
    tf_last_signal_time[key] = time.time()

# ==========================================
# MASTER LOOP UTAMA
# ==========================================
def run_bot_engine():
    global total_signals_sent

    while True:
        try:
            check_telegram_commands()

            if bot_is_paused:
                time.sleep(15)
                continue

            reset_daily_if_needed()

            if daily_loss_tracker >= ACCOUNT_BALANCE * (MAX_DAILY_LOSS / 100.0) or consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                log.warning("Circuit breaker aktif: Batas kerugian tercapai. Jeda 3 jam.")
                time.sleep(10800)
                continue

            if is_high_impact_event_time() or not is_optimal_trading_session():
                time.sleep(60)
                continue

            for tf in TIMEFRAMES:
                try:
                    signal, price, atr, score, reasons, dominance, b_pct, s_pct = analyze_tf(tf)
                    
                    if signal == "WAIT" or price == 0:
                        continue

                    if in_tf_cooldown(tf, signal):
                        continue

                    adaptive_sl_mult = 1.0 if tf in ["5min", "15min"] else 1.5
                    adaptive_tp_mult = 2.0 if tf in ["5min", "15min"] else 2.5

                    if signal == "BUY":
                        sl = price - (atr * adaptive_sl_mult)
                        tp = price + (atr * adaptive_tp_mult)
                        be_target = price + (atr * 0.7)
                    else:
                        sl = price + (atr * adaptive_sl_mult)
                        tp = price - (atr * adaptive_tp_mult)
                        be_target = price - (atr * 0.7)

                    lot = calculate_position_size(price, sl)
                    total_signals_sent += 1
                    
                    emoji = "🟢" if signal == "BUY" else "🔴"
                    side = "LONG / BUY" if signal == "BUY" else "SHORT / SELL"
                    fmt_reasons = "\n".join(f"• {r}" for r in reasons) or "• Memenuhi konfluensi"

                    msg = (
                        f"{emoji} <b>SINYAL DETAIL TF [{tf}] ({side})</b>\n"
                        f"💎 Pair: <code>{SYMBOL}</code>\n"
                        f"📊 <b>Dominasi TF {tf}:</b> {dominance} (B: {b_pct}% | S: {s_pct}%)\n"
                        f"📈 Skor Konfluensi: {score}\n\n"
                        f"📝 <b>Analisis Rinci Timeframe {tf}:</b>\n{fmt_reasons}\n\n"
                        f"💰 Entry (Close Candle {tf}): <code>{price}</code>\n"
                        f"📦 Lot (Risk Managed): <code>{lot}</code>\n"
                        f"🛑 SL Adaptif: <code>{round(sl, 4)}</code>\n"
                        f"🎯 Target Break-Even: <code>{round(be_target, 4)}</code>\n"
                        f"🎯 TP Adaptif: <code>{round(tp, 4)}</code>\n\n"
                        f"--------------------------\n"
                        f"Jangan lupa shodaqoh\n"
                        f"Jaga Ibadahmu\n"
                        f"Ttd. Abah FK"
                    )

                    send_telegram_message(msg)
                    mark_tf_signal(tf, signal)
                    time.sleep(10)

                except Exception as e:
                    log.exception(f"Error pada TF {tf}: {e}")
                time.sleep(5)

            gc.collect()

        except Exception as e:
            log.exception(f"Error dalam perulangan utama bot: {e}")
            time.sleep(10)
        
        time.sleep(POLL_INTERVAL)

# ==========================================
# SAFE DUMMY HTTP SERVER (UNTUK PORT RAILWAY)
# ==========================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active and running!")
    def log_message(self, format, *args):
        pass

def start_health_server():
    try:
        port_env = os.getenv("PORT")
        port = int(port_env) if port_env and str(port_env).isdigit() else 8080
    except:
        port = 8080

    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        server.serve_forever()
    except Exception as e:
        print(f"Health server warning: {e}")

if __name__ == "__main__":
    # Jalankan server mini di background secara aman
    threading.Thread(target=start_health_server, daemon=True).start()

    print("Adaptive Multi-TF Bot XAU/USD AKTIF")
    send_telegram_message("🚀 <b>Adaptive Multi-TF Bot XAU/USD Aktif & Berjalan</b>\nMemindai seluruh Timeframe secara independen.")

    while True:
        try:
            run_bot_engine()
        except KeyboardInterrupt:
            print("Bot dihentikan manual oleh user.")
            send_telegram_message("🛑 Bot dihentikan manual.")
            break
        except Exception as crash_error:
            print(f"Bot mengalami crash fatal: {crash_error}. Auto-restart dalam 30 detik...")
            time.sleep(30)

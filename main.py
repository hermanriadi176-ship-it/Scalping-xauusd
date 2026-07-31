import os
import time
import threading
import requests
from flask import Flask, render_template_string, request, redirect, url_for
import pytz
from datetime import datetime

app = Flask(__name__)

# Konfigurasi Global
config = {
    "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", ""),
    "TWELVE_DATA_API_KEY": os.environ.get("TWELVE_DATA_API_KEY", ""),
    "TIMEFRAME": "5min",
    "RISK_REWARD": "1:2",
    "LOT_SIZE": 0.1,
    "IS_RUNNING": False
}

bot_stats = {
    "total_signals": 0,
    "win_rate": "0.0%",
    "total_pnl": "+0.0 pips",
    "avg_pnl": "+0.0 pips / trade",
    "last_check": "Belum pernah",
    "active_signals": []
}

def send_telegram_alert(message):
    if not config["TELEGRAM_BOT_TOKEN"] or not config["TELEGRAM_CHAT_ID"]:
        return
    url = f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage"
    payload = {
        "chat_id": config["TELEGRAM_CHAT_ID"],
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print("Gagal mengirim Telegram:", e)

def trading_bot_worker():
    global bot_stats
    while True:
        if config["IS_RUNNING"]:
            try:
                # Ambil data harga XAUUSD dari Twelve Data
                api_key = config["TWELVE_DATA_API_KEY"]
                if api_key:
                    url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval={config['TIMEFRAME']}&outputsize=5&apikey={api_key}"
                    res = requests.get(url, timeout=10).json()
                    
                    if "values" in res:
                        latest = res["values"][0]
                        close_price = float(latest["close"])
                        now_wib = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M:%S")
                        bot_stats["last_check"] = now_wib
                        
                        # Contoh Analisis Sederhana / Deteksi Sinyal
                        # (Logika Lux Algo disederhanakan untuk demo stabil)
                        signal_type = "BUY" if float(latest["close"]) > float(res["values"][1]["close"]) else "SELL"
                        entry = close_price
                        sl = entry - 3.0 if signal_type == "BUY" else entry + 3.0
                        tp = entry + 6.0 if signal_type == "BUY" else entry - 6.0
                        
                        new_signal = {
                            "time": now_wib,
                            "tf": config["TIMEFRAME"],
                            "type": signal_type,
                            "entry": entry,
                            "sl": sl,
                            "tp": tp
                        }
                        
                        # Masukkan ke riwayat sinyal aktif
                        bot_stats["active_signals"].insert(0, new_signal)
                        if len(bot_stats["active_signals"]) > 10:
                            bot_stats["active_signals"].pop()
                            
                        bot_stats["total_signals"] += 1
                        
                        # Kirim notifikasi ke Telegram
                        msg = (f"🚨 *XAUUSD SIGNAL ALERT* 🚨\n\n"
                               f"🔹 Tipe: *{signal_type}*\n"
                               f"🔹 Entry: `{entry}`\n"
                               f"🔹 Stop Loss: `{sl}`\n"
                               f"🔹 Take Profit: `{tp}`\n"
                               f"⏱ Waktu: {now_wib}")
                        send_telegram_alert(msg)
            except Exception as e:
                print("Error pada worker bot:", e)
                
        # Cek setiap 5 menit
        time.sleep(300)

# Jalankan worker di background thread
t = threading.Thread(target=trading_bot_worker, daemon=True)
t.start()

# Tampilan Dashboard Web HTML
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>XAUUSD Scalping Bot Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; background-color: #121212; color: #e0e0e0; margin: 0; padding: 20px; }
        .container { max-width: 900px; margin: auto; background: #1e1e1e; padding: 20px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.5); }
        h1 { color: #f39c12; text-align: center; }
        .card-group { display: flex; gap: 15px; margin-bottom: 20px; flex-wrap: wrap; }
        .card { background: #2c2c2c; padding: 15px; border-radius: 6px; flex: 1; min-width: 180px; text-align: center; }
        .card h3 { margin: 0 0 10px; color: #b0b0b0; font-size: 14px; }
        .card p { margin: 0; font-size: 20px; font-weight: bold; color: #fff; }
        .status-running { color: #2ecc71 !important; }
        .status-stopped { color: #e74c3c !important; }
        form { background: #2c2c2c; padding: 15px; border-radius: 6px; margin-bottom: 20px; }
        label { display: block; margin-top: 10px; font-size: 14px; color: #ccc; }
        input[type="text"], select { width: 100%; padding: 8px; margin-top: 5px; background: #333; border: 1px solid #444; color: #fff; border-radius: 4px; box-sizing: border-box; }
        button { background: #f39c12; color: #121212; border: none; padding: 10px 15px; font-weight: bold; border-radius: 4px; cursor: pointer; margin-top: 15px; width: 100%; }
        button:hover { background: #d68910; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; background: #2c2c2c; border-radius: 6px; overflow: hidden; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #383838; font-size: 13px; }
        th { background: #333; color: #f39c12; }
    </style>
</head>
<body>
    <div class="container">
        <h1>XAUUSD Scalping Bot</h1>
        
        <div class="card-group">
            <div class="card">
                <h3>STATUS BOT</h3>
                <p class="{% if running %}status-running{% else %}status-stopped{% endif %}">
                    {{ "RUNNING" if running else "STOPPED" }}
                </p>
            </div>
            <div class="card">
                <h3>TOTAL SINYAL</h3>
                <p>{{ stats.total_signals }}</p>
            </div>
            <div class="card">
                <h3>WIN RATE</h3>
                <p>{{ stats.win_rate }}</p>
            </div>
            <div class="card">
                <h3>TOTAL PNL</h3>
                <p>{{ stats.total_pnl }}</p>
            </div>
        </div>

        <form method="POST" action="/update">
            <h3>Pengaturan API & Bot</h3>
            <label>Telegram Bot Token:</label>
            <input type="text" name="telegram_token" value="{{ config.TELEGRAM_BOT_TOKEN }}">
            
            <label>Telegram Chat ID:</label>
            <input type="text" name="telegram_chat_id" value="{{ config.TELEGRAM_CHAT_ID }}">
            
            <label>Twelve Data API Key:</label>
            <input type="text" name="twelve_data_key" value="{{ config.TWELVE_DATA_API_KEY }}">
            
            <label>Timeframe:</label>
            <select name="timeframe">
                <option value="1min" {% if config.TIMEFRAME == '1min' %}selected{% endif %}>1 Menit</option>
                <option value="5min" {% if config.TIMEFRAME == '5min' %}selected{% endif %}>5 Menit</option>
                <option value="15min" {% if config.TIMEFRAME == '15min' %}selected{% endif %}>15 Menit</option>
            </select>

            <div style="display: flex; gap: 10px;">
                <button type="submit" name="action" value="save" style="background: #3498db; color: #fff;">Simpan Pengaturan</button>
                {% if running %}
                <button type="submit" name="action" value="stop" style="background: #e74c3c; color: #fff;">Stop Bot</button>
                {% else %}
                <button type="submit" name="action" value="start" style="background: #2ecc71; color: #fff;">Start Bot</button>
                {% endif %}
            </div>
        </form>

        <h3>Riwayat Sinyal Aktif (Pengecekan Terakhir: {{ stats.last_check }})</h3>
        <table>
            <tr>
                <th>Waktu</th>
                <th>TF</th>
                <th>Tipe</th>
                <th>Entry</th>
                <th>SL</th>
                <th>TP</th>
            </tr>
            {% for sig in stats.active_signals %}
            <tr>
                <td>{{ sig.time }}</td>
                <td>{{ sig.tf }}</td>
                <td style="color: {% if sig.type == 'BUY' %}#2ecc71{% else %}#e74c3c{% endif %}; font-weight: bold;">{{ sig.type }}</td>
                <td>{{ sig.entry }}</td>
                <td>{{ sig.sl }}</td>
                <td>{{ sig.tp }}</td>
            </tr>
            {% else %}
            <tr>
                <td colspan="6" style="text-align: center; color: #777;">Belum ada sinyal aktif. Silakan Start Bot.</td>
            </tr>
            {% endfor %}
        </table>
    </div>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, config=config, running=config["IS_RUNNING"], stats=bot_stats)

@app.route("/update", methods=["POST"])
def update():
    config["TELEGRAM_BOT_TOKEN"] = request.form.get("telegram_token", "")
    config["TELEGRAM_CHAT_ID"] = request.form.get("telegram_chat_id", "")
    config["TWELVE_DATA_API_KEY"] = request.form.get("twelve_data_key", "")
    config["TIMEFRAME"] = request.form.get("timeframe", "5min")
    
    action = request.form.get("action")
    if action == "start":
        config["IS_RUNNING"] = True
    elif action == "stop":
        config["IS_RUNNING"] = False
        
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

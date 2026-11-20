#!/data/data/com.termux/files/usr/bin/bash
# SHADOW VOLUME ALERT launcher: keeps the phone awake and restarts the bot if it crashes.
cd "$(dirname "$0")" || exit 1

if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
fi

if [ ! -f config.env ]; then
    echo "config.env not found. Run: cp config.example.env config.env  then edit it with: nano config.env"
    exit 1
fi

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting SHADOW VOLUME ALERT"
    python shadow_volume_alert.py
    code=$?
    if [ "$code" -eq 0 ]; then
        echo "Bot stopped normally."
        break
    fi
    if [ "$code" -eq 2 ]; then
        echo "Telegram token is wrong. Fix TELEGRAM_BOT_TOKEN in config.env (nano config.env), then run: bash start.sh"
        break
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] bot exited with code $code, restarting in 30 seconds (Ctrl+C to cancel)"
    sleep 30
done

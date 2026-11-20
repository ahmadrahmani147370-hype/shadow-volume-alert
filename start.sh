#!/data/data/com.termux/files/usr/bin/bash
# SHADOW VOLUME ALERT launcher: keeps the phone awake and restarts the bot if it crashes.
# Run it with:  bash start.sh      (works from any folder)
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" || exit 1
DIR="$(pwd)"
LOG="$DIR/shadow_volume_alert.log"

# Pick the Python command (Termux has "python"; some systems only "python3")
if command -v python >/dev/null 2>&1; then PY=python
elif command -v python3 >/dev/null 2>&1; then PY=python3
else
    echo "Python is not installed. Run: pkg install -y python"
    exit 1
fi

if [ ! -f config.env ]; then
    echo "config.env not found in $DIR"
    echo "Run: cp config.example.env config.env   then edit it: nano config.env"
    exit 1
fi

if ! $PY -c "import requests" >/dev/null 2>&1; then
    echo "Installing the 'requests' library..."
    $PY -m pip install -r requirements.txt || { echo "pip install failed. Run: pip install requests"; exit 1; }
fi

# Refuse to start a second copy (two copies fight over Telegram commands)
if [ -f "$DIR/.start.pid" ] && kill -0 "$(cat "$DIR/.start.pid" 2>/dev/null)" 2>/dev/null; then
    echo "The bot is already running (start.sh pid $(cat "$DIR/.start.pid"))."
    echo "Stop it first with: bash stop.sh"
    exit 1
fi
echo $$ > "$DIR/.start.pid"
trap 'rm -f "$DIR/.start.pid"' EXIT

# Keep the CPU awake (needs the Termux:API app for full effect; missing is not fatal)
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock 2>/dev/null || true
fi

echo "$($PY shadow_volume_alert.py --version 2>/dev/null)"
echo "Folder : $DIR"
echo "Python : $($PY --version 2>&1)"
echo "Log    : $LOG"
echo "Errors : $DIR/errors.log"
echo "Watch the log in another session with:  tail -f $LOG"
echo "Find setup problems with:               $PY shadow_volume_alert.py --check"
echo

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting bot"
    # Python errors (tracebacks) are shown here AND saved in errors.log
    $PY shadow_volume_alert.py 2> >(tee -a "$DIR/errors.log" >&2)
    code=$?
    sleep 1
    if [ "$code" -eq 0 ]; then
        echo "Bot stopped normally."
        break
    fi
    echo
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] bot exited with code $code. Last log lines:"
    tail -n 25 "$LOG" 2>/dev/null
    if [ -s "$DIR/errors.log" ]; then
        echo "--- last Python errors ($DIR/errors.log) ---"
        tail -n 15 "$DIR/errors.log"
    fi
    echo
    if [ "$code" -eq 2 ]; then
        echo "Telegram token is wrong. Fix TELEGRAM_BOT_TOKEN in config.env (nano config.env), then: bash start.sh"
        echo "Roman Urdu: token galat hai. config.env mein BotFather wala token theek karein."
        break
    fi
    if [ "$code" -eq 3 ]; then
        echo "Another copy is already running. Run: bash stop.sh   then: bash start.sh"
        echo "Roman Urdu: bot pehle se chal raha hai. 'bash stop.sh' phir 'bash start.sh'."
        break
    fi
    echo "Restarting in 30 seconds (Ctrl+C to cancel). Diagnose with: $PY shadow_volume_alert.py --check"
    sleep 30
done

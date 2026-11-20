#!/data/data/com.termux/files/usr/bin/bash
# Stop SHADOW VOLUME ALERT and its restart loop
pkill -f "bash.*start.sh"
pkill -f "shadow_volume_alert.py"
command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock
echo "SHADOW VOLUME ALERT stopped."

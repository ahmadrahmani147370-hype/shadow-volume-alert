#!/data/data/com.termux/files/usr/bin/bash
# Stop SHADOW VOLUME ALERT: every copy, in any folder, old and new versions.
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" || exit 1
me=$$
killed=0

# 1) restart loops (start.sh) - stop them first so they don't restart the bot
for p in /proc/[0-9]*; do
    pid=${p#/proc/}
    [ "$pid" = "$me" ] && continue
    cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    case "$cmd" in
        *start.sh*)
            cwd=$(readlink "$p/cwd" 2>/dev/null)
            if [ -f "$cwd/shadow_volume_alert.py" ]; then
                kill "$pid" 2>/dev/null && { echo "stopped start.sh loop (pid $pid, $cwd)"; killed=1; }
            fi ;;
    esac
done

# 2) the bot itself
for p in /proc/[0-9]*; do
    pid=${p#/proc/}
    cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    case "$cmd" in
        *python*shadow_volume_alert.py*)
            case "$cmd" in *--check*|*--selftest*) continue ;; esac
            kill "$pid" 2>/dev/null && { echo "stopped bot (pid $pid: $cmd)"; killed=1; } ;;
    esac
done

# 3) fallback if /proc is not readable
if command -v pkill >/dev/null 2>&1; then
    pkill -f "python.*shadow_volume_alert.py" 2>/dev/null && killed=1
fi

sleep 2
# force-kill anything still alive
for p in /proc/[0-9]*; do
    cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    case "$cmd" in *python*shadow_volume_alert.py*)
        case "$cmd" in *--check*|*--selftest*) continue ;; esac
        kill -9 "${p#/proc/}" 2>/dev/null ;; esac
done

rm -f .start.pid
command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock 2>/dev/null
if [ "$killed" = 1 ]; then echo "SHADOW VOLUME ALERT stopped."; else echo "SHADOW VOLUME ALERT was not running."; fi

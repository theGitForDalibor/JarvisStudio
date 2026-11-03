#!/usr/bin/env bash
# Jarvis Studio - Linux/macOS Background Daemon Script

ACTION="${1:-status}"
PORT="${2:-8000}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$DIR/data/jarvis_studio.pid"
LOG_FILE="$DIR/logs/jarvis_daemon.log"

mkdir -p "$DIR/data" "$DIR/logs"

get_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if ps -p "$pid" > /dev/null 2>&1; then
            echo "$pid"
            return 0
        fi
    fi
    echo ""
}

case "$ACTION" in
    start)
        PID=$(get_pid)
        if [ -n "$PID" ]; then
            echo "[Jarvis Studio] Already running (PID: $PID)"
            exit 0
        fi
        echo "[Jarvis Studio] Starting background server on port $PORT..."
        nohup uv run jarvis-studio serve --port "$PORT" --host 0.0.0.0 > "$LOG_FILE" 2>&1 &
        NEW_PID=$!
        echo "$NEW_PID" > "$PID_FILE"
        echo "[Jarvis Studio] Started successfully! PID: $NEW_PID"
        echo "[Jarvis Studio] Web UI accessible at: http://localhost:$PORT/app"
        ;;
    stop)
        PID=$(get_pid)
        if [ -n "$PID" ]; then
            echo "[Jarvis Studio] Stopping daemon PID $PID..."
            kill "$PID"
            rm -f "$PID_FILE"
            echo "[Jarvis Studio] Daemon stopped."
        else
            echo "[Jarvis Studio] No running daemon found."
        fi
        ;;
    status)
        PID=$(get_pid)
        if [ -n "$PID" ]; then
            echo "[Jarvis Studio] Status: RUNNING (PID: $PID)"
            echo "[Jarvis Studio] Web UI: http://localhost:$PORT/app"
        else
            echo "[Jarvis Studio] Status: STOPPED"
        fi
        ;;
    restart)
        "$0" stop
        sleep 2
        "$0" start "$PORT"
        ;;
    *)
        echo "Usage: $0 {start|stop|status|restart} [port]"
        exit 1
        ;;
esac

#!/bin/bash
# Restart the MCP Google service via systemd.
# Usage: ./restart_server.sh

set -e

SERVICE="mcp-google.service"
PID_FILE="$(dirname "$0")/service.pid"

echo "=== Restarting $SERVICE ==="

# 1. Stop the service
echo "Stopping $SERVICE..."
sudo systemctl stop "$SERVICE" 2>/dev/null || true

# 2. Kill any lingering processes on the health port (11499) and proxy port (11403)
echo "Freeing ports..."
fuser -k 11499/tcp 2>/dev/null || true
fuser -k 11403/tcp 2>/dev/null || true

# 3. Clean up stale PID file
rm -f "$PID_FILE"

# 4. Wait for ports to be fully released
sleep 2

# 5. Reload systemd and start
echo "Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "Starting $SERVICE..."
sudo systemctl start "$SERVICE"

# 6. Check status
sleep 2
echo ""
echo "=== Service Status ==="
sudo systemctl status "$SERVICE" --no-pager -l || true

echo ""
echo "=== Recent Logs ==="
journalctl -u "$SERVICE" --no-pager -n 20 2>/dev/null || \
    tail -20 "$(dirname "$0")/service.log" 2>/dev/null || true

echo ""
echo "Done. Health endpoint: http://localhost:11499/health"
# Restart the MCP google proxy server in the background.
# Use this when you've edited the source and want the server to pick up the changes.
set -e

REPO_DIR="/home/sameer/Shared/Sync/Private/Apps/services/mcp/noapi-google-search-mcp"
LOG_FILE="$REPO_DIR/service.log"
PROCESS_FILE="$REPO_DIR/service.pid"
PYTHON_BIN="/home/sameer/anaconda3/envs/mcp-google/bin/python"
NODE_BIN_DIR="/home/sameer/.n/n/versions/node/26.3.0/bin"

cd "$REPO_DIR"

# Stop any running instance
for pid in $(pgrep -f "mcp-proxy.*--port 11403" || true); do
    kill -TERM "$pid" 2>/dev/null || true
done
for pid in $(pgrep -f "python -i -m google_search_mcp" || true); do
    kill -TERM "$pid" 2>/dev/null || true
done
sleep 1

# Reap if anything still running
for pid in $(pgrep -f "mcp-proxy.*--port 11403" || true); do
    kill -KILL "$pid" 2>/dev/null || true
done
for pid in $(pgrep -f "python -i -m google_search_mcp" || true); do
    kill -KILL "$pid" 2>/dev/null || true
done

# Launch via npm mcp-proxy (matches the existing start.sh path)
export PATH="$NODE_BIN_DIR:$PATH"
export PYTHONUNBUFFERED=1
export SKIP_COOKIE_VALIDATION=1
export ENABLE_HEALTH_SERVER=1
export HEALTH_HOST=0.0.0.0
export HEALTH_PORT=11499

nohup /home/sameer/.n/n/versions/node/26.3.0/bin/node \
    /home/sameer/.n/n/versions/node/26.3.0/bin/npx \
    -y mcp-proxy --port 11403 --transport streamable-http \
    -- "$PYTHON_BIN" -i -m google_search_mcp \
    >> "$LOG_FILE" 2>&1 &

echo "Started PID $! → $LOG_FILE"
echo "PID $! → $PROCESS_FILE"
echo $! > "$PROCESS_FILE"
disown

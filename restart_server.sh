#!/bin/bash
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

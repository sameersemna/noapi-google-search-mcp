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
echo "MCP endpoints:      http://localhost:11403/mcp (streamable HTTP)"
echo "                    http://localhost:11403/sse (deprecated SSE)"

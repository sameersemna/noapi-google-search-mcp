#!/bin/bash

dirUser='/home/sameer'
dirWork="$dirUser/Shared/Sync/Private/Apps/services/mcp/noapi-google-search-mcp"
condaEnvName="mcp-google"
pathPython="$dirUser/anaconda3/envs/$condaEnvName/bin/python"

# 1. Move into the working directory so relative package paths resolve
cd "$dirWork" || exit 1

# 2. The HTTP transports are served natively by the app itself — no mcp-proxy.
#    (`--transport streamable-http` used to be passed to mcp-proxy, which has no
#    such option and silently ignored it.)
httpCmd="$pathPython -m google_search_mcp --transport streamable-http --host 0.0.0.0 --port 11403"
echo "Running command: $httpCmd"

# 3. Source /etc/noapi-google-search-mcp.env if present — same env file
#    systemd reads. Keeps dev/prod parity. Missing file is OK (dev mode).
ENV_FILE='/etc/noapi-google-search-mcp.env'
if [ -r "$ENV_FILE" ]; then
    # `set -a` auto-exports every variable assigned in this block.
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
    echo "Loaded env from $ENV_FILE"
else
    echo "No $ENV_FILE — using inline defaults only"
fi

# 4. Export global env flags cleanly
export PYTHONUNBUFFERED=1
export SKIP_COOKIE_VALIDATION=1

# 5. Launch the server (blocking). Endpoints: /mcp (streamable HTTP) and
#    /sse + /messages/ (deprecated SSE).
$httpCmd

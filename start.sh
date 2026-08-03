#!/bin/bash

dirUser='/home/sameer'
dirWork="$dirUser/Shared/Sync/Private/Apps/services/mcp/noapi-google-search-mcp"
condaEnvName="mcp-google"
versionNode="26.3.0"
pathPython="$dirUser/anaconda3/envs/$condaEnvName/bin/python"
pathNpx="$dirUser/.n/n/versions/node/$versionNode/bin/node $dirUser/.n/n/versions/node/$versionNode/bin/npx"

# 1. Move into the working directory so relative package paths resolve
cd "$dirWork" || exit 1

# 2. Add the -i flag to keep Python's standard input loop open inside the proxy
pythonCmd="$pathPython -i -m google_search_mcp"
echo "Running command: $pythonCmd"

# 3. Export global env flags cleanly
export PYTHONUNBUFFERED=1
export SKIP_COOKIE_VALIDATION=1

# 4. Launch mcp-proxy using standard execution strings instead of an isolated shell block
$pathNpx -y mcp-proxy --port 11403 --debug --transport streamable-http -- $pythonCmd
# uvx -y mcp-proxy --port 11403 --debug --transport streamable-http -- $pythonCmd

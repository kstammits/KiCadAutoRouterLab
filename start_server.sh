#!/bin/bash
# Start the KiCad AutoRouter workflow UI server
cd /Users/karl/Workspace/KiCadAutoRouterLab
PORT=${1:-8000}

# Open the browser shortly after startup, in the background, then replace
# this shell with the server (exec) so Ctrl-C / SIGTERM go straight to
# Python's clean SIGINT/SIGTERM handler.
(
    sleep 2
    url="http://127.0.0.1:$PORT"
    if command -v open >/dev/null 2>&1; then
        # macOS
        open "$url"
    elif command -v xdg-open >/dev/null 2>&1; then
        # Linux
        xdg-open "$url"
    elif command -v start >/dev/null 2>&1; then
        # Windows
        start "$url"
    else
        echo "Server running at $url (open manually)"
    fi
) &

exec .venv/bin/python ui/server.py "$PORT"

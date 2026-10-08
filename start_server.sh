#!/bin/bash
# Start the KiCad AutoRouter workflow UI server
cd /Users/karl/Workspace/KiCadAutoRouterLab
PORT=${1:-8000}

# Start server in background
.venv/bin/python ui/server.py $PORT &
SERVER_PID=$!

# Trap signals and forward to child process
trap 'kill $SERVER_PID; wait $SERVER_PID; exit 1' INT TERM

# Wait a moment for server to start
sleep 3

# Open default browser
if command -v open >/dev/null 2>&1; then
    # macOS
    open "http://127.0.0.1:$PORT"
elif command -v xdg-open >/dev/null 2>&1; then
    # Linux
    xdg-open "http://127.0.0.1:$PORT"
elif command -v start >/dev/null 2>&1; then
    # Windows
    start "http://127.0.0.1:$PORT"
else
    echo "Server running at http://127.0.0.1:$PORT (open manually)"
fi

# Wait for server process
wait $SERVER_PID

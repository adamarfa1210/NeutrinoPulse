#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
echo "Starting NeutrinoPulse Backend on http://127.0.0.1:8090 (UDP Port ${UDP_PORT:-9999})..."
if [ -x ".venv/bin/python" ]; then
  exec .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8090
else
  exec python3 -m uvicorn main:app --host 127.0.0.1 --port 8090
fi

@echo off
cd /d "%~dp0"
echo Starting NeutrinoPulse Backend on http://127.0.0.1:8090 (UDP Port 9999)...
python -m uvicorn main:app --host 127.0.0.1 --port 8090

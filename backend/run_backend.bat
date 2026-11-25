@echo off
cd /d "%~dp0"
echo Starting NeutrinoPulse Backend on http://127.0.0.1:8090 (UDP Port 9999)...
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8090
) else (
  python -m uvicorn main:app --host 127.0.0.1 --port 8090
)

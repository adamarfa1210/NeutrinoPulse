"""NeutrinoPulse FastAPI server: UDP telemetry receiver, WebSocket broadcaster, & AI diagnosis API."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_copilot import diagnose_anomaly
from resolver import DEFAULT_ADDR2LINE, extract_code_context
from telemetry import AnomalyDetector, decode_packet

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

UDP_PORT = int(os.getenv("UDP_PORT", "9999"))
ADDR2LINE_EXE = os.getenv("QNX_ADDR2LINE", DEFAULT_ADDR2LINE)
TARGET_DIR = os.getenv("TARGET_DIR", str(BASE_DIR.parent / "target"))
DEFAULT_BINARY = os.getenv("TARGET_BINARY", str(Path(TARGET_DIR) / "brake_sim_buggy"))
FRONTEND_DIR = BASE_DIR.parent / "frontend"

detector = AnomalyDetector()
ws_clients: set[WebSocket] = set()


class UDPTelemetryProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        pkt = decode_packet(data)
        if pkt is not None:
            detector.ingest(pkt)


async def _ws_broadcaster() -> None:
    while True:
        await asyncio.sleep(0.15)
        if not ws_clients:
            continue
        payload = detector.snapshot()
        dead: list[WebSocket] = []
        for ws in list(ws_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_clients.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        UDPTelemetryProtocol,
        local_addr=("0.0.0.0", UDP_PORT),
    )
    broadcaster = asyncio.create_task(_ws_broadcaster())
    try:
        yield
    finally:
        broadcaster.cancel()
        transport.close()


app = FastAPI(title="NeutrinoPulse QNX Telemetry & AI Copilot", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class DiagnoseRequest(BaseModel):
    ip_hex: str | None = None
    binary_path: str | None = None


@app.get("/api/health")
async def health() -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    return {
        "status": "ok",
        "udp_port": UDP_PORT,
        "llm_provider": os.getenv("LLM_PROVIDER", "gemini"),
        "gemini_configured": bool(api_key and api_key != "your_gemini_api_key_here"),
        "addr2line_available": Path(ADDR2LINE_EXE).is_file(),
    }


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    return detector.snapshot()


@app.post("/api/reset")
async def reset_state() -> dict[str, str]:
    detector.reset()
    return {"status": "reset"}


@app.post("/api/diagnose")
async def run_diagnosis(req: DiagnoseRequest | None = None) -> dict[str, Any]:
    anomaly = detector.last_anomaly
    if anomaly is None:
        if req and req.ip_hex:
            anomaly = {
                "pid": detector.active_pid or 0,
                "tid": 0,
                "priority": 25,
                "state_name": "STATE_REPLY",
                "latency_us": detector.peak_latency_us,
                "ip_hex": req.ip_hex,
                "reason": "Manual diagnosis requested",
                "blocked_streak": 0,
            }
        else:
            raise HTTPException(status_code=400, detail="No anomaly detected yet to diagnose.")

    ip_hex = (req.ip_hex if req and req.ip_hex else None) or anomaly.get("ip_hex", "0x0")
    binary_path = (req.binary_path if req and req.binary_path else None) or DEFAULT_BINARY

    context = extract_code_context(
        ip_hex=ip_hex,
        target_dir=TARGET_DIR,
        binary_path=binary_path,
        addr2line_exe=ADDR2LINE_EXE,
    )
    diagnosis = await diagnose_anomaly(anomaly, context)
    return {
        "anomaly": anomaly,
        "symbol_resolution": context["client_resolution"],
        "diagnosis": diagnosis,
    }


@app.websocket("/ws/telemetry")
async def telemetry_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        await websocket.send_json(detector.snapshot())
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(websocket)
    except Exception:
        ws_clients.discard(websocket)


if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

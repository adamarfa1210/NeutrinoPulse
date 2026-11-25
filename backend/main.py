"""NeutrinoPulse FastAPI server: UDP telemetry receiver, WebSocket broadcaster, & AI diagnosis API."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import shutil
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_copilot import diagnose_anomaly
from resolver import DEFAULT_ADDR2LINE, HEX_IP_RE, compute_cache_stamp, extract_code_context, is_elf_file
from telemetry import AnomalyDetector, decode_packet

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

UDP_PORT = int(os.getenv("UDP_PORT", "9999"))
ADDR2LINE_EXE = os.getenv("QNX_ADDR2LINE", "").strip() or DEFAULT_ADDR2LINE
TARGET_DIR = os.getenv("TARGET_DIR", "").strip() or str(BASE_DIR.parent / "target")
DEFAULT_BINARY = os.getenv("TARGET_BINARY", "").strip() or str(Path(TARGET_DIR) / "brake_sim_buggy")
FRONTEND_DIR = BASE_DIR.parent / "frontend"

detector = AnomalyDetector()
ws_clients: set[WebSocket] = set()
_SOURCE_IMPL_EXTS = {".c", ".cpp", ".cc", ".cxx"}
_runtime_cfg: dict[str, str] = {
    "target_dir": TARGET_DIR,
    "default_binary": DEFAULT_BINARY,
}
_symbol_cache: dict[tuple[str, str, int, int], dict[str, Any]] = {}


def _list_elf_targets(target_dir: str) -> list[str]:
    root = Path(target_dir)
    if not root.is_dir():
        return []
    found = [
        p.name for p in sorted(root.iterdir())
        if p.name != "np_agent" and is_elf_file(p)
    ]
    return found


def _resolve_target_binary(target: str | None = None, binary_path: str | None = None) -> str:
    active_dir = _runtime_cfg["target_dir"]
    target_root = Path(active_dir).resolve()
    if target:
        clean_name = target.strip()
        if clean_name and Path(clean_name).name == clean_name:
            candidate = (target_root / clean_name).resolve()
            if target_root in candidate.parents and is_elf_file(candidate):
                return str(candidate)
    if binary_path:
        try:
            resolved = Path(binary_path).resolve(strict=True)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid binary_path: {exc}") from exc
        if target_root not in resolved.parents or not is_elf_file(resolved):
            raise HTTPException(status_code=400, detail="binary_path must be an ELF file inside configured TARGET_DIR")
        return str(resolved)
    return _runtime_cfg["default_binary"]


def _get_code_context(ip_hex: str, binary_path: str) -> dict[str, Any]:
    active_dir = _runtime_cfg["target_dir"]
    bin_mtime, src_mtime = compute_cache_stamp(binary_path, active_dir)
    cache_key = (ip_hex.lower(), binary_path, bin_mtime, src_mtime)
    if cache_key not in _symbol_cache:
        ctx = extract_code_context(
            ip_hex=ip_hex,
            target_dir=active_dir,
            binary_path=binary_path,
            addr2line_exe=ADDR2LINE_EXE,
        )
        stem = Path(binary_path).stem
        channels = list(ctx.get("server_channels") or [])
        matching = [
            ch for ch in channels
            if Path(ch.get("file", "")).stem == stem
            and Path(ch.get("file", "")).suffix.lower() in _SOURCE_IMPL_EXTS
        ]
        if matching:
            ctx["server_channels"] = matching
        elif not stem.startswith("brake_sim"):
            ctx["server_channels"] = [
                ch for ch in channels
                if not Path(ch.get("file", "")).name.startswith("brake_sim_")
            ]
        else:
            ctx["server_channels"] = channels
        if len(_symbol_cache) >= 64:
            _symbol_cache.clear()
        _symbol_cache[cache_key] = ctx
    return _symbol_cache[cache_key]


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
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class DiagnoseRequest(BaseModel):
    ip_hex: str | None = None
    binary_path: str | None = None
    target: str | None = None


class ConfigUpdateRequest(BaseModel):
    target_dir: str


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    targets = _list_elf_targets(_runtime_cfg["target_dir"])
    default_name = Path(_runtime_cfg["default_binary"]).name
    if targets and default_name not in targets:
        default_name = targets[0]
    return {
        "target_dir": _runtime_cfg["target_dir"],
        "targets": targets,
        "default_target": default_name,
    }


@app.post("/api/config")
async def update_config(req: ConfigUpdateRequest) -> dict[str, Any]:
    raw_dir = req.target_dir.strip()
    if not raw_dir:
        raise HTTPException(status_code=400, detail="target_dir must not be empty")
    try:
        resolved_dir = Path(raw_dir).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid target_dir: {exc}") from exc
    if not resolved_dir.is_dir():
        raise HTTPException(status_code=400, detail="target_dir must be an existing directory")

    _runtime_cfg["target_dir"] = str(resolved_dir)
    targets = _list_elf_targets(str(resolved_dir))
    if targets:
        preferred = "brake_sim_buggy" if "brake_sim_buggy" in targets else targets[0]
        _runtime_cfg["default_binary"] = str(resolved_dir / preferred)
    _symbol_cache.clear()
    return await get_config()


@app.get("/api/health")
async def health() -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    return {
        "status": "ok",
        "udp_port": UDP_PORT,
        "llm_provider": os.getenv("LLM_PROVIDER", "gemini"),
        "gemini_configured": bool(api_key and api_key != "your_gemini_api_key_here"),
        "addr2line_available": bool(Path(ADDR2LINE_EXE).is_file() or shutil.which(ADDR2LINE_EXE)),
    }


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    return detector.snapshot()


@app.post("/api/reset")
async def reset_state() -> dict[str, str]:
    detector.reset()
    _symbol_cache.clear()
    return {"status": "reset"}


@app.get("/api/symbol")
async def resolve_symbol_endpoint(ip_hex: str | None = None, target: str | None = None) -> dict[str, Any]:
    if ip_hex is not None and ip_hex.strip():
        if not HEX_IP_RE.fullmatch(ip_hex.strip()):
            raise HTTPException(status_code=400, detail="Invalid ip_hex format (expected 0x... hex)")
    resolved_ip = ip_hex.strip() if ip_hex and ip_hex.strip() else None
    if not resolved_ip and detector.last_anomaly:
        resolved_ip = detector.last_anomaly.get("ip_hex")
    if not resolved_ip:
        snap = detector.snapshot()
        pool = snap["threads"] or list(detector.threads.values())
        candidates = [t for t in pool if t.get("ip_hex") and t["ip_hex"] != "0x0"]
        if candidates:
            top = max(candidates, key=lambda t: (t.get("latency_us", 0), t.get("priority", 0)))
            resolved_ip = top["ip_hex"]

    if not resolved_ip or resolved_ip == "0x0":
        return {
            "resolved": False,
            "ip_hex": None,
            "client_resolution": {"resolved": False},
            "client_snippet": "",
            "server_channel": None,
        }

    binary_path = _resolve_target_binary(target=target)
    ctx = _get_code_context(resolved_ip, binary_path)
    channels = ctx.get("server_channels") or []
    is_resolved = bool(ctx.get("client_resolution", {}).get("resolved"))
    return {
        "resolved": is_resolved,
        "ip_hex": resolved_ip,
        "target_binary": Path(binary_path).name,
        "client_resolution": ctx.get("client_resolution", {"resolved": False}),
        "client_snippet": ctx.get("client_snippet", ""),
        "server_channel": channels[0] if (channels and is_resolved) else None,
    }


@app.post("/api/diagnose")
async def run_diagnosis(req: DiagnoseRequest | None = None) -> dict[str, Any]:
    anomaly = detector.last_anomaly
    if anomaly is None:
        snap = detector.snapshot()
        pool = snap["threads"] or list(detector.threads.values())
        fallback_ip = req.ip_hex if req and req.ip_hex else None
        top_thread = None
        if not fallback_ip and pool:
            candidates = [t for t in pool if t.get("ip_hex") and t["ip_hex"] != "0x0"]
            if candidates:
                top_thread = max(candidates, key=lambda t: (t.get("latency_us", 0), t.get("priority", 0)))
                fallback_ip = top_thread["ip_hex"]

        if fallback_ip:
            anomaly = {
                "pid": detector.active_pid or 0,
                "tid": top_thread["tid"] if top_thread else 0,
                "priority": top_thread["priority"] if top_thread else 25,
                "state_name": top_thread["state_name"] if top_thread else "RUNNING",
                "latency_us": detector.peak_latency_us,
                "ip_hex": fallback_ip,
                "reason": "Manual target inspection requested",
                "blocked_streak": top_thread.get("blocked_streak", 0) if top_thread else 0,
            }
        else:
            raise HTTPException(status_code=400, detail="No telemetry or anomaly detected yet to diagnose.")

    if req and req.ip_hex and req.ip_hex.strip():
        if not HEX_IP_RE.fullmatch(req.ip_hex.strip()):
            raise HTTPException(status_code=400, detail="Invalid ip_hex format (expected 0x... hex)")
    ip_hex = (req.ip_hex.strip() if req and req.ip_hex and req.ip_hex.strip() else None) or anomaly.get("ip_hex", "0x0")
    binary_path = _resolve_target_binary(
        target=req.target if req else None,
        binary_path=req.binary_path if req else None,
    )

    context = _get_code_context(ip_hex, binary_path)
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

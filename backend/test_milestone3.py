"""Verification test suite for Milestone 3 (Decoder, Detector, addr2line Bridge, FastAPI Diagnose)."""

import os
from pathlib import Path
import struct
import tempfile

os.environ["UDP_PORT"] = "19999"

from fastapi.testclient import TestClient

from ai_copilot import _rule_based_fallback
from main import _get_code_context, _symbol_cache, app, detector
from resolver import extract_code_context, find_addr2line, resolve_ip
from telemetry import PACKET_FMT, PACKET_SIZE, AnomalyDetector, decode_packet


def test_decoder_and_detector() -> None:
    assert PACKET_SIZE == 27, f"Expected 27 bytes, got {PACKET_SIZE}"
    assert decode_packet(b"A" * PACKET_SIZE) is None, "Corrupt 27B noise must be rejected"
    assert decode_packet(struct.pack(PACKET_FMT, 12345, 3, 1, 256, 100, 0x4011E9)) is None
    assert decode_packet(struct.pack(PACKET_FMT, 12345, 3, 1, 25, 99_999_999, 0x4011E9)) is None
    raw = struct.pack(PACKET_FMT, 12345, 3, 6, 25, 65000, 0x4011E9)
    pkt = decode_packet(raw)
    assert pkt is not None
    assert pkt.pid == 12345 and pkt.tid == 3 and pkt.state_name == "STATE_REPLY"

    det = AnomalyDetector()
    # Normal latency should not trigger anomaly
    for _ in range(5):
        det.ingest(decode_packet(struct.pack(PACKET_FMT, 12345, 3, 1, 25, 2000, 0x4011E9)))
    assert not det.is_anomaly

    # Priority inversion spike should trigger anomaly
    det.ingest(pkt)
    assert det.is_anomaly
    assert det.last_anomaly is not None
    assert det.last_anomaly["latency_us"] == 65000

    # Low-prio (<20) stuck STATE_REPLY with 0 us must still log to history (NP-17)
    hist_before = len(det.history)
    det.ingest(decode_packet(struct.pack(PACKET_FMT, 12345, 7, 6, 10, 0, 0x401239)))
    det.ingest(decode_packet(struct.pack(PACKET_FMT, 12345, 7, 6, 10, 0, 0x401239)))
    assert len(det.history) >= hist_before + 1
    print("[PASS] Binary decoder (27 bytes + bounds check) & AnomalyDetector Z-Score/threshold")


def test_addr2line_and_context_pairing() -> None:
    target_dir = str(Path(__file__).resolve().parent.parent / "target")
    binary_path = str(Path(target_dir) / "brake_sim_buggy")
    assert Path(find_addr2line()).name.startswith("ntox86_64-addr2line")
    bad = resolve_ip("--help", binary_path=binary_path)
    assert bad["resolved"] is False and "Invalid" in bad["error"]
    ctx = extract_code_context("0x4011e9", target_dir=target_dir, binary_path=binary_path)

    res = ctx["client_resolution"]
    assert res["resolved"] is True, f"addr2line failed: {res}"
    assert "critical_brake_client" in res["function"]
    assert "brake_sim_core.h" in res["file"]
    assert len(ctx["server_channels"]) >= 1
    assert "_NTO_CHF_FIXED_PRIORITY" in ctx["server_channels"][0]["snippet"]
    print(f"[PASS] addr2line resolved 0x4011e9 -> {res['function']} ({Path(res['file']).name}:{res['line']}) + paired server ChannelCreate")


def test_phase3_cpp_cache_and_fallback() -> None:
    # Recursive C++ ChannelCreate + MsgReceive discovery (NP-12) and dynamic diff/bottleneck fallback
    with tempfile.TemporaryDirectory() as tmp:
        nested = Path(tmp) / "src"
        nested.mkdir()
        cpp_file = nested / "sensor_srv.cpp"
        cpp_file.write_text(
            "int init() {\n  int g_chid = ChannelCreate(_NTO_CHF_FIXED_PRIORITY);\n  return g_chid;\n}\n",
            encoding="utf-8",
        )
        ctx_cpp = extract_code_context("0x0", target_dir=tmp, binary_path=str(Path(tmp) / "missing"))
        assert len(ctx_cpp["server_channels"]) == 1
        assert Path(ctx_cpp["server_channels"][0]["file"]).name == "sensor_srv.cpp"

        calm = _rule_based_fallback(
            {"priority": 25, "latency_us": 0, "state_name": "RUNNING", "tid": 3, "ip_hex": "0x0"},
            ctx_cpp,
            "unit",
        )
        assert calm["severity"] == "WARNING"
        assert "g_chid = ChannelCreate(_NTO_CHF_FIXED_PRIORITY);" in calm["code_before"]
        assert "g_chid = ChannelCreate(0);" in calm["code_after"]

        # Non-priority-inversion server bottleneck with ChannelCreate(0) + MsgReceive()
        dsp_ctx = {
            "client_resolution": {"resolved": True, "function": "fc_cli", "file": "fc.c", "line": 42},
            "client_snippet": ">>> 42 | NP_MSGSEND(coid, ...);",
            "server_channels": [{"file": "srv.c", "line": 10, "snippet": ">>> 10 | int chid = ChannelCreate(0);"}],
            "server_handlers": [{"file": "srv.c", "line": 25, "snippet": ">>> 25 | int rcvid = MsgReceive(chid, &m, sizeof(m), NULL);"}],
        }
        dsp_diag = _rule_based_fallback(
            {"priority": 24, "latency_us": 35000, "state_name": "STATE_REPLY", "tid": 3, "ip_hex": "0x1234"},
            dsp_ctx,
            "unit",
        )
        assert dsp_diag["severity"] == "CRITICAL"
        assert "Bottleneck" in dsp_diag["root_cause"]
        assert "MsgReceive" in dsp_diag["code_before"]
    print("[PASS] Phase 3 recursive C/C++ scan, dynamic diff, & non-PI handler fallback")


def test_fastapi_endpoints() -> None:
    detector.reset()
    # Simulate an incoming UDP anomaly packet into the shared detector
    detector.ingest(decode_packet(struct.pack(PACKET_FMT, 2740246, 3, 6, 25, 65533, 0x4011E9)))

    with TestClient(app) as client:
        r_health = client.get("/api/health")
        assert r_health.status_code == 200
        health = r_health.json()
        assert health["addr2line_available"] is True

        r_symbol = client.get("/api/symbol?target=brake_sim_buggy")
        assert r_symbol.status_code == 200, r_symbol.text
        sym = r_symbol.json()
        assert sym["resolved"] is True
        assert "critical_brake_client" in sym["client_resolution"]["function"]
        assert sym["server_channel"] is not None
        assert "_NTO_CHF_FIXED_PRIORITY" in sym["server_channel"]["snippet"]

        r_fixed = client.get("/api/symbol?ip_hex=0x4011e9&target=brake_sim_fixed")
        assert r_fixed.status_code == 200, r_fixed.text
        assert "ChannelCreate(0)" in (r_fixed.json()["server_channel"] or {}).get("snippet", "")

        assert client.get("/api/symbol?ip_hex=--help").status_code == 400
        assert client.post("/api/diagnose", json={"binary_path": str(Path(__file__).resolve())}).status_code == 400

        r_cfg = client.get("/api/config")
        assert r_cfg.status_code == 200 and "brake_sim_buggy" in r_cfg.json()["targets"]

        # Cache invalidation on /api/reset (NP-13)
        _ = _get_code_context("0x4011e9", str(Path(__file__).resolve().parent.parent / "target" / "brake_sim_buggy"))
        assert len(_symbol_cache) >= 1
        assert client.post("/api/reset").status_code == 200
        assert len(_symbol_cache) == 0

        # Restore anomaly packet before /api/diagnose check
        detector.ingest(decode_packet(struct.pack(PACKET_FMT, 2740246, 3, 6, 25, 65533, 0x4011E9)))

        r_diag = client.post("/api/diagnose", json={"target": "brake_sim_buggy"})
        assert r_diag.status_code == 200, r_diag.text
        body = r_diag.json()
        assert body["symbol_resolution"]["resolved"] is True
        diag = body["diagnosis"]
        assert "ChannelCreate" in diag["code_before"]
        assert "ChannelCreate(0)" in diag["code_after"]
        print(f"[PASS] FastAPI /api/health & /api/diagnose (provider={diag['provider']}, root_cause={diag['root_cause']})")


if __name__ == "__main__":
    test_decoder_and_detector()
    test_addr2line_and_context_pairing()
    test_phase3_cpp_cache_and_fallback()
    test_fastapi_endpoints()
    print(">> ALL MILESTONE 3 TESTS PASSED <<")

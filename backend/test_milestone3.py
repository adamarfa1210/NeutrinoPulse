"""Verification test suite for Milestone 3 (Decoder, Detector, addr2line Bridge, FastAPI Diagnose)."""

import os
from pathlib import Path
import struct

os.environ["UDP_PORT"] = "19999"

from fastapi.testclient import TestClient

from main import app, detector
from resolver import extract_code_context
from telemetry import PACKET_FMT, PACKET_SIZE, AnomalyDetector, decode_packet


def test_decoder_and_detector() -> None:
    assert PACKET_SIZE == 27, f"Expected 27 bytes, got {PACKET_SIZE}"
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
    print("[PASS] Binary decoder (27 bytes) & AnomalyDetector Z-Score/threshold")


def test_addr2line_and_context_pairing() -> None:
    target_dir = str(Path(__file__).resolve().parent.parent / "target")
    binary_path = str(Path(target_dir) / "brake_sim_buggy")
    ctx = extract_code_context("0x4011e9", target_dir=target_dir, binary_path=binary_path)

    res = ctx["client_resolution"]
    assert res["resolved"] is True, f"addr2line failed: {res}"
    assert "critical_brake_client" in res["function"]
    assert "brake_sim_core.h" in res["file"]
    assert len(ctx["server_channels"]) >= 1
    assert "_NTO_CHF_FIXED_PRIORITY" in ctx["server_channels"][0]["snippet"]
    print(f"[PASS] addr2line resolved 0x4011e9 -> {res['function']} ({Path(res['file']).name}:{res['line']}) + paired server ChannelCreate")


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
    test_fastapi_endpoints()
    print(">> ALL MILESTONE 3 TESTS PASSED <<")

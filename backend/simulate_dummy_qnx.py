"""Dummy QNX 8.0 27-byte UDP telemetry generator for end-to-end dashboard monitoring tests."""

import argparse
import random
import socket
import struct
import time

PACKET_FMT = "<IIBHQQ"  # pid, tid, state, priority, latency_us, instruction_pointer
STATE_RUNNING = 1
STATE_READY = 2
STATE_RECEIVE = 5
STATE_REPLY = 6
STATE_NANOSLEEP = 12


def send_frame(
    sock: socket.socket,
    addr: tuple[str, int],
    pid: int,
    buggy: bool,
    tick: int,
) -> int:
    """Emit one 100ms polling snapshot for 4 QNX target threads; return TID 3 latency."""
    spike = buggy and (tick % 3 != 0)
    lat_us = random.randint(64200, 65950) if spike else random.randint(1850, 2450)
    t3_state = STATE_REPLY if spike else STATE_RUNNING
    t2_state = STATE_READY if buggy else STATE_RECEIVE
    t4_state = STATE_RUNNING if spike else STATE_NANOSLEEP

    packets = [
        (pid, 1, STATE_NANOSLEEP, 10, 0, 0x26DEE298A),
        (pid, 2, t2_state, 10, 0, 0x26DEE2D6FD),
        (pid, 3, t3_state, 25, lat_us, 0x4011E9),
        (pid, 4, t4_state, 15, 0, 0x4010E3),
    ]
    for pkt in packets:
        sock.sendto(struct.pack(PACKET_FMT, *pkt), addr)
    return lat_us


def run_simulation(host: str, port: int, mode: str, duration: float, interval: float) -> None:
    assert struct.calcsize(PACKET_FMT) == 27, "Protocol packet size must be 27 bytes"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    addr = (host, port)
    start = time.monotonic()
    tick = 0

    print(f"[DummyQNX] Streaming 27-byte UDP telemetry -> {host}:{port} (mode={mode}, duration={duration}s)")
    try:
        while True:
            elapsed = time.monotonic() - start
            if duration > 0 and elapsed >= duration:
                break

            if mode == "demo":
                cycle_t = elapsed % 18.0
                if cycle_t < 3.0:
                    phase, buggy, pid = "WARMUP_NORMAL", False, 745494
                elif cycle_t < 12.0:
                    phase, buggy, pid = "BUGGY_INVERSION", True, 745494
                else:
                    phase, buggy, pid = "FIXED_RECOVERY", False, 745512
            elif mode == "buggy":
                phase, buggy, pid = "BUGGY_INVERSION", True, 745494
            else:
                phase, buggy, pid = "FIXED_HEALTHY", False, 745512

            lat_us = send_frame(sock, addr, pid=pid, buggy=buggy, tick=tick)
            if tick % 5 == 0:
                flag = "ANOMALY" if lat_us > 10000 else "OK"
                print(f"[{elapsed:5.1f}s] phase={phase:<15} pid={pid} tid=3 lat={lat_us:6d} us [{flag}]")
            tick += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[DummyQNX] Stopped by user.")
    finally:
        sock.close()
        print("[DummyQNX] Stream complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NeutrinoPulse Dummy QNX Telemetry Generator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--mode", choices=["buggy", "fixed", "demo"], default="buggy")
    parser.add_argument("--duration", type=float, default=15.0, help="Seconds to run (0 = infinite)")
    parser.add_argument("--interval", type=float, default=0.15, help="Frame interval in seconds")
    args = parser.parse_args()
    run_simulation(args.host, args.port, args.mode, args.duration, args.interval)

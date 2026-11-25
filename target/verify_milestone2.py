"""End-to-end QEMU target verification script (deploy-and-run over SSH)."""

import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time

TARGET_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TARGET_DIR.parent / "backend"))
from resolver import find_addr2line  # noqa: E402

FMT = "<IIBHQQ"
SIZE = struct.calcsize(FMT)
SSH_KEY = os.getenv("QNX_SSH_KEY", str(Path(r"C:\QNX\qemu_target\keys\qnx_qemu_ed25519") if os.name == "nt" else Path.home() / ".ssh" / "qnx_qemu_ed25519"))
SSH_HOST = os.getenv("QNX_SSH_HOST", "127.0.0.1")
SSH_PORT = os.getenv("QNX_SSH_PORT", "2222")
SSH_USER = os.getenv("QNX_SSH_USER", "root")
KNOWN_HOSTS_DEV = "NUL" if os.name == "nt" else "/dev/null"

SSH_BASE = [
    "ssh",
    *(["-i", SSH_KEY] if Path(SSH_KEY).is_file() else []),
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=no",
    "-o", f"UserKnownHostsFile={KNOWN_HOSTS_DEV}",
    "-o", "Ciphers=aes256-ctr",
    "-o", "MACs=hmac-sha2-256",
    "-p", SSH_PORT,
    f"{SSH_USER}@{SSH_HOST}",
]


def main() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.2)
    sock.bind(("0.0.0.0", int(os.getenv("UDP_PORT", "9999"))))

    packets = []
    stop_rx = False

    def rx_loop():
        while not stop_rx:
            try:
                data, _ = sock.recvfrom(1024)
                if len(data) == SIZE:
                    packets.append(struct.unpack(FMT, data))
            except socket.timeout:
                pass

    t = threading.Thread(target=rx_loop, daemon=True)
    t.start()

    remote_script = (
        "slay -f np_agent brake_sim_buggy brake_sim_fixed 2>/dev/null; sleep 1; "
        "/tmp/np_agent 10.0.2.2 9999 >/tmp/agent.log 2>&1 & "
        "/tmp/brake_sim_buggy >/tmp/buggy.log 2>&1 & "
        "sleep 3; slay -f brake_sim_buggy; sleep 1; "
        "/tmp/brake_sim_fixed >/tmp/fixed.log 2>&1 & "
        "sleep 3; slay -f brake_sim_fixed np_agent"
    )

    print("Running full QEMU verification (6s)...", flush=True)
    subprocess.run(
        SSH_BASE + [remote_script],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20
    )
    time.sleep(0.3)
    stop_rx = True
    t.join(timeout=1)
    sock.close()

    by_pid = {}
    for pid, tid, state, prio, lat, ip in packets:
        by_pid.setdefault(pid, []).append((tid, state, prio, lat, ip))

    print(f"Total UDP packets received: {len(packets)} across {len(by_pid)} PIDs", flush=True)
    addr2line_exe = os.getenv("QNX_ADDR2LINE", "").strip() or find_addr2line()
    buggy_bin = str(TARGET_DIR / "brake_sim_buggy")

    for idx, pid in enumerate(by_pid):
        label = "BUGGY" if idx == 0 else "FIXED"
        cli = [x for x in by_pid[pid] if x[2] == 25]
        lats = [x[3] for x in cli]
        ips = sorted(set(hex(x[4]) for x in cli if x[3] > 10000 and x[4] != 0))
        print(f"  [{label}] pid={pid} pkts={len(by_pid[pid])} prio25_pkts={len(cli)} "
              f"min={min(lats, default=0)} us max={max(lats, default=0)} us anomaly_ips={ips}", flush=True)

        if label == "BUGGY" and ips and Path(addr2line_exe).is_file() and Path(buggy_bin).is_file():
            a2l = subprocess.run(
                [addr2line_exe, "-e", buggy_bin, "-f", "-C", ips[0]],
                capture_output=True, text=True
            )
            print("  ADDR2LINE:", a2l.stdout.strip().replace("\n", " -> "), flush=True)


if __name__ == "__main__":
    main()

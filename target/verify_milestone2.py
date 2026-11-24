import socket
import struct
import subprocess
import threading
import time

FMT = "<IIBHQQ"
SIZE = struct.calcsize(FMT)
SSH_BASE = [
    "ssh", "-i", r"C:\QNX\qemu_target\keys\qnx_qemu_ed25519",
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=NUL",
    "-o", "Ciphers=aes256-ctr",
    "-o", "MACs=hmac-sha2-256",
    "-p", "2222", "root@127.0.0.1",
]


def main() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.2)
    sock.bind(("0.0.0.0", 9999))

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
    res = subprocess.run(
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
    pids = list(by_pid.keys())
    for idx, pid in enumerate(pids):
        label = "BUGGY" if idx == 0 else "FIXED"
        # Cari thread prioritas 25 (critical_brake_client)
        cli = [x for x in by_pid[pid] if x[2] == 25]
        lats = [x[3] for x in cli]
        ips = sorted(set(hex(x[4]) for x in cli if x[3] > 10000 and x[4] != 0))
        print(f"  [{label}] pid={pid} pkts={len(by_pid[pid])} prio25_pkts={len(cli)} "
              f"min={min(lats, default=0)} us max={max(lats, default=0)} us anomaly_ips={ips}", flush=True)

        if label == "BUGGY" and ips:
            a2l = subprocess.run(
                [r"C:\qnx800\host\win64\x86_64\usr\bin\ntox86_64-addr2line.exe",
                 "-e", r"C:\Users\Adam Arfa\Documents\Hackathon\QNX\.code\target\brake_sim_buggy",
                 "-f", "-C", ips[0]],
                capture_output=True, text=True
            )
            print("  ADDR2LINE:", a2l.stdout.strip().replace("\n", " -> "), flush=True)


if __name__ == "__main__":
    main()

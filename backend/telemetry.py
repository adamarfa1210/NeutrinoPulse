"""QNX 8.0 binary telemetry decoder and real-time anomaly detector."""

from collections import deque
from dataclasses import asdict, dataclass
import math
import struct
import time
from typing import Any

PACKET_FMT = "<IIBHQQ"
PACKET_SIZE = struct.calcsize(PACKET_FMT)  # 27 bytes

# State codes verified against $QNX_TARGET/usr/include/sys/states.h
QNX_STATES: dict[int, str] = {
    0: "DEAD",
    1: "RUNNING",
    2: "READY",
    3: "STOPPED",
    4: "SEND",
    5: "RECEIVE",
    6: "STATE_REPLY",
    7: "MQ_SEND",
    8: "MQ_RECEIVE",
    9: "WAITPAGE",
    10: "SIGSUSPEND",
    11: "SIGWAITINFO",
    12: "NANOSLEEP",
    13: "STATE_MUTEX",
    14: "CONDVAR",
    15: "JOIN",
    16: "INTR",
    17: "SEM",
    18: "WAITCTX",
    22: "PIPE",
}

BLOCKED_STATES = {6, 13}  # STATE_REPLY, STATE_MUTEX
LATENCY_THRESHOLD_US = 10_000  # 10 ms real-time budget threshold


@dataclass
class TelemetryPacket:
    pid: int
    tid: int
    state: int
    priority: int
    latency_us: int
    instruction_pointer: int
    state_name: str = ""
    timestamp: float = 0.0


def decode_packet(data: bytes) -> TelemetryPacket | None:
    """Unpack a 27-byte little-endian binary packet from np_agent."""
    if len(data) != PACKET_SIZE:
        return None
    pid, tid, state, priority, latency_us, ip = struct.unpack(PACKET_FMT, data)
    return TelemetryPacket(
        pid=pid,
        tid=tid,
        state=state,
        priority=priority,
        latency_us=latency_us,
        instruction_pointer=ip,
        state_name=QNX_STATES.get(state, f"STATE_{state}"),
        timestamp=time.time(),
    )


class AnomalyDetector:
    """Tracks per-thread states and detects real-time IPC latency anomalies."""

    def __init__(self, latency_threshold_us: int = LATENCY_THRESHOLD_US) -> None:
        self.latency_threshold_us = latency_threshold_us
        self.active_pid: int | None = None
        self.threads: dict[int, dict[str, Any]] = {}
        self.blocked_streaks: dict[int, int] = {}
        self.latency_window: deque[int] = deque(maxlen=60)
        self.history: deque[dict[str, Any]] = deque(maxlen=150)
        self.peak_latency_us: int = 0
        self.total_packets: int = 0
        self.anomaly_count: int = 0
        self.is_anomaly: bool = False
        self.last_anomaly: dict[str, Any] | None = None

    def reset(self) -> None:
        self.active_pid = None
        self.threads.clear()
        self.blocked_streaks.clear()
        self.latency_window.clear()
        self.history.clear()
        self.peak_latency_us = 0
        self.total_packets = 0
        self.anomaly_count = 0
        self.is_anomaly = False
        self.last_anomaly = None

    def _compute_zscore(self, value: int) -> float:
        if len(self.latency_window) < 10:
            return 0.0
        mean = sum(self.latency_window) / len(self.latency_window)
        var = sum((x - mean) ** 2 for x in self.latency_window) / len(self.latency_window)
        std = math.sqrt(var)
        return (value - mean) / std if std > 1e-6 else 0.0

    def ingest(self, pkt: TelemetryPacket) -> dict[str, Any]:
        # Auto-reset thread table & anomaly counters when switching target workloads (new PID)
        if self.active_pid is not None and pkt.pid != self.active_pid:
            self.threads.clear()
            self.blocked_streaks.clear()
            self.latency_window.clear()
            self.history.clear()
            self.peak_latency_us = 0
            self.anomaly_count = 0
            self.is_anomaly = False
            self.last_anomaly = None

        self.active_pid = pkt.pid
        self.total_packets += 1

        if pkt.state in BLOCKED_STATES:
            self.blocked_streaks[pkt.tid] = self.blocked_streaks.get(pkt.tid, 0) + 1
        else:
            self.blocked_streaks[pkt.tid] = 0

        z_score = self._compute_zscore(pkt.latency_us) if pkt.latency_us > 0 else 0.0
        if pkt.latency_us > 0:
            self.latency_window.append(pkt.latency_us)

        if pkt.latency_us > self.peak_latency_us:
            self.peak_latency_us = pkt.latency_us

        streak = self.blocked_streaks[pkt.tid]
        high_latency = pkt.latency_us > self.latency_threshold_us
        stat_spike = z_score > 4.0 and pkt.latency_us > 8_000
        stuck_blocked = pkt.state in BLOCKED_STATES and streak >= 2
        pkt_anomaly = high_latency or stat_spike or stuck_blocked

        self.threads[pkt.tid] = {
            "pid": pkt.pid,
            "tid": pkt.tid,
            "state": pkt.state,
            "state_name": pkt.state_name,
            "priority": pkt.priority,
            "latency_us": pkt.latency_us,
            "ip_hex": hex(pkt.instruction_pointer),
            "blocked_streak": streak,
            "updated_at": pkt.timestamp,
        }

        if pkt.latency_us > 0 or pkt.priority >= 20:
            self.history.append({
                "ts": round(pkt.timestamp, 3),
                "tid": pkt.tid,
                "priority": pkt.priority,
                "latency_us": pkt.latency_us,
                "is_anomaly": pkt_anomaly,
            })

        if pkt_anomaly:
            self.is_anomaly = True
            self.anomaly_count += 1
            reason = (
                f"IPC latency spike ({pkt.latency_us} us > {self.latency_threshold_us} us)"
                if high_latency
                else f"Thread stuck in {pkt.state_name} for {streak} polling cycles"
                if stuck_blocked
                else f"Statistical latency anomaly (Z-score={z_score:.2f}, {pkt.latency_us} us)"
            )
            # Keep the highest-latency / valid-IP anomaly snapshot for diagnosis
            if (
                self.last_anomaly is None
                or pkt.instruction_pointer != 0
                and pkt.latency_us >= self.last_anomaly.get("latency_us", 0)
            ):
                snapshot = asdict(pkt)
                snapshot["ip_hex"] = hex(pkt.instruction_pointer)
                snapshot["z_score"] = round(z_score, 2)
                snapshot["reason"] = reason
                snapshot["blocked_streak"] = streak
                self.last_anomaly = snapshot

        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        active_threads = [
            t for t in sorted(self.threads.values(), key=lambda x: x["tid"])
            if (now - t["updated_at"]) < 3.0
        ]
        return {
            "active_pid": self.active_pid,
            "total_packets": self.total_packets,
            "peak_latency_us": self.peak_latency_us,
            "is_anomaly": self.is_anomaly,
            "anomaly_count": self.anomaly_count,
            "last_anomaly": self.last_anomaly,
            "threads": active_threads,
            "history": list(self.history),
        }

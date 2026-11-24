# NeutrinoPulse v2.0

**AI-Assisted Real-Time Telemetry, Symbol-Resolved Tracing & IPC Anomaly Copilot for QNX Neutrino RTOS 8.0**

Built for **QNX Global Hackathon 2026** (*Developer Tooling & Host Tools*).

---

## Overview

**NeutrinoPulse** bridges QNX Neutrino RTOS 8.0 runtime telemetry with modern AI-assisted root-cause analysis inside a lightweight host web dashboard (and VS Code Simple Browser):

* **Zero-Jitter Core Isolation:** `np_agent` is pinned to **CPU Core 1** (`ThreadCtl(_NTO_TCTL_RUNMASK, 0x02)`), isolated from real-time workloads running on **CPU Core 0** (`0x01`).
* **Deterministic Binary Telemetry:** Streams fixed-size **27-byte packed structs** over non-blocking UDP (`10.0.2.2:9999` via QEMU SLIRP) to avoid JSON formatting overhead on the target.
* **Lock-Free IPC Latency Probe (`np_probe.h`):** Measures `MsgSend()` round-trip latency in microseconds using `ClockCycles()` and captures exact application return addresses via `__builtin_return_address(0)` (avoiding `libc.so` `??:0` trap).
* **Symbol Resolution & AI Code Diff:** Host backend maps target instruction pointers via `ntox86_64-addr2line.exe`, pairs client `MsgSend` and server `ChannelCreate` contexts, and uses **Gemini API** (with optional local LM Studio / deterministic fallback) to generate an actionable QNX fix diff.

---

## Project Structure

```text
.
├── target/                 # QNX 8.0 C target agent, probe header, & demo workloads
│   ├── np_protocol.h       # Shared 27-byte packed UDP contract & lock-free shm struct
│   ├── np_probe.h          # ClockCycles() + __builtin_return_address(0) probe
│   ├── np_agent.c          # Core-1 pinned /proc polling & UDP telemetry daemon
│   ├── brake_sim_core.h    # 3-thread Core-0 RT workload (Prio 10, 15, 25)
│   ├── brake_sim_buggy.c   # Priority Inversion via _NTO_CHF_FIXED_PRIORITY
│   ├── brake_sim_fixed.c   # Fixed via ChannelCreate(0) priority inheritance
│   └── Makefile            # Cross-compile with qcc -Vgcc_ntox86_64 -g -no-pie
├── backend/                # Host Python FastAPI + UDP receiver + addr2line + Gemini
│   ├── main.py             # FastAPI app, asyncio UDP server, WebSocket /ws/telemetry
│   ├── telemetry.py        # 27-byte decoder & Z-Score/blocking anomaly detector
│   ├── resolver.py         # ntox86_64-addr2line bridge & client/server pairing
│   ├── ai_copilot.py       # Hybrid Gemini / LM Studio / fallback diagnosis engine
│   └── test_milestone3.py  # Automated backend & addr2line verification tests
├── frontend/
│   └── index.html          # Real-time Chart.js, thread table, & Live Code Diff UI
└── run_qemu.bat            # QNX 8.0 x86_64 QEMU launcher (-smp 2 + SLIRP)
```

---

## Quick Start (Build & Run)

### 1. Build QNX 8.0 Target Binaries
```cmd
cd target
build.bat
```
*(Or source `qnxsdp-env.bat` / `qnxsdp-env.sh` and run `make all` using `qcc -Vgcc_ntox86_64`).*

### 2. Boot QNX 8.0 in QEMU (2 Cores)
```cmd
run_qemu.bat
```

### 3. Start Host Backend & Dashboard
```cmd
cd backend
pip install -r requirements.txt
copy .env.example .env
# Add your GEMINI_API_KEY inside .env
run_backend.bat
```
Open **`http://127.0.0.1:8090/`** in your browser or VS Code Simple Browser.

### 4. Deploy & Run on QNX Target
Transfer `np_agent`, `brake_sim_buggy`, and `brake_sim_fixed` to `/tmp/` on QEMU and run:
```sh
/tmp/np_agent 10.0.2.2 9999 &
/tmp/brake_sim_buggy
```
Click **Diagnose with AI** on the dashboard when the latency spike appears, then switch to `/tmp/brake_sim_fixed` to observe QNX priority inheritance restore real-time determinism.

---

## Evidence of QNX 8.0 Use

* **QNX Neutrino Microkernel IPC & Thread Control:** `ChannelCreate(_NTO_CHF_FIXED_PRIORITY)`, `ChannelCreate(0)`, `MsgSend()`, `MsgReceive()`, `MsgReply()`, `ThreadCtl(_NTO_TCTL_RUNMASK, ...)`, `ClockCycles()`, and `SYSPAGE_ENTRY(qtime)->cycles_per_sec` (`target/np_probe.h`, `target/brake_sim_core.h`, `target/brake_sim_buggy.c`).
* **QNX Procfs Instrumentation:** `DCMD_PROC_TIDSTATUS`, `DCMD_PROC_CURTHREAD`, `DCMD_PROC_GETGREG`, and `sys/states.h` thread states (`target/np_agent.c`, `backend/telemetry.py`).
* **QNX SDP 8.0 Toolchain:** Compiled with `qcc -Vgcc_ntox86_64` and symbol-resolved via `ntox86_64-addr2line.exe` (`target/Makefile`, `backend/resolver.py`).

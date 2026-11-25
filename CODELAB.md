# Codelab: Building a Zero-Jitter Real-Time Telemetry Agent & Fixing Priority Inversion in QNX Neutrino 8.0

**Estimated time:** 25 minutes  
**Target Platform:** QNX Neutrino RTOS 8.0 (`x86_64` on QEMU `-smp 2`) + Windows Host  
**Stack:** C (`qcc`), QNX Procfs (`devctl`), Shared Memory (`/np_probe_shm`), UDP Binary Telemetry, Python FastAPI, `ntox86_64-addr2line`, Gemini AI

---

## What You Will Learn

By the end of this codelab, you will know how to:
1. Isolate a QNX telemetry agent onto a dedicated CPU core (`CPU 1`) so monitoring never perturbs a real-time workload on `CPU 0`.
2. Measure `MsgSend()` round-trip latency and capture exact application caller addresses (`__builtin_return_address(0)`) without falling into `libc.so` (`??:0`).
3. Poll `/proc/<pid>/as` from user space using `DCMD_PROC_TIDSTATUS` and stream a compact **27-byte packed struct** over non-blocking UDP.
4. Reproduce an authentic QNX **Priority Inversion** bug using `ChannelCreate(_NTO_CHF_FIXED_PRIORITY)`, trace it to the exact C line with `ntox86_64-addr2line`, and fix it with `ChannelCreate(0)`.

---

## Prerequisites

- **QNX SDP 8.0** installed (`C:\qnx800` on Windows)
- **QEMU** (`x86_64`) booted with 2 CPU cores (`-smp 2`) and SLIRP networking (`10.0.2.2` -> Host)
- **Python 3.10+** on the host machine
- Optional: **Gemini API Key** in `backend/.env` (a deterministic rule-based fallback works offline too)

---

## Step 1: Define a Zero-Overhead 27-Byte Binary Contract

Formatting JSON strings on a real-time embedded target wastes CPU cycles and allocates heap memory. Instead, define a fixed-size `__attribute__((packed))` struct shared between the QNX target (`C`) and the host decoder (`Python`).

See [`target/np_protocol.h`](target/np_protocol.h):

```c
typedef struct __attribute__((packed)) {
    uint32_t pid;
    uint32_t tid;
    uint8_t  state;
    uint16_t priority;
    uint64_t latency_us;
    uint64_t instruction_pointer;
} telemetry_packet_t;
```

- **Total size:** `4 + 4 + 1 + 2 + 8 + 8 = 27 bytes`.
- **Host unpacking (`backend/telemetry.py`):** `struct.unpack("<IIBHQQ", data)`.

---

## Step 2: Capture `MsgSend()` Latency & Exact Caller IP (`np_probe.h`)

If an external profiler inspects a thread while it is blocked inside `MsgSend()`, the hardware `RIP` often points inside `libc.so` rather than your application `.c` file — causing `addr2line` to return `??:0`.

To solve this, [`target/np_probe.h`](target/np_probe.h) wraps `MsgSend()` with a `noinline` helper that records:
1. High-resolution timestamps via `ClockCycles()` and `SYSPAGE_ENTRY(qtime)->cycles_per_sec`.
2. The application call-site address via `__builtin_return_address(0)`.
3. A lock-free sequence counter (`seq`: odd = writing, even = stable) in POSIX shared memory (`/np_probe_shm`), so the real-time thread on Core 0 never acquires a mutex or blocks on IPC.

```c
static __attribute__((noinline)) uint64_t np_get_caller_ip(void) {
    return (uint64_t)(uintptr_t)__builtin_return_address(0);
}

static inline long np_msgsend_traced(int coid, const void *smsg, size_t sbytes,
                                     void *rmsg, size_t rbytes, uint64_t caller_ip) {
    uint64_t t_start = ClockCycles();
    /* Write start_cycles + caller_ip + in_flight=1 to lock-free /np_probe_shm slot */
    long rc = MsgSend(coid, smsg, sbytes, rmsg, rbytes);
    uint64_t t_end = ClockCycles();
    /* Convert (t_end - t_start) cycles to microseconds */
    return rc;
}

#define NP_MSGSEND(coid, smsg, sbytes, rmsg, rbytes) \
    np_msgsend_traced((coid), (smsg), (sbytes), (rmsg), (rbytes), np_get_caller_ip())
```

> **Important:** Compile with `-g -fno-pie -no-pie` in [`target/Makefile`](target/Makefile) so virtual addresses match ELF DWARF debug offsets cleanly in `ntox86_64-addr2line`.

---

## Step 3: Pin the Telemetry Agent to CPU Core 1 (`np_agent.c`)

In [`target/np_agent.c`](target/np_agent.c), the monitoring daemon isolates itself from the real-time workload:

1. **CPU Core Pinning:**
   ```c
   ThreadCtl(_NTO_TCTL_RUNMASK, (void *)(uintptr_t)0x02); /* CPU Core 1 */
   ```
2. **Non-blocking `/proc/<pid>/as` polling (100 ms cadence):**
   - Calls `devctl(proc_fd, DCMD_PROC_TIDSTATUS, &st, sizeof(st), NULL)` to read `tid`, `state` (`STATE_REPLY`, `RUNNING`, `READY`, `NANOSLEEP`, etc.), and `priority`.
   - Only falls back to `DCMD_PROC_GETGREG` if a thread remains blocked for more than 1 polling cycle without a probe slot.
   - Sends the 27-byte `telemetry_packet_t` over a non-blocking UDP socket (`O_NONBLOCK`) to `10.0.2.2:9999`.

---

## Step 4: Reproduce Priority Inversion in QNX 8.0

[`target/brake_sim_core.h`](target/brake_sim_core.h) spawns 3 real-time threads all pinned to **CPU Core 0** (`ThreadCtl(_NTO_TCTL_RUNMASK, 0x01)`):

| Thread | Priority | Role |
| :--- | :---: | :--- |
| `critical_brake_client` | **25** (High) | Sends safety-critical brake command via `NP_MSGSEND()` |
| `telemetry_logger_hog` | **15** (Medium) | Runs a 65 ms CPU-bound busy loop on Core 0 |
| `brake_actuator_server` | **10** (Low) | Receives message via `MsgReceive()`, simulates 2 ms work, calls `MsgReply()` |

### The Buggy Server ([`target/brake_sim_buggy.c`](target/brake_sim_buggy.c))
```c
int chid = ChannelCreate(_NTO_CHF_FIXED_PRIORITY);
```
Passing `_NTO_CHF_FIXED_PRIORITY` **disables QNX Neutrino's automatic Priority Inheritance**. When `critical_brake_client` (Prio 25) sends a message, `brake_actuator_server` stays at Priority 10, gets preempted by `telemetry_logger_hog` (Prio 15), and stalls the critical client for **~65,000 µs** (`STATE_REPLY`).

---

## Step 5: Run, Observe, and Diagnose with AI

1. **Build & launch QEMU:**
   ```cmd
   cd target && build.bat && cd ..
   run_qemu.bat
   ```
2. **Start the host backend & 4-Tab Fluent Dark dashboard:**
   ```cmd
   cd backend
   run_backend.bat
   ```
   Open **`http://127.0.0.1:8090/`**.
3. **Run the agent and buggy target inside QEMU:**
   ```sh
   /tmp/np_agent 10.0.2.2 9999 &
   /tmp/brake_sim_buggy
   ```
4. **Inspect across the 4 Dashboard Tabs:**
   - **Performance:** Watch `MsgSend()` latency spike past the `10,000 µs` budget line (`~65,533 µs`).
   - **Threads:** See `TID 3` highlighted in `STATE_REPLY` with `Caller IP = 0x4011e9`. Click **`Inspect →`**.
   - **Kernel Timeline:** Observe `TID 3` turning red (`STATE_REPLY`) in the exact slices where `TID 4` turns green (`RUNNING`) and `TID 2` stays blue (`READY`).
   - **AI Copilot & Code Fix:**
     - `ntox86_64-addr2line` resolves `0x4011e9` -> `critical_brake_client() @ brake_sim_core.h:110` and pairs it with `brake_sim_buggy.c:11`.
     - Click **`✦ Diagnose with AI`** to receive the root-cause explanation and diff patch:
       ```diff
       - int chid = ChannelCreate(_NTO_CHF_FIXED_PRIORITY);
       + int chid = ChannelCreate(0);
       ```

---

## Step 6: Verify the Fix (`brake_sim_fixed`)

Stop `brake_sim_buggy` (`Ctrl+C`) in QEMU and run the fixed binary ([`target/brake_sim_fixed.c`](target/brake_sim_fixed.c)):

```sh
/tmp/brake_sim_fixed
```

With `ChannelCreate(0)`, the QNX Neutrino microkernel dynamically boosts `brake_actuator_server` from Priority 10 to Priority 25 while processing `critical_brake_client`'s message. `telemetry_logger_hog` (Prio 15) can no longer preempt it, and latency drops back below **`10,000 µs` (`HEALTHY (REAL-TIME)`)**.

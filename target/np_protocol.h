#ifndef NP_PROTOCOL_H
#define NP_PROTOCOL_H

#include <stdint.h>

#define NP_UDP_PORT  9999
#define NP_HOST_IP   "10.0.2.2"
#define NP_SHM_NAME  "/np_probe_shm"
#define NP_SHM_MAGIC 0x4E505348U  /* 'NPSH' */
#define NP_MAX_SLOTS 32

/*
 * Kontrak biner UDP (27 bytes, little-endian, zero padding).
 * Harus cocok persis dengan Python struct.unpack('<IIBHQQ', data).
 */
typedef struct __attribute__((packed)) {
    uint32_t pid;
    uint32_t tid;
    uint8_t  state;
    uint16_t priority;
    uint64_t latency_us;
    uint64_t instruction_pointer;
} telemetry_packet_t;

/*
 * Slot shared-memory lock-free antara workload (Core 0) dan np_agent (Core 1).
 * Menghindari syscalls dan lock contention pada jalur kritis real-time.
 */
typedef struct {
    volatile uint32_t seq;          /* Ganjil = sedang ditulis, Genap = stabil */
    uint32_t tid;
    volatile uint8_t  in_flight;    /* 1 = sedang menunggu di MsgSend */
    uint64_t start_cycles;
    uint64_t last_latency_us;
    uint64_t caller_ip;             /* Return address di biner aplikasi (bukan libc.so) */
} np_probe_slot_t;

typedef struct {
    volatile uint32_t magic;
    volatile uint32_t target_pid;
    uint64_t cycles_per_sec;
    np_probe_slot_t slots[NP_MAX_SLOTS];
} np_shm_region_t;

#endif /* NP_PROTOCOL_H */

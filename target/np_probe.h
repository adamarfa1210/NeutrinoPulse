#ifndef NP_PROBE_H
#define NP_PROBE_H

#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/neutrino.h>
#include <sys/syspage.h>
#include <unistd.h>

#include "np_protocol.h"

static np_shm_region_t *g_np_shm = NULL;

/*
 * Fungsi noinline agar __builtin_return_address(0) menangkap alamat instruksi
 * tepat di baris kode aplikasi pemanggil (menghindari RIP terjebak di libc.so).
 */
static __attribute__((noinline)) uint64_t np_get_caller_ip(void) {
    return (uint64_t)(uintptr_t)__builtin_return_address(0);
}

static inline int np_probe_init(void) {
    int fd = shm_open(NP_SHM_NAME, O_RDWR | O_CREAT, 0666);
    if (fd < 0) {
        perror("np_probe: shm_open");
        return -1;
    }
    if (ftruncate(fd, (off_t)sizeof(np_shm_region_t)) < 0) {
        perror("np_probe: ftruncate");
        close(fd);
        return -1;
    }
    void *ptr = mmap(NULL, sizeof(np_shm_region_t), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (ptr == MAP_FAILED) {
        perror("np_probe: mmap");
        return -1;
    }

    g_np_shm = (np_shm_region_t *)ptr;
    memset((void *)g_np_shm, 0, sizeof(np_shm_region_t));
    g_np_shm->cycles_per_sec = SYSPAGE_ENTRY(qtime)->cycles_per_sec;
    g_np_shm->target_pid = (uint32_t)getpid();
    __sync_synchronize();
    g_np_shm->magic = NP_SHM_MAGIC;
    return 0;
}

static inline void np_probe_cleanup(void) {
    if (g_np_shm) {
        if (g_np_shm->target_pid == (uint32_t)getpid()) {
            g_np_shm->target_pid = 0;
        }
        munmap((void *)g_np_shm, sizeof(np_shm_region_t));
        g_np_shm = NULL;
    }
}

static inline long np_msgsend_traced(int coid, const void *smsg, size_t sbytes,
                                     void *rmsg, size_t rbytes, uint64_t caller_ip) {
    uint32_t tid = (uint32_t)pthread_self();
    np_probe_slot_t *slot = NULL;
    uint64_t t_start = ClockCycles();

    if (g_np_shm && g_np_shm->cycles_per_sec > 0) {
        slot = &g_np_shm->slots[tid % NP_MAX_SLOTS];
        slot->seq++;
        __sync_synchronize();
        slot->tid = tid;
        slot->caller_ip = caller_ip;
        slot->start_cycles = t_start;
        slot->in_flight = 1;
        __sync_synchronize();
        slot->seq++;
    }

    long rc = MsgSend(coid, smsg, sbytes, rmsg, rbytes);
    uint64_t t_end = ClockCycles();

    if (slot) {
        uint64_t delta_us = ((t_end - t_start) * 1000000ULL) / g_np_shm->cycles_per_sec;
        slot->seq++;
        __sync_synchronize();
        slot->last_latency_us = delta_us;
        slot->in_flight = 0;
        __sync_synchronize();
        slot->seq++;
    }
    return rc;
}

/* Macro wrapper untuk mencatat alamat pemanggil langsung di baris MsgSend */
#define NP_MSGSEND(coid, smsg, sbytes, rmsg, rbytes) \
    np_msgsend_traced((coid), (smsg), (sbytes), (rmsg), (rbytes), np_get_caller_ip())

#endif /* NP_PROBE_H */

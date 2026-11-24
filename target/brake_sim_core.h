#ifndef BRAKE_SIM_CORE_H
#define BRAKE_SIM_CORE_H

#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/netmgr.h>
#include <sys/neutrino.h>
#include <sys/syspage.h>
#include <unistd.h>

#include "np_probe.h"

static volatile sig_atomic_t g_running = 1;
static int g_chid = -1;

static void handle_sig(int sig) {
    (void)sig;
    g_running = 0;
}

static void pin_to_core(uintptr_t core_mask) {
    if (ThreadCtl(_NTO_TCTL_RUNMASK, (void *)core_mask) == -1) {
        perror("ThreadCtl(_NTO_TCTL_RUNMASK)");
    }
}

static void busy_wait_us(uint64_t duration_us) {
    uint64_t cps = SYSPAGE_ENTRY(qtime)->cycles_per_sec;
    uint64_t target_cycles = (duration_us * cps) / 1000000ULL;
    uint64_t start = ClockCycles();
    while ((ClockCycles() - start) < target_cycles && g_running) {
        /* Simulasi beban CPU deterministik */
    }
}

static int spawn_rt_thread(pthread_t *tid, int priority, void *(*fn)(void *), void *arg) {
    pthread_attr_t attr;
    struct sched_param param;
    pthread_attr_init(&attr);
    pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    pthread_attr_setschedpolicy(&attr, SCHED_RR);
    param.sched_priority = priority;
    pthread_attr_setschedparam(&attr, &param);
    int rc = pthread_create(tid, &attr, fn, arg);
    pthread_attr_destroy(&attr);
    return rc;
}

typedef struct {
    uint32_t seq;
    uint32_t brake_force;
} brake_msg_t;

/* Thread 1: Server aktuator rem (Prioritas Rendah: 10) di Core 0 */
static void *brake_actuator_server(void *arg) {
    (void)arg;
    pin_to_core(0x01);

    brake_msg_t req, resp;
    while (g_running) {
        int rcvid = MsgReceive(g_chid, &req, sizeof(req), NULL);
        if (rcvid == -1) {
            if (errno == EINTR) continue;
            break;
        }
        /* Simulasi kerja aktuator 2ms */
        busy_wait_us(2000);
        resp.seq = req.seq;
        resp.brake_force = req.brake_force;
        MsgReply(rcvid, EOK, &resp, sizeof(resp));
    }
    return NULL;
}

/* Thread 2: Background CPU Hog / Logger (Prioritas Menengah: 15) di Core 0 */
static void *telemetry_logger_hog(void *arg) {
    (void)arg;
    pin_to_core(0x01);

    while (g_running) {
        /* Mengambil CPU Core 0 selama 65ms setiap siklus */
        busy_wait_us(65000);
        usleep(35000);
    }
    return NULL;
}

/* Thread 3: Klien Kritis Pengereman (Prioritas Tinggi: 25) di Core 0 */
static void *critical_brake_client(void *arg) {
    (void)arg;
    pin_to_core(0x01);

    int coid = ConnectAttach(ND_LOCAL_NODE, 0, g_chid, _NTO_SIDE_CHANNEL, 0);
    if (coid == -1) {
        perror("ConnectAttach");
        return NULL;
    }

    brake_msg_t req = {0, 100};
    brake_msg_t resp = {0, 0};

    while (g_running) {
        req.seq++;
        uint64_t t0 = ClockCycles();
        long rc = NP_MSGSEND(coid, &req, sizeof(req), &resp, sizeof(resp));
        uint64_t t1 = ClockCycles();

        if (rc == 0) {
            uint64_t us = ((t1 - t0) * 1000000ULL) / SYSPAGE_ENTRY(qtime)->cycles_per_sec;
            if (req.seq % 10 == 0 || us > 10000) {
                printf("[BrakeClient Prio 25] seq=%u latency=%llu us %s\n",
                       req.seq, (unsigned long long)us,
                       (us > 10000) ? "<<< ANOMALY: PRIORITY INVERSION!" : "(OK)");
            }
        }
        usleep(40000);
    }

    ConnectDetach(coid);
    return NULL;
}

static int run_brake_simulation(int chid, const char *mode_label) {
    if (chid == -1) {
        perror("ChannelCreate");
        return 1;
    }
    g_chid = chid;

    signal(SIGINT, handle_sig);
    signal(SIGTERM, handle_sig);

    if (np_probe_init() != 0) {
        fprintf(stderr, "Warning: np_probe_init failed, running without shm probe.\n");
    }

    printf("=====================================================\n");
    printf("  NeutrinoPulse Demo Workload: %s\n", mode_label);
    printf("  PID: %d | Pinned to CPU Core 0 (0x01)\n", getpid());
    printf("  Threads: Server(10) | LoggerHog(15) | BrakeClient(25)\n");
    printf("=====================================================\n");

    pthread_t t_srv, t_hog, t_cli;
    if (spawn_rt_thread(&t_srv, 10, brake_actuator_server, NULL) != 0) {
        fprintf(stderr, "Error: gagal membuat thread server (jalankan sebagai root).\n");
        np_probe_cleanup();
        return 1;
    }
    usleep(20000); /* Beri waktu server masuk ke MsgReceive sebelum hog jalan */
    if (spawn_rt_thread(&t_cli, 25, critical_brake_client, NULL) != 0 ||
        spawn_rt_thread(&t_hog, 15, telemetry_logger_hog, NULL) != 0) {
        fprintf(stderr, "Error: gagal membuat RT threads (jalankan sebagai root).\n");
        np_probe_cleanup();
        return 1;
    }

    while (g_running) {
        sleep(1);
    }

    ChannelDestroy(g_chid);
    pthread_join(t_cli, NULL);
    pthread_join(t_hog, NULL);
    pthread_join(t_srv, NULL);
    np_probe_cleanup();
    printf("\nSimulation stopped cleanly.\n");
    return 0;
}

#endif /* BRAKE_SIM_CORE_H */

#define _DEBUG_TARGET_X86_64
#include <arpa/inet.h>
#include <devctl.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/neutrino.h>
#include <sys/procfs.h>
#include <sys/socket.h>
#include <sys/states.h>
#include <sys/syspage.h>
#include <unistd.h>

#include "np_protocol.h"

static volatile sig_atomic_t g_running = 1;

static void handle_sig(int sig) {
    (void)sig;
    g_running = 0;
}

static const np_shm_region_t *attach_probe_shm(void) {
    int fd = shm_open(NP_SHM_NAME, O_RDONLY, 0);
    if (fd < 0) return NULL;
    void *ptr = mmap(NULL, sizeof(np_shm_region_t), PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (ptr == MAP_FAILED) return NULL;
    const np_shm_region_t *shm = (const np_shm_region_t *)ptr;
    if (shm->magic != NP_SHM_MAGIC) {
        munmap(ptr, sizeof(np_shm_region_t));
        return NULL;
    }
    return shm;
}

static int read_slot_stable(const np_shm_region_t *shm, uint32_t tid, np_probe_slot_t *out) {
    if (!shm) return 0;
    const np_probe_slot_t *slot = &shm->slots[tid % NP_MAX_SLOTS];
    for (int retry = 0; retry < 4; retry++) {
        uint32_t s1 = slot->seq;
        if (s1 & 1U) continue;
        __sync_synchronize();
        np_probe_slot_t copy = *slot;
        __sync_synchronize();
        uint32_t s2 = slot->seq;
        if (s1 == s2 && copy.tid == tid) {
            *out = copy;
            return 1;
        }
    }
    return 0;
}

static uint64_t get_fallback_ip(int proc_fd, pthread_t tid) {
    if (devctl(proc_fd, DCMD_PROC_CURTHREAD, &tid, sizeof(tid), NULL) != EOK) {
        return 0;
    }
    procfs_greg greg;
    memset(&greg, 0, sizeof(greg));
    if (devctl(proc_fd, DCMD_PROC_GETGREG, &greg, sizeof(greg), NULL) != EOK) {
        return 0;
    }
    return greg.x86_64.rip;
}

static const char *path_basename(const char *path) {
    const char *slash = strrchr(path, '/');
    return slash ? slash + 1 : path;
}

static void fetch_exe_basename(int proc_fd, char *out, size_t out_sz) {
    struct {
        procfs_debuginfo info;
        char path[256];
    } dbg;
    out[0] = '\0';
    memset(&dbg, 0, sizeof(dbg));
    if (devctl(proc_fd, DCMD_PROC_MAPDEBUG_BASE, &dbg, sizeof(dbg), NULL) == EOK && dbg.info.path[0]) {
        snprintf(out, out_sz, "%s", path_basename(dbg.info.path));
    }
}

/* Translate ASLR virtual IP back to ELF link offset for PIE binaries */
static uint64_t normalize_pie_ip(int proc_fd, const char *exe_base, uint64_t raw_ip) {
    if (proc_fd < 0 || raw_ip == 0 || !exe_base || !exe_base[0]) return raw_ip;
    struct {
        procfs_debuginfo info;
        char path[256];
    } dbg;
    memset(&dbg, 0, sizeof(dbg));
    dbg.info.vaddr = raw_ip;
    if (devctl(proc_fd, DCMD_PROC_MAPDEBUG, &dbg, sizeof(dbg), NULL) == EOK &&
        dbg.info.path[0] &&
        strcmp(path_basename(dbg.info.path), exe_base) == 0) {
        return dbg.info.vaddr;
    }
    return raw_ip;
}

int main(int argc, char *argv[]) {
    const char *host_ip = (argc >= 2) ? argv[1] : NP_HOST_IP;
    int udp_port = (argc >= 3) ? atoi(argv[2]) : NP_UDP_PORT;
    pid_t manual_pid = (argc >= 4) ? (pid_t)atoi(argv[3]) : 0;

    signal(SIGINT, handle_sig);
    signal(SIGTERM, handle_sig);

    /* Isolasi np_agent pada CPU Core 1 (0x02) agar tidak mengganggu Core 0 */
    if (ThreadCtl(_NTO_TCTL_RUNMASK, (void *)(uintptr_t)0x02) == -1) {
        perror("np_agent: ThreadCtl(_NTO_TCTL_RUNMASK 0x02)");
    }

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        perror("np_agent: socket");
        return 1;
    }
    fcntl(sock, F_SETFL, O_NONBLOCK);

    struct sockaddr_in dest;
    memset(&dest, 0, sizeof(dest));
    dest.sin_family = AF_INET;
    dest.sin_port = htons((uint16_t)udp_port);
    if (inet_pton(AF_INET, host_ip, &dest.sin_addr) != 1) {
        fprintf(stderr, "Invalid host IP: %s\n", host_ip);
        close(sock);
        return 1;
    }

    printf("=====================================================\n");
    printf("  NeutrinoPulse Agent (np_agent) Active\n");
    printf("  Pinned Core : CPU 1 (0x02)\n");
    printf("  UDP Target  : %s:%d (non-blocking, 27-byte struct)\n", host_ip, udp_port);
    printf("  Packet Size : %zu bytes\n", sizeof(telemetry_packet_t));
    printf("=====================================================\n");

    const np_shm_region_t *shm = NULL;
    pid_t active_pid = manual_pid;
    int proc_fd = -1;
    char exe_base[128] = {0};
    uint8_t blocked_streak[NP_MAX_SLOTS] = {0};
    uint64_t total_sent = 0;

    while (g_running) {
        if (!shm) {
            shm = attach_probe_shm();
        }
        pid_t desired_pid = manual_pid ? manual_pid : (shm && shm->magic == NP_SHM_MAGIC ? (pid_t)shm->target_pid : 0);

        if (desired_pid <= 0) {
            if (proc_fd >= 0) {
                close(proc_fd);
                proc_fd = -1;
                active_pid = 0;
                exe_base[0] = '\0';
            }
            usleep(100000);
            continue;
        }

        if (desired_pid != active_pid || proc_fd < 0) {
            if (proc_fd >= 0) close(proc_fd);
            char path[64];
            snprintf(path, sizeof(path), "/proc/%d/as", (int)desired_pid);
            proc_fd = open(path, O_RDONLY);
            if (proc_fd < 0) {
                if (shm && !manual_pid) {
                    munmap((void *)shm, sizeof(np_shm_region_t));
                    shm = NULL;
                }
                usleep(100000);
                continue;
            }
            active_pid = desired_pid;
            memset(blocked_streak, 0, sizeof(blocked_streak));
            fetch_exe_basename(proc_fd, exe_base, sizeof(exe_base));
            printf("[np_agent] Attached to target PID %d (/proc/%d/as, exe=%s)\n",
                   (int)active_pid, (int)active_pid, exe_base[0] ? exe_base : "unknown");
        }

        uint64_t now_cycles = ClockCycles();
        pthread_t tid = 1;
        int found_any = 0;
        int shm_matches_pid = (shm != NULL && shm->magic == NP_SHM_MAGIC &&
                               shm->target_pid == (uint32_t)active_pid);

        while (g_running) {
            procfs_status st;
            memset(&st, 0, sizeof(st));
            st.tid = tid;
            if (devctl(proc_fd, DCMD_PROC_TIDSTATUS, &st, sizeof(st), NULL) != EOK) {
                break;
            }
            found_any = 1;

            uint32_t slot_idx = (uint32_t)st.tid % NP_MAX_SLOTS;
            if (st.state == STATE_REPLY || st.state == STATE_MUTEX) {
                if (blocked_streak[slot_idx] < 255) blocked_streak[slot_idx]++;
            } else {
                blocked_streak[slot_idx] = 0;
            }

            telemetry_packet_t pkt;
            memset(&pkt, 0, sizeof(pkt));
            pkt.pid = (uint32_t)active_pid;
            pkt.tid = (uint32_t)st.tid;
            pkt.state = (uint8_t)st.state;
            pkt.priority = (uint16_t)st.priority;
            pkt.instruction_pointer = st.ip;

            np_probe_slot_t slot;
            if (shm_matches_pid && read_slot_stable(shm, (uint32_t)st.tid, &slot)) {
                if (slot.in_flight && shm->cycles_per_sec > 0 && now_cycles >= slot.start_cycles) {
                    pkt.latency_us = ((now_cycles - slot.start_cycles) * 1000000ULL) / shm->cycles_per_sec;
                } else {
                    pkt.latency_us = slot.last_latency_us;
                }
                if (slot.caller_ip != 0) {
                    pkt.instruction_pointer = slot.caller_ip;
                }
            } else if (blocked_streak[slot_idx] > 1) {
                /* Panggil DCMD_PROC_GETGREG hanya secara kondisional saat macet >1 siklus */
                uint64_t rip = get_fallback_ip(proc_fd, st.tid);
                if (rip != 0) pkt.instruction_pointer = rip;
            }
            pkt.instruction_pointer = normalize_pie_ip(proc_fd, exe_base, pkt.instruction_pointer);

            ssize_t n = sendto(sock, &pkt, sizeof(pkt), 0,
                               (struct sockaddr *)&dest, sizeof(dest));
            if (n == (ssize_t)sizeof(pkt)) {
                total_sent++;
                if (total_sent % 50 == 1) {
                    printf("[np_agent] Streamed %llu packets | tid=%u state=%u prio=%u lat=%llu us ip=0x%llx\n",
                           (unsigned long long)total_sent, pkt.tid, pkt.state, pkt.priority,
                           (unsigned long long)pkt.latency_us, (unsigned long long)pkt.instruction_pointer);
                }
            }

            tid = st.tid + 1;
        }

        if (!found_any) {
            close(proc_fd);
            proc_fd = -1;
            active_pid = 0;
            if (shm && !manual_pid) {
                munmap((void *)shm, sizeof(np_shm_region_t));
                shm = NULL;
            }
        }

        usleep(100000); /* 100ms polling cadence */
    }

    if (proc_fd >= 0) close(proc_fd);
    if (shm) munmap((void *)shm, sizeof(np_shm_region_t));
    close(sock);
    printf("\n[np_agent] Stopped cleanly. Total packets sent: %llu\n",
           (unsigned long long)total_sent);
    return 0;
}

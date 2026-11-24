#include "brake_sim_core.h"

/*
 * BUGGY VERSION:
 * Channel dibuat dengan flag _NTO_CHF_FIXED_PRIORITY yang MEMATIKAN fitur
 * Priority Inheritance bawaan kernel QNX Neutrino. Akibatnya, saat Server
 * (Prio 10) memproses pesan Klien Kritis (Prio 25), Server tertahan oleh
 * LoggerHog (Prio 15) -> Priority Inversion terjadi!
 */
int main(void) {
    int chid = ChannelCreate(_NTO_CHF_FIXED_PRIORITY);
    return run_brake_simulation(chid, "BUGGY (_NTO_CHF_FIXED_PRIORITY)");
}

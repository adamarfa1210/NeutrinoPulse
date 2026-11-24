#include "brake_sim_core.h"

/*
 * FIXED VERSION:
 * Menghapus _NTO_CHF_FIXED_PRIORITY (menggunakan ChannelCreate(0)).
 * Kernel QNX Neutrino otomatis menaikkan prioritas Server (10 -> 25)
 * saat menerima pesan dari Klien Kritis (Priority Inheritance aktif).
 */
int main(void) {
    int chid = ChannelCreate(0);
    return run_brake_simulation(chid, "FIXED (Priority Inheritance Enabled)");
}

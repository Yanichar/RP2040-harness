/*
 * RP2040 Harness — firmware entry point.
 *
 * Raspberry Pi Pico W (RP2040), Pico SDK 2.3.1.
 * USB raw-vendor device exposing 24 individually controllable digital lines.
 * See docs/protocol.md for the wire protocol and TASK.md for the requirements.
 *
 * SPDX-License-Identifier: MIT
 */

#include <stdio.h>

#include "pico/stdlib.h"
#include "tusb.h"

#include "gpio_ctrl.h"
#include "harness_config.h"
#include "protocol.h"
#include "usb_vendor.h"

/* -------------------------------------------------------------------------- */
/* TinyUSB callbacks                                                          */
/* -------------------------------------------------------------------------- */

void tud_mount_cb(void) {
    printf("[harness] USB mounted\n");
}

void tud_umount_cb(void) {
    /*
     * TASK.md §3.1/§3.4: stop everything and return every line to INPUT with no
     * pulls so a dead host cannot leave lines driving.
     *
     * NOTE: the RP2040 builds the device stack with FORCE_VBUS_DETECT=1, which
     * compiles DCD_EVENT_UNPLUGGED (and therefore this callback) out entirely —
     * see tud_suspend_cb() below for the path that actually fires on this board.
     */
    printf("[harness] USB unmounted -> safe state\n");
    harness_usb_vendor_reset_state();
    harness_gpio_safe_state();
}

void tud_suspend_cb(bool remote_wakeup_en) {
    (void)remote_wakeup_en;

    /*
     * This is the link-loss path on real hardware.
     *
     * The RP2040 has no VBUS detect and TinyUSB is built with
     * FORCE_VBUS_DETECT=1, so the DCD cannot distinguish "host unplugged" from
     * "bus suspended" and never raises DCD_EVENT_UNPLUGGED: tud_umount_cb() is
     * dead code on this board. A suspend is therefore the ONLY signal that the
     * host is gone, and TASK.md §3.4 requires the safe state on link loss.
     *
     * Consequences, deliberate and documented in docs/protocol.md §5.2:
     *  - a suspend drops every line to INPUT with no pull;
     *  - the configuration is NOT restored on resume — the host must re-apply it.
     *
     * A bus only suspends when the host genuinely stops driving it (>= 3 ms of
     * complete bus idle, e.g. unplug, host sleep, port power-down); SOF packets
     * keep arriving once per millisecond while a session is live, so this does
     * not fire during normal interactive use or while streaming.
     */
    printf("[harness] USB suspend -> safe state\n");
    harness_usb_vendor_reset_state();
    harness_gpio_safe_state();
}

void tud_resume_cb(void) {
}

/* -------------------------------------------------------------------------- */
/* Main                                                                       */
/* -------------------------------------------------------------------------- */

int main(void) {
    /*
     * Bring every harness line to the reset state BEFORE USB is initialised, so
     * that even a host that enumerates instantly sees inputs with no pulls
     * (TASK.md §3.1, docs/protocol.md §5.1).
     */
    harness_gpio_safe_state();
    harness_protocol_init();

    /* Debug console on UART0 (GP0/GP1) — those pins are out of the pin map. */
    stdio_init_all();

    printf("\n[harness] RP2040 Harness firmware %u.%u.%u (protocol %u.%u)\n",
           HARNESS_FW_VERSION_MAJOR, HARNESS_FW_VERSION_MINOR,
           HARNESS_FW_VERSION_PATCH, HARNESS_PROTO_VERSION_MAJOR,
           HARNESS_PROTO_VERSION_MINOR);
    printf("[harness] %u lines, USB %04X:%04X\n", HARNESS_PIN_COUNT,
           (unsigned)HARNESS_USB_VID, (unsigned)HARNESS_USB_PID);

    tusb_init();

    for (;;) {
        tud_task();
        harness_usb_vendor_task();
    }

    return 0;
}

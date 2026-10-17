/*
 * RP2040 Harness — custom TinyUSB vendor class driver (interface 0).
 *
 * The SDK's `vendor_device.c` class is deliberately NOT used: it hard-codes
 * `tud_vendor_*` and would collide with the MS OS 2.0 control-request handler
 * that the Pico reset interface also wants to own. Instead this file registers
 * a small class driver through `usbd_app_driver_get_cb`, alongside the SDK's
 * reset-interface driver.
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef HARNESS_USB_VENDOR_H
#define HARNESS_USB_VENDOR_H

#include <stdbool.h>
#include <stdint.h>

#include "protocol.h"

/*
 * Vendor control requests (bmRequestType = 0xC0/0x40, recipient = device).
 *
 * These are diagnostics, not part of the bulk wire protocol. They exist because
 * a vendor control request is the ONLY channel that keeps working when the bulk
 * endpoints are misbehaving, so they are how a wedged device is inspected and
 * recovered without unplugging it.
 *
 * bRequest 0x42 GET_STATE  IN,  returns harness_usb_state_t
 * bRequest 0x43 REARM      OUT, re-arms the OUT endpoint, no data
 */
#define HARNESS_CTRL_GET_STATE 0x42u
#define HARNESS_CTRL_REARM     0x43u

#define HARNESS_USB_STATE_MAGIC   0xA5u
#define HARNESS_USB_STATE_VERSION 0x01u

/* String returned by HARNESS_CTRL_GET_STATE, so a host can sanity-check it. */
typedef struct HARNESS_PACKED {
    uint8_t magic;
    uint8_t version;
    uint16_t struct_len;

    /* TinyUSB / driver state */
    uint8_t tud_ready;
    uint8_t tud_mounted;
    uint8_t itf_num;
    uint8_t ep_in;
    uint8_t ep_out;
    uint8_t out_armed;
    uint8_t out_hw_busy;
    uint8_t tx_busy;
    uint8_t tx_in_zlp;
    uint8_t rx_have;
    uint8_t last_result;
    uint8_t reserved0;

    /* Lifecycle counters */
    uint32_t init_count;
    uint32_t open_count;
    uint32_t reset_count;
    uint32_t out_cb_count;
    uint32_t in_cb_count;
    uint32_t out_rearm_ok;
    uint32_t out_rearm_fail;
    uint32_t in_start_ok;
    uint32_t in_start_fail;
    uint32_t in_dropped;
    uint32_t rx_bytes;
    uint32_t frames_ok;
    uint32_t frames_err;
    uint32_t crc_errors;
    uint32_t uptime_ms;
} harness_usb_state_t;

/* True once interface 0 has been opened by the host. */
bool harness_usb_vendor_mounted(void);

/* Main-loop hook: self-heals the OUT endpoint and drains pending frames. */
void harness_usb_vendor_task(void);

/* Drop any partially-received frame and forget the endpoints. */
void harness_usb_vendor_reset_state(void);

/* Fill in the diagnostic snapshot (HARNESS_CTRL_GET_STATE). */
void harness_usb_vendor_state(harness_usb_state_t *out);

/* Re-arm the OUT endpoint if it is not armed (HARNESS_CTRL_REARM). */
void harness_usb_vendor_rearm(void);

#endif /* HARNESS_USB_VENDOR_H */

/*
 * RP2040 Harness — custom TinyUSB vendor class driver (interface 0).
 *
 * SPDX-License-Identifier: MIT
 */

#include "usb_vendor.h"

#include <string.h>

#include "device/usbd.h"
#include "device/usbd_pvt.h"
#include "pico/time.h"
#include "pico/usb_reset.h"
#include "tusb.h"

#include "harness_config.h"
#include "protocol.h"

/* -------------------------------------------------------------------------- */
/* State                                                                      */
/* -------------------------------------------------------------------------- */

static uint8_t s_itf_num = 0xFFu;
static uint8_t s_ep_out = 0u;
static uint8_t s_ep_in = 0u;

/*
 * The OUT endpoint is armed with room for a WHOLE maximum-size frame.
 *
 * Arming with a single 64-byte packet makes a 1024-byte STREAM request arrive as
 * 16 separate USB transfers, each needing an interrupt, a tud_task() pass and a
 * re-arm. Measured on the bench: ~4.2 ms per request, ~59k updates/s. Arming with
 * the whole frame lets a request arrive as ONE transfer.
 *
 * A transfer then only ends on a short packet, so the host appends a
 * zero-length packet when the frame length is an exact multiple of the packet
 * size (docs/protocol.md §2.5).
 */
#define HARNESS_RX_BUF_SIZE (HARNESS_MAX_FRAME + HARNESS_EP_SIZE)

static uint8_t s_rx_ep_buf[HARNESS_RX_BUF_SIZE];
static uint8_t s_tx_buf[HARNESS_MAX_FRAME];
static uint8_t s_reply_buf[HARNESS_MAX_FRAME];

static harness_rx_t s_rx;

/*
 * Reply state machine. At most one reply is ever in flight:
 *   s_tx_busy    an IN transfer is in progress
 *   s_tx_in_zlp  the in-flight transfer is the trailing zero-length packet
 *
 * A request is only consumed from the accumulator once its reply has been
 * handed to the USB stack, so a host that pipelines requests can never lose a
 * reply — the extra frames simply wait in `s_rx`.
 */
static volatile bool s_tx_busy;
static volatile bool s_tx_in_zlp;

/* Diagnostics (see harness_usb_state_t). */
static uint32_t s_init_count;
static uint32_t s_open_count;
static uint32_t s_reset_count;
static uint32_t s_out_cb_count;
static uint32_t s_in_cb_count;
static uint32_t s_out_rearm_ok;
static uint32_t s_out_rearm_fail;
static uint32_t s_in_start_ok;
static uint32_t s_in_start_fail;
static uint32_t s_in_dropped;
static uint32_t s_rx_bytes;
static uint8_t s_last_result;

bool harness_usb_vendor_mounted(void) {
    return s_itf_num != 0xFFu;
}

void harness_usb_vendor_reset_state(void) {
    s_itf_num = 0xFFu;
    s_ep_out = 0u;
    s_ep_in = 0u;
    s_tx_busy = false;
    s_tx_in_zlp = false;
    harness_rx_init(&s_rx);
}

/* -------------------------------------------------------------------------- */
/* Endpoint arming                                                            */
/* -------------------------------------------------------------------------- */

/*
 * Arm the OUT endpoint if the stack does not already have a transfer pending.
 *
 * NOTE: the state of a transfer is taken from the stack (`usbd_edpt_busy`), NOT
 * from a shadow flag. This matters: Windows issues CLEAR_FEATURE(ENDPOINT_HALT)
 * when a WinUSB handle is closed, and TinyUSB's handler calls
 * `usbd_edpt_clear_stall()`, which clears the busy flag and discards the pending
 * transfer WITHOUT ever invoking our xfer callback. A shadow flag would then
 * claim the endpoint is armed while the controller NAKs every packet, and the
 * device would be deaf for good — which is exactly what happened in bring-up.
 *
 * Safe to call from the USB callback and from the main loop; the main loop
 * retry is what makes the device self-healing.
 */
static bool out_arm(void) {
    if (s_ep_out == 0u) {
        return false;
    }
    if (usbd_edpt_busy(0u, s_ep_out)) {
        return true;
    }
    if (usbd_edpt_xfer(0u, s_ep_out, s_rx_ep_buf, sizeof(s_rx_ep_buf))) {
        s_out_rearm_ok++;
        return true;
    }
    s_out_rearm_fail++;
    return false;
}

/* -------------------------------------------------------------------------- */
/* Frame handling                                                             */
/* -------------------------------------------------------------------------- */

static void start_tx(const uint8_t *frame, uint16_t len) {
    if (len == 0u || s_ep_in == 0u) {
        return;
    }

    memcpy(s_tx_buf, frame, len);
    s_tx_busy = true;
    if (usbd_edpt_xfer(0u, s_ep_in, s_tx_buf, len)) {
        s_in_start_ok++;
        return;
    }
    s_in_start_fail++;
    s_tx_busy = false;
}

/*
 * Drain every complete request whose reply we can start right now.
 * Called from the USB callbacks and from the main loop.
 */
static void process_frames(void) {
    while (!s_tx_busy) {
        uint16_t frame_len = 0;
        const harness_rx_result_t res = harness_rx_poll(&s_rx, &frame_len);

        if (res == HARNESS_RX_NONE) {
            return;
        }

        if (res == HARNESS_RX_CORRUPT) {
            /* harness_rx_poll already discarded the bad bytes and resynced. */
            uint16_t rep_len = 0;
            harness_protocol_error_reply(s_reply_buf, s_rx.last_error, 0u, &rep_len);
            start_tx(s_reply_buf, rep_len);
            continue;
        }

        uint16_t rep_len = 0;
        harness_protocol_handle(s_rx.buf, frame_len, s_reply_buf, sizeof(s_reply_buf),
                                &rep_len);
        harness_rx_consume(&s_rx, frame_len);

        if (rep_len != 0u) {
            start_tx(s_reply_buf, rep_len);
        }
    }
}

void harness_usb_vendor_task(void) {
    /*
     * Self-healing, both directions. A vendor control request is not the only
     * way back from a dropped transfer.
     */
    out_arm();

    /*
     * Symmetric healing for the IN path: CLEAR_FEATURE(ENDPOINT_HALT) on the IN
     * pipe drops a pending reply exactly the same way it drops a pending OUT
     * transfer. If we still believe a reply is in flight while the stack says
     * the endpoint is idle, release the reply slot instead of blocking on a
     * callback that will never arrive.
     */
    if (s_tx_busy && !s_tx_in_zlp && s_ep_in != 0u && !usbd_edpt_busy(0u, s_ep_in)) {
        s_tx_busy = false;
        s_in_dropped++;
    }

    /* A pipelined request may have been waiting for the reply slot. */
    process_frames();
}

void harness_usb_vendor_rearm(void) {
    out_arm();
}

void harness_usb_vendor_state(harness_usb_state_t *out) {
    memset(out, 0, sizeof(*out));
    out->magic = HARNESS_USB_STATE_MAGIC;
    out->version = HARNESS_USB_STATE_VERSION;
    out->struct_len = (uint16_t)sizeof(*out);
    out->tud_ready = tud_ready() ? 1u : 0u;
    out->tud_mounted = tud_mounted() ? 1u : 0u;
    out->itf_num = s_itf_num;
    out->ep_in = s_ep_in;
    out->ep_out = s_ep_out;
    /* Derived from the stack, never from a shadow flag — see out_arm(). */
    out->out_armed = (s_ep_out != 0u && usbd_edpt_busy(0u, s_ep_out)) ? 1u : 0u;
    out->out_hw_busy = out->out_armed;
    out->tx_busy = s_tx_busy ? 1u : 0u;
    out->tx_in_zlp = s_tx_in_zlp ? 1u : 0u;
    out->rx_have = (uint8_t)(s_rx.have > 0xFFu ? 0xFFu : s_rx.have);
    out->last_result = s_last_result;
    out->init_count = s_init_count;
    out->open_count = s_open_count;
    out->reset_count = s_reset_count;
    out->out_cb_count = s_out_cb_count;
    out->in_cb_count = s_in_cb_count;
    out->out_rearm_ok = s_out_rearm_ok;
    out->out_rearm_fail = s_out_rearm_fail;
    out->in_start_ok = s_in_start_ok;
    out->in_start_fail = s_in_start_fail;
    out->in_dropped = s_in_dropped;
    out->rx_bytes = s_rx_bytes;

    harness_status_t proto;
    harness_protocol_stats(&proto);
    out->frames_ok = proto.frames_ok;
    out->frames_err = proto.frames_err;
    out->crc_errors = proto.crc_errors;
    out->uptime_ms = proto.uptime_ms;
}

/* -------------------------------------------------------------------------- */
/* USBD driver                                                                */
/* -------------------------------------------------------------------------- */

static void harnessd_init(void) {
    s_init_count++;
    harness_usb_vendor_reset_state();
    harness_protocol_init();
}

static void harnessd_reset(uint8_t rhport) {
    (void)rhport;
    s_reset_count++;
    harness_usb_vendor_reset_state();
}

static uint16_t harnessd_open(uint8_t rhport, tusb_desc_interface_t const *desc_itf,
                              uint16_t max_len) {
    TU_VERIFY(desc_itf->bInterfaceClass == TUSB_CLASS_VENDOR_SPECIFIC &&
                  desc_itf->bInterfaceSubClass == HARNESS_ITF_SUBCLASS &&
                  desc_itf->bInterfaceProtocol == HARNESS_ITF_PROTOCOL,
              0);

    const uint8_t *p_desc = tu_desc_next(desc_itf);
    const uint8_t *desc_end = p_desc + max_len;

    s_itf_num = desc_itf->bInterfaceNumber;

    uint8_t found_ep = 0;
    while (found_ep < desc_itf->bNumEndpoints) {
        while (p_desc < desc_end && TUSB_DESC_ENDPOINT != tu_desc_type(p_desc)) {
            p_desc = tu_desc_next(p_desc);
        }
        if (p_desc >= desc_end) {
            break;
        }

        const tusb_desc_endpoint_t *desc_ep = (const tusb_desc_endpoint_t *)p_desc;
        TU_ASSERT(usbd_edpt_open(rhport, desc_ep));

        if (tu_edpt_dir(desc_ep->bEndpointAddress) == TUSB_DIR_IN) {
            s_ep_in = desc_ep->bEndpointAddress;
        } else {
            s_ep_out = desc_ep->bEndpointAddress;
        }

        found_ep++;
        p_desc = tu_desc_next(p_desc);
    }

    TU_ASSERT(s_ep_in != 0u && s_ep_out != 0u, 0);

    harness_rx_init(&s_rx);
    s_tx_busy = false;
    s_tx_in_zlp = false;

    s_open_count++;

    /* Prime the OUT endpoint. If this ever fails, the main loop retries. */
    (void)out_arm();

    return (uint16_t)((uintptr_t)p_desc - (uintptr_t)desc_itf);
}

static bool harnessd_control_xfer_cb(uint8_t rhport, uint8_t stage,
                                     tusb_control_request_t const *request) {
    (void)rhport;
    (void)stage;
    (void)request;
    /* Class/vendor control requests for this interface are not implemented. */
    return false;
}

static bool harnessd_xfer_cb(uint8_t rhport, uint8_t ep_addr, xfer_result_t result,
                             uint32_t xferred_bytes) {
    if (ep_addr == s_ep_out) {
        s_out_cb_count++;
        s_last_result = (uint8_t)result;

        if (result == XFER_RESULT_SUCCESS && xferred_bytes != 0u) {
            s_rx_bytes += xferred_bytes;
            harness_rx_push(&s_rx, s_rx_ep_buf, (uint16_t)xferred_bytes);
            process_frames();
        }

        /* Re-arm so the next request can be received while the previous reply
         * is still going out. The main loop retries if this fails. */
        (void)out_arm();
        return true;
    }

    if (ep_addr == s_ep_in) {
        s_in_cb_count++;

        if (s_tx_in_zlp) {
            /* The trailing zero-length packet finished: the reply is complete. */
            s_tx_in_zlp = false;
            s_tx_busy = false;
        } else if (xferred_bytes != 0u && (xferred_bytes % HARNESS_EP_SIZE) == 0u) {
            /* Reply length is a multiple of the packet size — the host cannot
             * see a short packet, so terminate with a ZLP. */
            s_tx_in_zlp = true;
            if (!usbd_edpt_xfer(rhport, s_ep_in, NULL, 0u)) {
                s_tx_in_zlp = false;
                s_tx_busy = false;
            }
            return true;
        } else {
            s_tx_busy = false;
        }

        /* A pipelined request may have been waiting for the reply slot. */
        process_frames();
        return true;
    }

    return false;
}

static usbd_class_driver_t const harness_usbd_driver = {
#if CFG_TUSB_DEBUG >= 2
    .name = "HARNESS",
#endif
    .init = harnessd_init,
    .reset = harnessd_reset,
    .open = harnessd_open,
    .control_xfer_cb = harnessd_control_xfer_cb,
    .xfer_cb = harnessd_xfer_cb,
    .sof = NULL,
};

/*
 * Register our driver next to the SDK's reset-interface driver. The reset
 * interface is what makes `picotool reboot -f -u` work (TASK.md §6).
 *
 * TinyUSB expects a pointer to a contiguous ARRAY of driver structs, so the two
 * separately-declared drivers are copied into one array here. `tud_init()`
 * calls this callback exactly once, before any class driver is used.
 */
usbd_class_driver_t const *usbd_app_driver_get_cb(uint8_t *driver_count) {
    static usbd_class_driver_t s_app_drivers[2];

    s_app_drivers[0] = harness_usbd_driver;
    s_app_drivers[1] = usb_reset_interface_driver;

    *driver_count = 2u;
    return s_app_drivers;
}

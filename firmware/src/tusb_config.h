/*
 * RP2040 Harness — TinyUSB device configuration.
 *
 * The Pico SDK only supplies a default `tusb_config.h` through pico_stdio_usb;
 * a project that drives TinyUSB directly must provide its own (this is also why
 * the SDK's own reset-interface config defaults are "off" for such projects).
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef _HARNESS_TUSB_CONFIG_H
#define _HARNESS_TUSB_CONFIG_H

/* The RP2040 has a single USB controller, used as a device. */
#define CFG_TUSB_RHPORT0_MODE (OPT_MODE_DEVICE)

/*
 * No TinyUSB logging — the debug console is UART0 (GP0/GP1) and a blocking
 * printf inside the USB task would wreck the STREAM timing measurements.
 */
#ifndef CFG_TUSB_DEBUG
#define CFG_TUSB_DEBUG 0
#endif

/*
 * Class drivers.
 *
 * CFG_TUD_VENDOR MUST stay 0. The harness implements its own vendor class
 * driver (src/usb_vendor.c) registered through `usbd_app_driver_get_cb`,
 * because TinyUSB's built-in vendor class would claim the single global
 * `tud_vendor_control_xfer_cb` hook that the Microsoft OS 2.0 (WCID) descriptor
 * handler needs — see src/usb_descriptors.c.
 */
#define CFG_TUD_CDC     0
#define CFG_TUD_MSC     0
#define CFG_TUD_HID     0
#define CFG_TUD_MIDI    0
#define CFG_TUD_VENDOR  0
#define CFG_TUD_AUDIO   0
#define CFG_TUD_VIDEO   0
#define CFG_TUD_USBTMC  0
#define CFG_TUD_DFU     0
#define CFG_TUD_DFU_RT  0
#define CFG_TUD_BTH     0

/* Full-speed control endpoint size. */
#ifndef CFG_TUD_ENDPOINT0_SIZE
#define CFG_TUD_ENDPOINT0_SIZE 64
#endif

#endif /* _HARNESS_TUSB_CONFIG_H */

/*
 * RP2040 Harness — compile-time configuration and pin map.
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef HARNESS_CONFIG_H
#define HARNESS_CONFIG_H

#include <stdint.h>

/* -------------------------------------------------------------------------- */
/* Identity                                                                   */
/* -------------------------------------------------------------------------- */

#define HARNESS_FW_VERSION_MAJOR 1
#define HARNESS_FW_VERSION_MINOR 0
#define HARNESS_FW_VERSION_PATCH 2

#define HARNESS_PROTO_VERSION_MAJOR 1
#define HARNESS_PROTO_VERSION_MINOR 0

#ifndef HARNESS_USB_VID
#define HARNESS_USB_VID 0x2E8A
#endif
#ifndef HARNESS_USB_PID
#define HARNESS_USB_PID 0x10C0
#endif

#define HARNESS_USB_MANUFACTURER "RP2040-Harness"
#define HARNESS_USB_PRODUCT      "RP2040 Harness"

/* -------------------------------------------------------------------------- */
/* USB interfaces / endpoints                                                 */
/* -------------------------------------------------------------------------- */

#define HARNESS_ITF_DATA   0u
#define HARNESS_ITF_RESET  1u
#define HARNESS_ITF_COUNT  2u

/* Interface 0 identity — must match docs/protocol.md §1. */
#define HARNESS_ITF_SUBCLASS 0x48u /* 'H' */
#define HARNESS_ITF_PROTOCOL 0x01u

/* Interface 0 string descriptor index. */
#define HARNESS_STR_DATA 4u

#define HARNESS_EP_DATA_OUT 0x01u
#define HARNESS_EP_DATA_IN  0x81u
#define HARNESS_EP_SIZE     64u

/* -------------------------------------------------------------------------- */
/* Pin map — logical protocol index 0..23 -> GPIO (docs/protocol.md §3)       */
/*                                                                            */
/*   index  0..20 -> GP2..GP22                                                */
/*   index 21..23 -> GP26..GP28                                               */
/* -------------------------------------------------------------------------- */

#define HARNESS_PIN_COUNT 24u
#define HARNESS_PIN_MASK  0x00FFFFFFu

extern const uint8_t harness_gpio[HARNESS_PIN_COUNT];

#endif /* HARNESS_CONFIG_H */

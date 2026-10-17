/*
 * RP2040 Harness — USB descriptor entry points.
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef HARNESS_USB_DESCRIPTORS_H
#define HARNESS_USB_DESCRIPTORS_H

#include <stdint.h>

/* Provided by usb_descriptors.c and consumed by the TinyUSB device stack. */
const uint8_t *tud_descriptor_device_cb(void);
const uint8_t *tud_descriptor_configuration_cb(uint8_t index);
const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid);
const uint8_t *tud_descriptor_bos_cb(void);

#endif /* HARNESS_USB_DESCRIPTORS_H */

/*
 * RP2040 Harness — GPIO configuration and atomic line driving.
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef HARNESS_GPIO_CTRL_H
#define HARNESS_GPIO_CTRL_H

#include <stdbool.h>
#include <stdint.h>

/* Wire values for `flags` (docs/protocol.md §4.5). */
#define HARNESS_FLAG_PULL_UP    0x01u
#define HARNESS_FLAG_PULL_DOWN  0x02u
#define HARNESS_FLAG_OPEN_DRAIN 0x04u
#define HARNESS_FLAG_SCHMITT    0x08u
#define HARNESS_FLAG_ALL        0x0Fu

/* Wire values for `dir`. */
#define HARNESS_DIR_IN  0u
#define HARNESS_DIR_OUT 1u

/* Wire values for `drive` (docs/protocol.md §4.6). */
#define HARNESS_DRIVE_2MA  0u
#define HARNESS_DRIVE_4MA  1u
#define HARNESS_DRIVE_8MA  2u
#define HARNESS_DRIVE_12MA 3u

/* Wire values for `slew`. */
#define HARNESS_SLEW_SLOW 0u
#define HARNESS_SLEW_FAST 1u

/*
 * Boot/reset state: every harness line is an input with no pulls.
 * Called before USB is brought up, and again on USB unlink (TASK.md §3.1,
 * docs/protocol.md §5.1/§5.2).
 */
void harness_gpio_safe_state(void);

/* True when `index` is a valid logical pin index (0..23). */
bool harness_gpio_index_valid(uint8_t index);

/*
 * Apply a complete pin configuration. Returns 0 on success or a protocol error
 * code (HARNESS_STATUS_ERR_BAD_PARAM). On error nothing is changed.
 */
int harness_gpio_configure(uint8_t index, uint8_t dir, uint8_t flags,
                           uint8_t drive, uint8_t slew, uint8_t level);

/* Drive an already-OUTPUT pin to `level`. Returns 0 or ERR_BAD_PARAM. */
int harness_gpio_set(uint8_t index, uint8_t level);

/* Atomic masked write; returns the resulting 24-bit output latch value. */
uint32_t harness_gpio_set_mask(uint32_t mask, uint32_t value);

/* Current levels of all 24 lines (SIO GPIO_IN). */
uint32_t harness_gpio_read_all(void);

/* Read-back accessors for GET_PIN. */
uint8_t harness_gpio_get_dir(uint8_t index);
uint8_t harness_gpio_get_flags(uint8_t index);
uint8_t harness_gpio_get_drive(uint8_t index);
uint8_t harness_gpio_get_slew(uint8_t index);
uint8_t harness_gpio_get_level(uint8_t index);

/*
 * Maximum-rate sequence writer used by the STREAM opcode.
 *
 * For each element of `values` this performs exactly one 32-bit store to the
 * SIO output-latch register, so no intermediate "torn" state can be observed on
 * any line. Bits outside `mask` keep their previous value throughout.
 *
 * `toggles` (optional) receives the total number of line transitions.
 * Returns the number of values written.
 */
uint32_t harness_gpio_stream(uint32_t mask, const uint32_t *values,
                             uint32_t count, uint32_t *toggles);

#endif /* HARNESS_GPIO_CTRL_H */

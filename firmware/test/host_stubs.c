/*
 * Host-build stubs for the RP2040-side dependencies of firmware/src/protocol.c.
 *
 * This models just enough of the pin hardware for the protocol layer to be
 * exercised on a PC: per-pin direction/flags/drive/slew plus an output latch.
 * It is NOT the firmware's real GPIO layer — that one lives in
 * firmware/src/gpio_ctrl.c and is only ever built for the target.
 *
 * SPDX-License-Identifier: MIT
 */

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "gpio_ctrl.h"
#include "harness_config.h"
#include "harness_status.h"
#include "pico/time.h"
#include "pico/unique_id.h"

/* -------------------------------------------------------------------------- */
/* Pin map (must match firmware/src/gpio_ctrl.c)                              */
/* -------------------------------------------------------------------------- */

const uint8_t harness_gpio[HARNESS_PIN_COUNT] = {
    2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22,
    26, 27, 28,
};

#define HOST_GPIO_MAX 30u

static uint8_t g_dir[HARNESS_PIN_COUNT];
static uint8_t g_flags[HARNESS_PIN_COUNT];
static uint8_t g_drive[HARNESS_PIN_COUNT];
static uint8_t g_slew[HARNESS_PIN_COUNT];

/* Output latch for every physical GPIO, mirroring SIO GPIO_OUT. */
static uint32_t g_latch;

/*
 * Logical protocol index <-> physical SIO/GPIO bit. The real firmware uses a
 * byte lookup table here for speed; the host model just loops, because it only
 * has to agree with the firmware, not match its performance.
 */
static uint32_t logical_to_gpio_bits(uint32_t logical) {
    uint32_t bits = 0;
    for (uint32_t i = 0; i < HARNESS_PIN_COUNT; i++) {
        if (logical & (1u << i)) {
            bits |= 1u << harness_gpio[i];
        }
    }
    return bits;
}

static uint32_t gpio_to_logical_bits(uint32_t gpio_bits) {
    uint32_t logical = 0;
    for (uint32_t i = 0; i < HARNESS_PIN_COUNT; i++) {
        if (gpio_bits & (1u << harness_gpio[i])) {
            logical |= 1u << i;
        }
    }
    return logical;
}

static bool valid_flags(uint8_t flags) {
    if (flags & (uint8_t)~HARNESS_FLAG_ALL) {
        return false;
    }
    if ((flags & HARNESS_FLAG_PULL_UP) && (flags & HARNESS_FLAG_PULL_DOWN)) {
        return false;
    }
    return true;
}

void harness_gpio_safe_state(void) {
    memset(g_dir, HARNESS_DIR_IN, sizeof(g_dir));
    memset(g_flags, 0, sizeof(g_flags));
    memset(g_drive, HARNESS_DRIVE_4MA, sizeof(g_drive));
    memset(g_slew, HARNESS_SLEW_SLOW, sizeof(g_slew));
    for (unsigned i = 0; i < HARNESS_PIN_COUNT; i++) {
        g_flags[i] = HARNESS_FLAG_SCHMITT;
    }
    g_latch = 0;
}

bool harness_gpio_index_valid(uint8_t index) {
    return index < HARNESS_PIN_COUNT;
}

int harness_gpio_configure(uint8_t index, uint8_t dir, uint8_t flags,
                           uint8_t drive, uint8_t slew, uint8_t level) {
    if (!harness_gpio_index_valid(index)) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if (dir > HARNESS_DIR_OUT) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if (!valid_flags(flags)) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if (drive > HARNESS_DRIVE_12MA || slew > HARNESS_SLEW_FAST || level > 1u) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if ((flags & HARNESS_FLAG_OPEN_DRAIN) && dir != HARNESS_DIR_OUT) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }

    const uint32_t bit = 1u << harness_gpio[index];
    g_latch = (g_latch & ~bit) | ((dir == HARNESS_DIR_OUT && level) ? bit : 0u);

    /* docs/protocol.md §4.4: drive/slew are ignored for an INPUT pin. */
    g_dir[index] = dir;
    g_flags[index] = flags;
    if (dir == HARNESS_DIR_OUT) {
        g_drive[index] = drive;
        g_slew[index] = slew;
    }
    return HARNESS_STATUS_OK;
}

int harness_gpio_set(uint8_t index, uint8_t level) {
    if (!harness_gpio_index_valid(index) || level > 1u) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if (g_dir[index] != HARNESS_DIR_OUT) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    const uint32_t bit = 1u << harness_gpio[index];
    g_latch = (g_latch & ~bit) | (level ? bit : 0u);
    return HARNESS_STATUS_OK;
}

uint32_t harness_gpio_set_mask(uint32_t mask, uint32_t value) {
    const uint32_t gm = logical_to_gpio_bits(mask & (uint32_t)HARNESS_PIN_MASK);
    const uint32_t gv = logical_to_gpio_bits(value & (uint32_t)HARNESS_PIN_MASK);
    g_latch = (g_latch & ~gm) | (gv & gm);
    return gpio_to_logical_bits(g_latch);
}

uint32_t harness_gpio_read_all(void) {
    uint32_t levels = 0;
    for (uint8_t i = 0; i < HARNESS_PIN_COUNT; i++) {
        if (g_dir[i] == HARNESS_DIR_OUT && (g_latch & (1u << harness_gpio[i]))) {
            levels |= (uint32_t)1u << i;
        }
    }
    return levels;
}

uint8_t harness_gpio_get_dir(uint8_t index) {
    return harness_gpio_index_valid(index) ? g_dir[index] : 0u;
}

uint8_t harness_gpio_get_flags(uint8_t index) {
    return harness_gpio_index_valid(index) ? g_flags[index] : 0u;
}

uint8_t harness_gpio_get_drive(uint8_t index) {
    return harness_gpio_index_valid(index) ? g_drive[index] : 0u;
}

uint8_t harness_gpio_get_slew(uint8_t index) {
    return harness_gpio_index_valid(index) ? g_slew[index] : 0u;
}

uint8_t harness_gpio_get_level(uint8_t index) {
    if (!harness_gpio_index_valid(index)) {
        return 0u;
    }
    return (g_latch >> harness_gpio[index]) & 1u;
}

uint32_t harness_gpio_stream(uint32_t mask, const uint32_t *values,
                             uint32_t count, uint32_t *toggles) {
    const uint32_t gm = logical_to_gpio_bits(mask & (uint32_t)HARNESS_PIN_MASK);
    const uint32_t keep = ~gm;
    uint32_t transitions = 0;
    uint32_t i = 0;

    for (; i < count; i++) {
        const uint32_t next = (g_latch & keep) | (logical_to_gpio_bits(values[i]) & gm);
        if (next != g_latch) {
            uint32_t x = next ^ g_latch;
            while (x) {
                transitions += x & 1u;
                x >>= 1;
            }
        }
        g_latch = next;
    }

    if (toggles) {
        *toggles = transitions;
    }
    return i;
}

/* -------------------------------------------------------------------------- */
/* pico/time.h and pico/unique_id.h stand-ins                                 */
/* -------------------------------------------------------------------------- */

static uint64_t g_fake_us = 0;

/* Each call advances the fake clock by 1 us so STREAM durations are nonzero. */
uint32_t time_us_32(void) {
    return (uint32_t)(g_fake_us++);
}

uint64_t get_absolute_time(void) {
    return g_fake_us;
}

uint32_t to_ms_since_boot(absolute_time_t t) {
    return (uint32_t)(t / 1000u);
}

void pico_get_unique_board_id(pico_unique_board_id_t *id) {
    static const uint8_t fixed[PICO_UNIQUE_BOARD_ID_SIZE_BYTES] = {
        0xDE, 0xAD, 0xBE, 0xEF, 0x01, 0x02, 0x03, 0x04,
    };
    memcpy(id->id, fixed, sizeof(fixed));
}

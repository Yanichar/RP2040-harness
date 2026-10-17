/*
 * RP2040 Harness — GPIO configuration and atomic line driving.
 *
 * SPDX-License-Identifier: MIT
 */

#include "gpio_ctrl.h"

#include <string.h>

#include "hardware/gpio.h"
#include "hardware/regs/pads_bank0.h"
#include "hardware/structs/padsbank0.h"
#include "hardware/structs/sio.h"

#include "harness_config.h"
#include "harness_status.h"

/* Logical protocol index -> GPIO (docs/protocol.md §3). */
const uint8_t harness_gpio[HARNESS_PIN_COUNT] = {
    2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22,
    26, 27, 28,
};

typedef struct {
    uint8_t dir;
    uint8_t flags;
    uint8_t drive;
    uint8_t slew;
} pin_state_t;

static pin_state_t pin_state[HARNESS_PIN_COUNT];

/*
 * ---------------------------------------------------------------------------
 * Logical index <-> SIO bit translation
 * ---------------------------------------------------------------------------
 *
 * The protocol addresses lines by LOGICAL index 0..23 (docs/protocol.md §3),
 * but the SIO output/input registers are indexed by PHYSICAL GPIO number. The
 * pin map is deliberately not contiguous (GP2..GP22 then GP26..GP28), so every
 * mask, value and readback has to be translated.
 *
 * The translation sits on the STREAM hot path, so it is done with three
 * 256-entry byte lookups rather than a 24-iteration bit loop.
 */

static uint32_t s_byte_lut[3][256];
static bool s_lut_ready;

static void build_lut(void) {
    for (uint32_t byte = 0; byte < 3; byte++) {
        for (uint32_t value = 0; value < 256; value++) {
            uint32_t bits = 0;
            for (uint32_t k = 0; k < 8; k++) {
                const uint32_t index = byte * 8u + k;
                if (index < HARNESS_PIN_COUNT && (value & (1u << k))) {
                    bits |= 1u << harness_gpio[index];
                }
            }
            s_byte_lut[byte][value] = bits;
        }
    }
    s_lut_ready = true;
}

static inline void ensure_lut(void) {
    if (!s_lut_ready) {
        build_lut();
    }
}

/* Logical-index bits -> SIO/GPIO bits. */
static inline uint32_t logical_to_gpio_bits(uint32_t logical) {
    return s_byte_lut[0][logical & 0xFFu] | s_byte_lut[1][(logical >> 8) & 0xFFu] |
           s_byte_lut[2][(logical >> 16) & 0xFFu];
}

/* SIO/GPIO bits -> logical-index bits (cold path only). */
static uint32_t gpio_to_logical_bits(uint32_t gpio_bits) {
    uint32_t logical = 0;
    for (uint32_t i = 0; i < HARNESS_PIN_COUNT; i++) {
        if (gpio_bits & (1u << harness_gpio[i])) {
            logical |= 1u << i;
        }
    }
    return logical;
}

bool harness_gpio_index_valid(uint8_t index) {
    return index < HARNESS_PIN_COUNT;
}

/*
 * The RP2040 has no open-drain helper in the SDK; the pad's OD bit is the
 * hardware feature (PADS_BANK0_GPIO0_OD, bit 7).
 */
static inline void gpio_set_open_drain(uint gpio, bool open_drain) {
    uint32_t pad = padsbank0_hw->io[gpio];
    if (open_drain) {
        pad |= PADS_BANK0_GPIO0_OD_BITS;
    } else {
        pad &= ~(uint32_t)PADS_BANK0_GPIO0_OD_BITS;
    }
    padsbank0_hw->io[gpio] = pad;
}

static void apply_pin(uint8_t index, const pin_state_t *st) {
    const uint gpio = harness_gpio[index];

    /* Slew / drive / Schmitt are pad properties; set them before enabling the
     * output driver so the first driven edge already has the right shape. */
    gpio_set_slew_rate(gpio, (enum gpio_slew_rate)st->slew);
    gpio_set_drive_strength(gpio, (enum gpio_drive_strength)st->drive);
    gpio_set_input_hysteresis_enabled(gpio, (st->flags & HARNESS_FLAG_SCHMITT) != 0);

    gpio_set_pulls(gpio, (st->flags & HARNESS_FLAG_PULL_UP) != 0,
                   (st->flags & HARNESS_FLAG_PULL_DOWN) != 0);

    gpio_set_open_drain(gpio, (st->flags & HARNESS_FLAG_OPEN_DRAIN) != 0);

    if (st->dir == HARNESS_DIR_OUT) {
        gpio_set_dir(gpio, GPIO_OUT);
    } else {
        gpio_set_dir(gpio, GPIO_IN);
    }
}

void harness_gpio_safe_state(void) {
    ensure_lut();

    for (uint8_t i = 0; i < HARNESS_PIN_COUNT; i++) {
        const uint gpio = harness_gpio[i];

        /* Drop the output driver first, then clear the latch. Each gpio_put()
         * is a single atomic SIO write, and only touches this line. */
        gpio_set_dir(gpio, GPIO_IN);
        gpio_set_function(gpio, GPIO_FUNC_SIO);
        gpio_put(gpio, 0);
        gpio_set_pulls(gpio, false, false);
        gpio_set_open_drain(gpio, false);
        gpio_set_input_hysteresis_enabled(gpio, true);
        gpio_set_slew_rate(gpio, GPIO_SLEW_RATE_SLOW);
        gpio_set_drive_strength(gpio, GPIO_DRIVE_STRENGTH_4MA);

        pin_state[i].dir = HARNESS_DIR_IN;
        pin_state[i].flags = HARNESS_FLAG_SCHMITT;
        pin_state[i].drive = HARNESS_DRIVE_4MA;
        pin_state[i].slew = HARNESS_SLEW_SLOW;
    }
}

int harness_gpio_configure(uint8_t index, uint8_t dir, uint8_t flags,
                           uint8_t drive, uint8_t slew, uint8_t level) {
    if (!harness_gpio_index_valid(index)) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if (dir > HARNESS_DIR_OUT) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if (flags & (uint8_t)~HARNESS_FLAG_ALL) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if (flags & HARNESS_FLAG_PULL_UP && flags & HARNESS_FLAG_PULL_DOWN) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if (drive > HARNESS_DRIVE_12MA) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if (slew > HARNESS_SLEW_FAST) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if (level > 1u) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    /* Open-drain only makes sense on a driven line (TASK.md §3.4). */
    if ((flags & HARNESS_FLAG_OPEN_DRAIN) && dir != HARNESS_DIR_OUT) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }

    /*
     * docs/protocol.md §4.4: for an INPUT pin, `drive`, `slew` and `level` are
     * validated but ignored — the line keeps the drive/slew it already had, and
     * its output latch is forced low. `flags` always applies.
     */
    pin_state_t next = pin_state[index];
    next.dir = dir;
    next.flags = flags;
    if (dir == HARNESS_DIR_OUT) {
        next.drive = drive;
        next.slew = slew;
    }

    const uint gpio = harness_gpio[index];
    gpio_put(gpio, dir == HARNESS_DIR_OUT ? (level != 0) : false);

    pin_state[index] = next;
    apply_pin(index, &next);
    return HARNESS_STATUS_OK;
}

int harness_gpio_set(uint8_t index, uint8_t level) {
    if (!harness_gpio_index_valid(index) || level > 1u) {
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }
    if (pin_state[index].dir != HARNESS_DIR_OUT) {
        /* Implicit direction changes are forbidden (docs/protocol.md §4.7). */
        return HARNESS_STATUS_ERR_BAD_PARAM;
    }

    const uint32_t bit = 1u << harness_gpio[index];
    sio_hw->gpio_out = (sio_hw->gpio_out & ~bit) | (level ? bit : 0u);
    return HARNESS_STATUS_OK;
}

uint32_t harness_gpio_set_mask(uint32_t mask, uint32_t value) {
    ensure_lut();

    const uint32_t gpio_mask = logical_to_gpio_bits(mask & (uint32_t)HARNESS_PIN_MASK);
    const uint32_t gpio_value = logical_to_gpio_bits(value & (uint32_t)HARNESS_PIN_MASK);

    /* One 32-bit store to the SIO output latch: every masked line changes in
     * the same cycle, so no intermediate state is ever visible (TASK.md §3.1). */
    const uint32_t next = (sio_hw->gpio_out & ~gpio_mask) | (gpio_value & gpio_mask);
    sio_hw->gpio_out = next;

    return gpio_to_logical_bits(next);
}

uint32_t harness_gpio_read_all(void) {
    return gpio_to_logical_bits(sio_hw->gpio_in);
}

uint8_t harness_gpio_get_dir(uint8_t index) {
    return harness_gpio_index_valid(index) ? pin_state[index].dir : 0u;
}

uint8_t harness_gpio_get_flags(uint8_t index) {
    return harness_gpio_index_valid(index) ? pin_state[index].flags : 0u;
}

uint8_t harness_gpio_get_drive(uint8_t index) {
    return harness_gpio_index_valid(index) ? pin_state[index].drive : 0u;
}

uint8_t harness_gpio_get_slew(uint8_t index) {
    return harness_gpio_index_valid(index) ? pin_state[index].slew : 0u;
}

uint8_t harness_gpio_get_level(uint8_t index) {
    if (!harness_gpio_index_valid(index)) {
        return 0u;
    }
    return (sio_hw->gpio_in >> harness_gpio[index]) & 1u;
}

uint32_t harness_gpio_stream(uint32_t mask, const uint32_t *values,
                             uint32_t count, uint32_t *toggles) {
    if (!values || !count || !mask) {
        if (toggles) {
            *toggles = 0;
        }
        return 0;
    }

    ensure_lut();

    const uint32_t gpio_mask = logical_to_gpio_bits(mask & (uint32_t)HARNESS_PIN_MASK);
    const uint32_t keep = ~gpio_mask;
    uint32_t cur = sio_hw->gpio_out;
    uint32_t transitions = 0;
    uint32_t i = 0;

    for (; i < count; i++) {
        /* Translation is three table lookups; the store below is the whole
         * point of the opcode — one atomic SIO write per update. */
        const uint32_t next = (cur & keep) | (logical_to_gpio_bits(values[i]) & gpio_mask);
        if (next != cur) {
            transitions += (uint32_t)__builtin_popcount(next ^ cur);
            cur = next;
        }
        sio_hw->gpio_out = next;
    }

    if (toggles) {
        *toggles = transitions;
    }
    return i;
}

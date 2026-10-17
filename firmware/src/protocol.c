/*
 * RP2040 Harness — wire protocol codec and request dispatcher.
 *
 * SPDX-License-Identifier: MIT
 */

#include "protocol.h"

#include <string.h>

#include "pico/time.h"
#include "pico/unique_id.h"

#include "crc16.h"
#include "gpio_ctrl.h"
#include "harness_config.h"
#include "harness_status.h"

/* Largest reply payload we ever produce (GET_INFO = 32 bytes). */
#define HARNESS_MAX_REPLY_PAYLOAD 32u

static uint32_t s_frames_ok;
static uint32_t s_frames_err;
static uint32_t s_crc_errors;
static uint8_t s_last_status = HARNESS_STATUS_OK;

/* -------------------------------------------------------------------------- */
/* Little-endian helpers (no unaligned accesses)                              */
/* -------------------------------------------------------------------------- */

static inline uint16_t rd16(const uint8_t *p) {
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static inline uint32_t rd32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static inline void wr16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
}

/* -------------------------------------------------------------------------- */
/* Receive accumulator                                                        */
/* -------------------------------------------------------------------------- */

void harness_rx_init(harness_rx_t *rx) {
    rx->have = 0;
    rx->last_error = HARNESS_STATUS_OK;
}

void harness_rx_push(harness_rx_t *rx, const uint8_t *data, uint16_t len) {
    if ((uint32_t)rx->have + len > sizeof(rx->buf)) {
        /* Cannot resynchronise on an overflowing stream (docs/protocol.md §2.5). */
        rx->have = 0;
        rx->last_error = HARNESS_STATUS_ERR_BAD_LENGTH;
        return;
    }
    memcpy(rx->buf + rx->have, data, len);
    rx->have = (uint16_t)(rx->have + len);
}

harness_rx_result_t harness_rx_poll(harness_rx_t *rx, uint16_t *frame_len) {
    *frame_len = 0;

    if (rx->have < HARNESS_HDR_LEN) {
        return HARNESS_RX_NONE;
    }

    const uint8_t magic = rx->buf[0];
    const uint16_t len = rd16(rx->buf + 4);

    if (magic != HARNESS_REQ_MAGIC || len > HARNESS_MAX_PAYLOAD) {
        rx->last_error = (magic != HARNESS_REQ_MAGIC) ? HARNESS_STATUS_ERR_MAGIC
                                                      : HARNESS_STATUS_ERR_BAD_LENGTH;

        /* Resynchronise on the next magic byte. */
        uint16_t i = 1;
        while (i < rx->have && rx->buf[i] != HARNESS_REQ_MAGIC) {
            i++;
        }
        if (i < rx->have) {
            memmove(rx->buf, rx->buf + i, (size_t)(rx->have - i));
            rx->have = (uint16_t)(rx->have - i);
        } else {
            rx->have = 0;
        }
        return HARNESS_RX_CORRUPT;
    }

    const uint16_t total = (uint16_t)(HARNESS_HDR_LEN + len);
    if (rx->have < total) {
        return HARNESS_RX_NONE;
    }

    *frame_len = total;
    return HARNESS_RX_FRAME;
}

void harness_rx_consume(harness_rx_t *rx, uint16_t frame_len) {
    if (frame_len >= rx->have) {
        rx->have = 0;
        return;
    }
    memmove(rx->buf, rx->buf + frame_len, (size_t)(rx->have - frame_len));
    rx->have = (uint16_t)(rx->have - frame_len);
}

/* -------------------------------------------------------------------------- */
/* Counters                                                                   */
/* -------------------------------------------------------------------------- */

void harness_protocol_init(void) {
    s_frames_ok = 0;
    s_frames_err = 0;
    s_crc_errors = 0;
    s_last_status = HARNESS_STATUS_OK;
}

void harness_protocol_stats(harness_status_t *out) {
    memset(out, 0, sizeof(*out));
    out->state = 0; /* the device is never streaming between USB transactions */
    out->last_status = s_last_status;
    out->frames_ok = s_frames_ok;
    out->frames_err = s_frames_err;
    out->crc_errors = s_crc_errors;
    out->uptime_ms = to_ms_since_boot(get_absolute_time());
}

/* -------------------------------------------------------------------------- */
/* Reply rendering                                                            */
/* -------------------------------------------------------------------------- */

/*
 * CRC of a frame covers `opcode/status, seq, len` (5 bytes at offset 1) followed
 * by the payload. Those two regions are NOT contiguous in the buffer — the CRC
 * field itself sits between them at offset 6 — so the checksum is accumulated in
 * two steps.
 */
static uint16_t frame_crc(const uint8_t *frame, uint16_t payload_len) {
    uint16_t crc = harness_crc16_update(HARNESS_CRC16_INIT, frame + 1, 5u);
    if (payload_len != 0u) {
        crc = harness_crc16_update(crc, frame + HARNESS_HDR_LEN, payload_len);
    }
    return crc;
}

static void build_reply(uint8_t *rep, uint8_t status, uint16_t seq,
                        const uint8_t *payload, uint16_t payload_len) {
    rep[0] = HARNESS_REP_MAGIC;
    rep[1] = status;
    wr16(rep + 2, seq);
    wr16(rep + 4, payload_len);
    if (payload_len && payload) {
        memcpy(rep + HARNESS_HDR_LEN, payload, payload_len);
    }
    wr16(rep + 6, frame_crc(rep, payload_len));
}

/* -------------------------------------------------------------------------- */
/* Dispatcher                                                                 */
/* -------------------------------------------------------------------------- */

void harness_protocol_error_reply(uint8_t *rep, uint8_t status, uint16_t seq,
                                  uint16_t *rep_len) {
    s_frames_err++;
    s_last_status = status;
    build_reply(rep, status, seq, NULL, 0);
    *rep_len = HARNESS_HDR_LEN;
}

void harness_protocol_handle(const uint8_t *req, uint16_t req_len,
                             uint8_t *rep, uint16_t rep_cap, uint16_t *rep_len) {
    uint8_t payload[HARNESS_MAX_REPLY_PAYLOAD];
    uint16_t payload_len = 0;
    uint8_t status = HARNESS_STATUS_OK;
    uint16_t seq = 0;

    if (req_len < HARNESS_HDR_LEN || rep_cap < HARNESS_HDR_LEN + HARNESS_MAX_REPLY_PAYLOAD) {
        /* Programming error — cannot even build a reply. */
        *rep_len = 0;
        return;
    }

    const uint8_t opcode = req[1];
    seq = rd16(req + 2);
    const uint16_t len = rd16(req + 4);

    if (req[0] != HARNESS_REQ_MAGIC) {
        status = HARNESS_STATUS_ERR_MAGIC;
        seq = 0;
    } else if (len > HARNESS_MAX_PAYLOAD || req_len != (uint16_t)(HARNESS_HDR_LEN + len)) {
        status = HARNESS_STATUS_ERR_BAD_LENGTH;
    } else {
        const uint16_t crc_rx = rd16(req + 6);
        const uint16_t crc_calc = frame_crc(req, len);
        if (crc_rx != crc_calc) {
            status = HARNESS_STATUS_ERR_CRC;
            s_crc_errors++;
        }
    }

    if (status != HARNESS_STATUS_OK) {
        s_frames_err++;
        s_last_status = status;
        build_reply(rep, status, seq, NULL, 0);
        *rep_len = HARNESS_HDR_LEN;
        return;
    }

    const uint8_t *body = req + HARNESS_HDR_LEN;

    switch (opcode) {
        case HARNESS_OP_GET_INFO: {
            if (len != 0u) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            harness_info_t info;
            memset(&info, 0, sizeof(info));
            info.proto_major = HARNESS_PROTO_VERSION_MAJOR;
            info.proto_minor = HARNESS_PROTO_VERSION_MINOR;
            info.fw_major = HARNESS_FW_VERSION_MAJOR;
            info.fw_minor = HARNESS_FW_VERSION_MINOR;
            info.fw_patch = HARNESS_FW_VERSION_PATCH;
            info.pin_count = (uint8_t)HARNESS_PIN_COUNT;
            info.caps = HARNESS_CAPS_ALL;
            info.pin_mask = HARNESS_PIN_MASK;
            info.reset_iface = HARNESS_ITF_RESET;

            pico_unique_board_id_t board_id;
            pico_get_unique_board_id(&board_id);
            memcpy(info.board_id, board_id.id, sizeof(info.board_id));

            memcpy(payload, &info, sizeof(info));
            payload_len = (uint16_t)sizeof(info);
            break;
        }

        case HARNESS_OP_GET_PIN: {
            if (len != 1u) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            const uint8_t index = body[0];
            if (!harness_gpio_index_valid(index)) {
                status = HARNESS_STATUS_ERR_BAD_PARAM;
                break;
            }
            harness_pin_state_t st;
            memset(&st, 0, sizeof(st));
            st.index = index;
            st.dir = harness_gpio_get_dir(index);
            st.level = harness_gpio_get_level(index);
            st.flags = harness_gpio_get_flags(index);
            st.drive = harness_gpio_get_drive(index);
            st.slew = harness_gpio_get_slew(index);
            memcpy(payload, &st, sizeof(st));
            payload_len = (uint16_t)sizeof(st);
            break;
        }

        case HARNESS_OP_CONFIGURE_PIN: {
            if (len != sizeof(harness_cfg_pin_t)) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            harness_cfg_pin_t cfg;
            memcpy(&cfg, body, sizeof(cfg));
            const int rc = harness_gpio_configure(cfg.index, cfg.dir, cfg.flags,
                                                  cfg.drive, cfg.slew, cfg.level);
            status = (uint8_t)rc;
            break;
        }

        case HARNESS_OP_SET_PIN: {
            if (len != sizeof(harness_set_pin_t)) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            harness_set_pin_t sp;
            memcpy(&sp, body, sizeof(sp));
            status = (uint8_t)harness_gpio_set(sp.index, sp.level);
            break;
        }

        case HARNESS_OP_SET_MASK: {
            if (len != sizeof(harness_set_mask_t)) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            harness_set_mask_t sm;
            memcpy(&sm, body, sizeof(sm));
            const uint32_t applied = harness_gpio_set_mask(sm.mask, sm.value);
            payload[0] = (uint8_t)(applied & 0xFFu);
            payload[1] = (uint8_t)((applied >> 8) & 0xFFu);
            payload[2] = (uint8_t)((applied >> 16) & 0xFFu);
            payload[3] = (uint8_t)((applied >> 24) & 0xFFu);
            payload_len = 4;
            break;
        }

        case HARNESS_OP_READ_ALL: {
            if (len != 0u) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            const uint32_t inputs = harness_gpio_read_all();
            payload[0] = (uint8_t)(inputs & 0xFFu);
            payload[1] = (uint8_t)((inputs >> 8) & 0xFFu);
            payload[2] = (uint8_t)((inputs >> 16) & 0xFFu);
            payload[3] = (uint8_t)((inputs >> 24) & 0xFFu);
            payload_len = 4;
            break;
        }

        case HARNESS_OP_SET_MANY: {
            if (len == 0u || (len % sizeof(harness_set_pin_t)) != 0u) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            const uint16_t count = (uint16_t)(len / sizeof(harness_set_pin_t));
            if (count > HARNESS_SET_MANY_MAX) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            /* Validate the whole batch before touching any line. */
            for (uint16_t i = 0; i < count; i++) {
                harness_set_pin_t sp;
                memcpy(&sp, body + (size_t)i * sizeof(sp), sizeof(sp));
                if (!harness_gpio_index_valid(sp.index) || sp.level > 1u ||
                    harness_gpio_get_dir(sp.index) != HARNESS_DIR_OUT) {
                    status = HARNESS_STATUS_ERR_BAD_PARAM;
                    break;
                }
            }
            if (status != HARNESS_STATUS_OK) {
                break;
            }
            for (uint16_t i = 0; i < count; i++) {
                harness_set_pin_t sp;
                memcpy(&sp, body + (size_t)i * sizeof(sp), sizeof(sp));
                harness_gpio_set(sp.index, sp.level);
            }
            payload[0] = (uint8_t)count;
            payload_len = 1;
            break;
        }

        case HARNESS_OP_CONFIGURE_MANY: {
            if (len == 0u || (len % sizeof(harness_cfg_pin_t)) != 0u) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            const uint16_t count = (uint16_t)(len / sizeof(harness_cfg_pin_t));
            if (count > HARNESS_CFG_MANY_MAX) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            /* Validate everything first so a bad element cannot leave a
             * half-applied configuration behind. */
            for (uint16_t i = 0; i < count; i++) {
                harness_cfg_pin_t cfg;
                memcpy(&cfg, body + (size_t)i * sizeof(cfg), sizeof(cfg));
                if (!harness_gpio_index_valid(cfg.index) || cfg.dir > HARNESS_DIR_OUT ||
                    (cfg.flags & (uint8_t)~HARNESS_FLAG_ALL) ||
                    (cfg.flags & HARNESS_FLAG_PULL_UP && cfg.flags & HARNESS_FLAG_PULL_DOWN) ||
                    cfg.drive > HARNESS_DRIVE_12MA || cfg.slew > HARNESS_SLEW_FAST ||
                    cfg.level > 1u ||
                    ((cfg.flags & HARNESS_FLAG_OPEN_DRAIN) && cfg.dir != HARNESS_DIR_OUT)) {
                    status = HARNESS_STATUS_ERR_BAD_PARAM;
                    break;
                }
            }
            if (status != HARNESS_STATUS_OK) {
                break;
            }
            for (uint16_t i = 0; i < count; i++) {
                harness_cfg_pin_t cfg;
                memcpy(&cfg, body + (size_t)i * sizeof(cfg), sizeof(cfg));
                harness_gpio_configure(cfg.index, cfg.dir, cfg.flags, cfg.drive,
                                       cfg.slew, cfg.level);
            }
            payload[0] = (uint8_t)count;
            payload_len = 1;
            break;
        }

        case HARNESS_OP_STOP: {
            if (len != 0u) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            harness_gpio_safe_state();
            break;
        }

        case HARNESS_OP_GET_STATUS: {
            if (len != 0u) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            harness_status_t st;
            harness_protocol_stats(&st);
            memcpy(payload, &st, sizeof(st));
            payload_len = (uint16_t)sizeof(st);
            break;
        }

        case HARNESS_OP_STREAM: {
            if (len < sizeof(harness_stream_hdr_t)) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            const uint32_t mask = rd32(body + 0);
            const uint32_t count = rd32(body + 4);
            const uint32_t mode = rd32(body + 8);
            if (mode != 0u || count == 0u || count > HARNESS_STREAM_MAX) {
                status = HARNESS_STATUS_ERR_BAD_PARAM;
                break;
            }
            if (len != (uint16_t)(sizeof(harness_stream_hdr_t) + count * 4u)) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            const uint32_t masked = mask & (uint32_t)HARNESS_PIN_MASK;
            if (masked == 0u) {
                status = HARNESS_STATUS_ERR_BAD_PARAM;
                break;
            }

            /* Copy into an aligned buffer: the receive accumulator gives no
             * alignment guarantee, and Cortex-M0+ cannot do unaligned words. */
            static uint32_t values[HARNESS_STREAM_MAX];
            for (uint32_t i = 0; i < count; i++) {
                values[i] = rd32(body + sizeof(harness_stream_hdr_t) + i * 4u);
            }

            uint32_t toggles = 0;
            const uint32_t t0 = time_us_32();
            const uint32_t applied = harness_gpio_stream(masked, values, count, &toggles);
            const uint32_t duration = time_us_32() - t0;

            harness_stream_rep_t sr;
            memset(&sr, 0, sizeof(sr));
            sr.applied = applied;
            sr.duration_us = duration;
            sr.toggles = toggles;
            memcpy(payload, &sr, sizeof(sr));
            payload_len = (uint16_t)sizeof(sr);
            break;
        }

        case HARNESS_OP_PING: {
            if (len != 4u) {
                status = HARNESS_STATUS_ERR_BAD_LENGTH;
                break;
            }
            memcpy(payload, body, 4);
            payload_len = 4;
            break;
        }

        default:
            status = HARNESS_STATUS_ERR_UNKNOWN_OPCODE;
            break;
    }

    if (status == HARNESS_STATUS_OK) {
        s_frames_ok++;
    } else {
        s_frames_err++;
        payload_len = 0;
    }
    s_last_status = status;

    build_reply(rep, status, seq, payload_len ? payload : NULL, payload_len);
    *rep_len = (uint16_t)(HARNESS_HDR_LEN + payload_len);
}

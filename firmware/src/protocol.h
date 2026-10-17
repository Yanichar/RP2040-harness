/*
 * RP2040 Harness — wire protocol codec and request dispatcher.
 *
 * Implements docs/protocol.md v1.0. The document is authoritative; if this file
 * and the document disagree, this file is wrong.
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef HARNESS_PROTOCOL_H
#define HARNESS_PROTOCOL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "harness_status.h"

#if defined(__GNUC__)
#define HARNESS_PACKED __attribute__((packed))
#else
#define HARNESS_PACKED
#endif

/* Framing (docs/protocol.md §2). */
#define HARNESS_REQ_MAGIC    0xA5u
#define HARNESS_REP_MAGIC    0x5Au
#define HARNESS_HDR_LEN      8u
#define HARNESS_MAX_PAYLOAD  1024u
#define HARNESS_MAX_FRAME    (HARNESS_HDR_LEN + HARNESS_MAX_PAYLOAD)

/* Opcodes (docs/protocol.md §4). */
#define HARNESS_OP_GET_INFO      0x01u
#define HARNESS_OP_GET_PIN       0x02u
#define HARNESS_OP_CONFIGURE_PIN 0x03u
#define HARNESS_OP_SET_PIN       0x04u
#define HARNESS_OP_SET_MASK      0x05u
#define HARNESS_OP_READ_ALL      0x06u
#define HARNESS_OP_SET_MANY      0x07u
#define HARNESS_OP_CONFIGURE_MANY 0x08u
#define HARNESS_OP_STOP          0x09u
#define HARNESS_OP_GET_STATUS    0x0Au
#define HARNESS_OP_STREAM        0x10u
#define HARNESS_OP_PING          0x7Fu

/* Caps bits (docs/protocol.md §4.2). */
#define HARNESS_CAP_PIN_DIR        (1u << 0)
#define HARNESS_CAP_PULL           (1u << 1)
#define HARNESS_CAP_OPEN_DRAIN     (1u << 2)
#define HARNESS_CAP_DRIVE_STRENGTH (1u << 3)
#define HARNESS_CAP_SLEW           (1u << 4)
#define HARNESS_CAP_SCHMITT        (1u << 5)
#define HARNESS_CAP_ATOMIC_MASK    (1u << 6)
#define HARNESS_CAP_STREAM         (1u << 7)
#define HARNESS_CAP_BATCH          (1u << 8)
#define HARNESS_CAP_RESET_IFACE    (1u << 9)
#define HARNESS_CAPS_ALL           (0x3FFu)

/* Batch limits derived from the 1024-byte payload cap. */
#define HARNESS_SET_MANY_MAX 64u  /* 64 * 4  = 256 bytes */
#define HARNESS_CFG_MANY_MAX 32u  /* 32 * 8  = 256 bytes */
#define HARNESS_STREAM_MAX   250u /* 16 + 250*4 = 1016 bytes */

/* -------------------------------------------------------------------------- */
/* Wire structures — little-endian, packed                                    */
/* -------------------------------------------------------------------------- */

typedef struct HARNESS_PACKED {
    uint8_t magic;
    uint8_t opcode;
    uint16_t seq;
    uint16_t len;
    uint16_t crc;
} harness_req_hdr_t;

typedef struct HARNESS_PACKED {
    uint8_t magic;
    uint8_t status;
    uint16_t seq;
    uint16_t len;
    uint16_t crc;
} harness_rep_hdr_t;

typedef struct HARNESS_PACKED {
    uint8_t proto_major;
    uint8_t proto_minor;
    uint8_t fw_major;
    uint8_t fw_minor;
    uint8_t fw_patch;
    uint8_t pin_count;
    uint16_t reserved0;
    uint32_t caps;
    uint32_t pin_mask;
    uint8_t board_id[8];
    uint32_t reset_iface;
    uint32_t reserved1;
} harness_info_t;

typedef struct HARNESS_PACKED {
    uint8_t index;
    uint8_t dir;
    uint8_t level;
    uint8_t flags;
    uint8_t drive;
    uint8_t slew;
    uint16_t reserved;
} harness_pin_state_t;

typedef struct HARNESS_PACKED {
    uint8_t index;
    uint8_t dir;
    uint8_t flags;
    uint8_t drive;
    uint8_t slew;
    uint8_t level;
    uint16_t reserved;
} harness_cfg_pin_t;

typedef struct HARNESS_PACKED {
    uint8_t index;
    uint8_t level;
    uint16_t reserved;
} harness_set_pin_t;

typedef struct HARNESS_PACKED {
    uint32_t mask;
    uint32_t value;
} harness_set_mask_t;

typedef struct HARNESS_PACKED {
    uint32_t mask;
    uint32_t count;
    uint32_t mode;
    uint32_t reserved;
} harness_stream_hdr_t;

typedef struct HARNESS_PACKED {
    uint32_t applied;
    uint32_t duration_us;
    uint32_t toggles;
    uint32_t reserved;
} harness_stream_rep_t;

typedef struct HARNESS_PACKED {
    uint8_t state;
    uint8_t last_status;
    uint16_t reserved0;
    uint32_t frames_ok;
    uint32_t frames_err;
    uint32_t crc_errors;
    uint32_t uptime_ms;
    uint32_t reserved1;
} harness_status_t;

_Static_assert(sizeof(harness_req_hdr_t) == 8, "req header must be 8 bytes");
_Static_assert(sizeof(harness_rep_hdr_t) == 8, "rep header must be 8 bytes");
_Static_assert(sizeof(harness_info_t) == 32, "info_t must be 32 bytes");
_Static_assert(sizeof(harness_pin_state_t) == 8, "pin_state_t must be 8 bytes");
_Static_assert(sizeof(harness_cfg_pin_t) == 8, "cfg_pin_t must be 8 bytes");
_Static_assert(sizeof(harness_set_pin_t) == 4, "set_pin_t must be 4 bytes");
_Static_assert(sizeof(harness_set_mask_t) == 8, "set_mask_t must be 8 bytes");
_Static_assert(sizeof(harness_stream_hdr_t) == 16, "stream_hdr_t must be 16 bytes");
_Static_assert(sizeof(harness_stream_rep_t) == 16, "stream_rep_t must be 16 bytes");
_Static_assert(sizeof(harness_status_t) == 24, "status_t must be 24 bytes");

/* -------------------------------------------------------------------------- */
/* Receive accumulator                                                        */
/* -------------------------------------------------------------------------- */

typedef struct {
    uint8_t buf[HARNESS_MAX_FRAME + 64];
    uint16_t have;
    uint8_t last_error;
} harness_rx_t;

typedef enum {
    HARNESS_RX_NONE = 0, /* need more bytes */
    HARNESS_RX_FRAME,    /* a plausible, complete frame sits at rx->buf[0] */
    HARNESS_RX_CORRUPT,  /* bad magic / oversize length; bytes were discarded */
} harness_rx_result_t;

void harness_rx_init(harness_rx_t *rx);
void harness_rx_push(harness_rx_t *rx, const uint8_t *data, uint16_t len);
harness_rx_result_t harness_rx_poll(harness_rx_t *rx, uint16_t *frame_len);
void harness_rx_consume(harness_rx_t *rx, uint16_t frame_len);

/* -------------------------------------------------------------------------- */
/* Dispatch                                                                   */
/* -------------------------------------------------------------------------- */

void harness_protocol_init(void);

/*
 * Handle one complete request frame and render the reply frame.
 * Always writes a reply (possibly an error one). `rep_cap` must be at least
 * HARNESS_HDR_LEN + 32.
 */
void harness_protocol_handle(const uint8_t *req, uint16_t req_len,
                             uint8_t *rep, uint16_t rep_cap, uint16_t *rep_len);

/* Render a bare error reply (used when framing itself cannot be recovered). */
void harness_protocol_error_reply(uint8_t *rep, uint8_t status, uint16_t seq,
                                  uint16_t *rep_len);

/* Fill a status_t for GET_STATUS. */
void harness_protocol_stats(harness_status_t *out);

#endif /* HARNESS_PROTOCOL_H */

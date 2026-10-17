/*
 * harness_fx2.c - CY7C68013A (FX2LP) 24-line harness firmware.
 *
 * This is the Cypress sibling of the RP2040 harness firmware: it exposes
 * PORTA[0..7], PORTB[0..7] and PORTD[0..7] as 24 controllable digital lines
 * and speaks the FROZEN wire protocol v1.0 (docs/protocol.md) byte for byte,
 * so the very same Python API drives both boards.
 *
 * WHY EP0 ONLY
 * ------------
 * The device has no bulk endpoints at all (see src/dscr.a51): the whole
 * interface is built from vendor control transfers on endpoint zero.  A
 * protocol frame is up to 1032 bytes, far more than one 64-byte EP0 packet,
 * so the frame is moved with a small chunked transport implemented here and
 * in ../host/src/fx2_harness/transport.py (README.md, "Chunked EP0
 * transport"):
 *
 *   bmRequestType  bRequest  wValue          wIndex      data stage
 *   0x40           0x20      byte offset     byte count  OUT: frame bytes
 *   0x40           0x21      total length    0           none; run the frame
 *   0xC0           0x22      0               0           IN: 2 = reply length
 *   0xC0           0x23      byte offset     byte count  IN: reply bytes
 *
 * Diagnostic extensions (not part of the protocol, used by the CLI/loader):
 *
 *   0x40           0x28      -               -           hand USB back to the
 *                                                        EZ-USB core (04B4:8613)
 *   0xC0           0x29      -               -           IN 16 bytes of state
 *
 * THE OUT DATA STAGE
 * ------------------
 * fx2lib's handle_setupdata() calls handle_vendorcommand() while the setup
 * packet is still the most recent USB event: the data stage of a control-OUT
 * transfer has NOT arrived yet.  A request whose payload travels in the data
 * stage must therefore start it explicitly and wait for it.  Per the TRM
 * ("Endpoint Zero"), writing the transfer length to EP0BCH:EP0BCL arms EP0 for
 * the data stage; EP0CS bit 1 (bmEPBUSY) is set until the packet has been
 * received, after which EP0BCH:EP0BCL holds the received byte count and the
 * bytes are in EP0BUF[].  Only then may the status stage be acknowledged, and
 * handle_setupdata() does that for us (EP0CS |= bmHSNAK) after we return TRUE.
 *
 * MEMORY MAP (see build.ps1 linker flags)
 *   0x0000-0x2FFF  code
 *   0x3000-0x3DFF  xdata: rx_frame, tx_frame (2 x 1032 bytes) and state
 *   0x3E00         DSCR_AREA (descriptors, read by the EZ-USB core)
 *   0x3F00         INT2JT   (fx2lib autovector jump table)
 */

#include <fx2regs.h>
#include <fx2macros.h>
#include <autovector.h>
#include <setupdat.h>
#include <eputils.h>
#include <delay.h>

/* ------------------------------------------------ vendor request codes ---- */

#define REQ_OUT_CHUNK       0x20
#define REQ_EXEC            0x21
#define REQ_REPLY_LEN       0x22
#define REQ_IN_CHUNK        0x23
#define REQ_RESET_TO_BOOT   0x28
#define REQ_DEBUG           0x29

#define EP0_CHUNK           64          /* EP0 max packet size */

/* ---------------------------------------- protocol v1.0 (docs/protocol.md) - */

#define REQ_MAGIC           0xA5
#define REP_MAGIC           0x5A
#define PROTO_HDR           8
#define PROTO_MAX_PAYLOAD   1024
#define FRAME_MAX           (PROTO_HDR + PROTO_MAX_PAYLOAD)     /* 1032 */

#define ST_OK               0x00
#define ST_ERR_UNKNOWN_OPCODE 0x01
#define ST_ERR_BAD_LENGTH   0x02
#define ST_ERR_BAD_PARAM    0x03
#define ST_ERR_BUSY         0x04
#define ST_ERR_CRC          0x05
#define ST_ERR_MAGIC        0x06

#define OP_GET_INFO         0x01
#define OP_GET_PIN          0x02
#define OP_CONFIGURE_PIN    0x03
#define OP_SET_PIN          0x04
#define OP_SET_MASK         0x05
#define OP_READ_ALL         0x06
#define OP_SET_MANY         0x07
#define OP_CONFIGURE_MANY   0x08
#define OP_STOP             0x09
#define OP_GET_STATUS       0x0A
#define OP_STREAM           0x10
#define OP_PING             0x7F

#define PIN_COUNT           24
#define ALL_PINS_MASK       0x00FFFFFFUL

#define SET_MANY_MAX        64
#define CONFIGURE_MANY_MAX  32
#define STREAM_COUNT_MAX    250

/* §4.2 caps: only what this hardware really has.  The FX2LP port pins have
 * no pull-up/pull-down, no open-drain mode, no drive-strength or slew
 * control and no Schmitt-trigger switch, so those bits are clear. */
#define CAP_PIN_DIR         (1UL << 0)
#define CAP_PULL            (1UL << 1)
#define CAP_OPEN_DRAIN      (1UL << 2)
#define CAP_DRIVE_STRENGTH  (1UL << 3)
#define CAP_SLEW            (1UL << 4)
#define CAP_SCHMITT         (1UL << 5)
#define CAP_ATOMIC_MASK     (1UL << 6)
#define CAP_STREAM          (1UL << 7)
#define CAP_BATCH           (1UL << 8)
#define CAP_RESET_IFACE     (1UL << 9)

#define CAPS_USED (CAP_PIN_DIR | CAP_ATOMIC_MASK | CAP_STREAM | CAP_BATCH)

/* §4.5 flags bits.  Stored and read back verbatim; none of them changes the
 * hardware, because the FX2LP pins have no such controls. */
#define FLAG_PULL_UP        0x01
#define FLAG_PULL_DOWN      0x02
#define FLAG_OPEN_DRAIN     0x04
#define FLAG_SCHMITT        0x08
#define FLAG_MASK           0x0F

#define DIR_INPUT           0
#define DIR_OUTPUT          1

/* Firmware version reported in info_t. */
#define FW_MAJOR            1
#define FW_MINOR            0
#define FW_PATCH            0

/*! board_id (info_t bytes 16..23, little endian).
 *
 * The FX2LP has no unique silicon id that is readable without touching an
 * external EEPROM, so this is a stable *model* id, not a per-board serial:
 * the ASCII bytes "FX2LPHN1" ("FX2LP harness, hardware revision 1"), stored
 * little-endian as 0x4658324C50484E31. */
static const BYTE __code board_id[8] = {
    0x31, 0x4E, 0x48, 0x50, 0x4C, 0x32, 0x58, 0x46
};

/* ------------------------------------------------------- interrupt numbers */
/* fx2lib declares these as an enum, but SDCC 4.x only accepts a numeric
 * constant inside __interrupt().  Order per fx2lib/include/autovector.h. */
#define ISR_SUDAV           13
#define ISR_SOF             14
#define ISR_USBRESET        17
#define ISR_HISPEED         18

/* ------------------------------------------------------------------ state - */

__xdata BYTE rx_frame[FRAME_MAX];       /* frame being assembled by the host */
__xdata BYTE tx_frame[FRAME_MAX];       /* reply frame being built           */
__xdata WORD tx_len;                    /* reply frame length in bytes       */

__xdata BYTE pin_flags[PIN_COUNT];      /* §4.5, stored verbatim             */
__xdata BYTE pin_drive[PIN_COUNT];      /* §4.6, stored verbatim             */
__xdata BYTE pin_slew[PIN_COUNT];       /* 0 = slow, 1 = fast, stored        */
__xdata BYTE out_latch[3];              /* shadow of IOA/IOB/IOD latches     */

__xdata DWORD frames_ok;
__xdata DWORD frames_err;
__xdata DWORD crc_errors;
__xdata DWORD sof_count;                /* 1 per SOF packet (see uptime_ms)  */
__xdata WORD  ticks_per_ms;             /* Timer 0 ticks in 1 ms, calibrated */
__xdata BYTE  last_status;
__xdata BYTE  streaming;
__xdata BYTE  err_count;                /* transport-level errors (debug)    */
__xdata WORD  g_plen;                   /* payload length of the reply       */

volatile __bit got_sud;
volatile __bit got_reset;

/* -------------------------------------------------------------- tuning --- */

/* Timer 0 counts machine cycles.  Per the FX2 TRM (quoted in fx2lib's
 * delay.c) one machine cycle is 12 crystal pulses = 83.3 ns at 48 MHz, i.e.
 * 12 MHz / 12000 ticks per millisecond.  timer_calibrate() measures the real
 * value against the USB SOF (1 ms) and only this default is used if the bus
 * never shows a SOF. */
#define TICKS_PER_MS_DEFAULT 12000u

/* ------------------------------------------------------- payload writers - */

#define PUT8(i, v)   (tx_frame[PROTO_HDR + (i)] = (BYTE)(v))
#define PUT16(i, v)  do { \
        PUT8((i),     (WORD)(v) & 0xFF); \
        PUT8((i) + 1, ((WORD)(v) >> 8) & 0xFF); \
    } while (0)
#define PUT32(i, v)  do { \
        PUT8((i),     (DWORD)(v) & 0xFF); \
        PUT8((i) + 1, ((DWORD)(v) >> 8) & 0xFF); \
        PUT8((i) + 2, ((DWORD)(v) >> 16) & 0xFF); \
        PUT8((i) + 3, ((DWORD)(v) >> 24) & 0xFF); \
    } while (0)

/* Little-endian reader over the request frame. */
static DWORD rd32(WORD off)
{
    return (DWORD)rx_frame[off]
         | ((DWORD)rx_frame[off + 1] << 8)
         | ((DWORD)rx_frame[off + 2] << 16)
         | ((DWORD)rx_frame[off + 3] << 24);
}

/* ----------------------------------------------------------- port access - */
/* Logical harness index 0..7 -> PA0..PA7, 8..15 -> PB0..PB7,
 * 16..23 -> PD0..PD7 (docs/pinmap.md). */

static BYTE port_out_get(BYTE p)
{
    switch (p) {
    case 0:  return IOA;
    case 1:  return IOB;
    default: return IOD;
    }
}

static void port_out_set(BYTE p, BYTE v)
{
    switch (p) {
    case 0:  IOA = v; break;
    case 1:  IOB = v; break;
    default: IOD = v; break;
    }
}

static BYTE port_dir_get(BYTE p)
{
    switch (p) {
    case 0:  return OEA;
    case 1:  return OEB;
    default: return OED;
    }
}

static void port_dir_set(BYTE p, BYTE v)
{
    switch (p) {
    case 0:  OEA = v; break;
    case 1:  OEB = v; break;
    default: OED = v; break;
    }
}

/* Current pad levels of all 24 lines as a 24-bit mask (§4.9). */
static DWORD pads_read_all(void)
{
    return (DWORD)IOA | ((DWORD)IOB << 8) | ((DWORD)IOD << 16);
}

/* Current output-latch state as a 24-bit mask (§4.8 reply). */
static DWORD latches_read_all(void)
{
    return (DWORD)out_latch[0] | ((DWORD)out_latch[1] << 8)
         | ((DWORD)out_latch[2] << 16);
}

/* Write a shadow latch to the hardware.  One store: atomic per port. */
static void latch_commit(BYTE p)
{
    port_out_set(p, out_latch[p]);
}

static BYTE popcount8(BYTE v)
{
    BYTE c = 0;

    while (v) {
        c += (BYTE)(v & 1);
        v >>= 1;
    }
    return c;
}

/* ---------------------------------------------------------------- time --- */

static WORD timer_read(void)
{
    BYTE lo, hi, lo2;

    /* Read TL0 around TH0 and retry only if TL0 *wrapped* in between.  A
     * plain "retry until two TL0 reads match" loop never terminates: TL0
     * advances every machine cycle (83 ns at 48 MHz), i.e. faster than the
     * loop body runs. */
    for (;;) {
        lo  = TL0;
        hi  = TH0;
        lo2 = TL0;
        if (lo2 >= lo) {
            return ((WORD)hi << 8) | lo2;
        }
    }
}

static void timer_init(void)
{
    TMOD = (TMOD & 0xF0) | 0x01;        /* Timer 0, mode 1: 16-bit counter  */
    TL0 = 0;
    TH0 = 0;
    TCON |= 0x10;                       /* TR0 = 1, free running            */
}

/*! Measure Timer 0 against the USB SOF and remember ticks per millisecond.
 *
 * In full speed a SOF packet arrives every 1 ms; in high speed every 125 us
 * (8 per millisecond).  The measurement spans 4 ms so that the interrupt
 * latency of a single SOF does not dominate the result.  If the bus is silent
 * (no SOF within a few milliseconds) the hard-coded TRM value stays in place.
 */
static void timer_calibrate(void)
{
    WORD t0, t1, delta;
    BYTE need, guard, sofs_per_ms;
    WORD span_ms;
    DWORD last;

    if (ticks_per_ms != 0) {
        return;                         /* already calibrated */
    }
    ticks_per_ms = TICKS_PER_MS_DEFAULT;

    /* SOF packets in one millisecond: 8 at high speed, 1 at full speed. */
    sofs_per_ms = HISPEED ? 8 : 1;
    span_ms = 4;                        /* measurement window                 */
    need = (BYTE)(sofs_per_ms * span_ms);

    /* Wait for the next SOF edge (up to ~40 requested ms). */
    last = sof_count;
    guard = 0;
    while (sof_count == last) {
        if (guard >= 40) {
            return;                     /* no SOF: keep the default */
        }
        delay(1);
        guard++;
    }

    last = sof_count;
    t0 = timer_read();
    for (;;) {
        if (sof_count != last) {
            last = sof_count;
            need--;
            if (need == 0) {
                break;
            }
        }
        /* Give up (instead of hanging or reading a wrapped timer) if the bus
         * goes quiet mid-measurement. */
        if ((WORD)(timer_read() - t0) > 50000u) {
            return;
        }
    }
    t1 = timer_read();
    delta = (WORD)(t1 - t0);
    if (delta != 0) {
        ticks_per_ms = (WORD)(delta / span_ms);
    }
}

static DWORD duration_us(WORD ticks)
{
    return ((DWORD)ticks * 1000UL) / ticks_per_ms;
}

static DWORD uptime_ms(void)
{
    return HISPEED ? (sof_count >> 3) : sof_count;
}

/* ------------------------------------------------------------------ CRC --- */

/*! CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection).
 * One pass over a buffer; call repeatedly to continue over disjoint ranges. */
static WORD crc16_step(WORD crc, BYTE __xdata *p, WORD len)
{
    BYTE b, i;

    while (len--) {
        b = *p++;
        crc ^= (WORD)b << 8;
        for (i = 0; i < 8; i++) {
            if (crc & 0x8000) {
                crc = (WORD)((crc << 1) ^ 0x1021);
            } else {
                crc = (WORD)(crc << 1);
            }
        }
    }
    return crc;
}

/* -------------------------------------------------------------- replies --- */

static void reply_frame(BYTE status, WORD seq, WORD len)
{
    WORD crc;

    tx_frame[0] = REP_MAGIC;
    tx_frame[1] = status;
    tx_frame[2] = (BYTE)(seq & 0xFF);
    tx_frame[3] = (BYTE)(seq >> 8);
    tx_frame[4] = (BYTE)(len & 0xFF);
    tx_frame[5] = (BYTE)(len >> 8);
    crc = crc16_step(0xFFFF, &tx_frame[1], 5);
    crc = crc16_step(crc, &tx_frame[PROTO_HDR], len);
    tx_frame[6] = (BYTE)(crc & 0xFF);
    tx_frame[7] = (BYTE)(crc >> 8);
    tx_len = (WORD)(PROTO_HDR + len);
    last_status = status;
}

static void reply_status(BYTE status, WORD seq)
{
    reply_frame(status, seq, 0);
}

/* ------------------------------------------------------ pin configuration - */

/*! Validate one cfg_pin_t (8 bytes at rx_frame[off]); returns a status code. */
static BYTE cfg_validate(WORD off)
{
    BYTE index = rx_frame[off];
    BYTE dir   = rx_frame[off + 1];
    BYTE flags = rx_frame[off + 2];
    BYTE drive = rx_frame[off + 3];
    BYTE slew  = rx_frame[off + 4];
    BYTE level = rx_frame[off + 5];

    if (index >= PIN_COUNT)                 return ST_ERR_BAD_PARAM;
    if (dir != DIR_INPUT && dir != DIR_OUTPUT) return ST_ERR_BAD_PARAM;
    if (flags & (BYTE)~FLAG_MASK)           return ST_ERR_BAD_PARAM;
    if ((flags & FLAG_PULL_UP) && (flags & FLAG_PULL_DOWN))
                                            return ST_ERR_BAD_PARAM;
    if ((flags & FLAG_OPEN_DRAIN) && dir != DIR_OUTPUT)
                                            return ST_ERR_BAD_PARAM;
    if (drive > 3)                          return ST_ERR_BAD_PARAM;
    if (slew > 1)                           return ST_ERR_BAD_PARAM;
    if (level > 1)                          return ST_ERR_BAD_PARAM;
    return ST_OK;
}

/*! Apply one validated cfg_pin_t.  §4.4 semantics: for an INPUT line the
 * drive/slew fields are validated but ignored (the stored values stay) and
 * the output latch is forced low; flags are stored in every case. */
static void cfg_apply(WORD off)
{
    BYTE index = rx_frame[off];
    BYTE dir   = rx_frame[off + 1];
    BYTE flags = rx_frame[off + 2];
    BYTE p     = (BYTE)(index >> 3);
    BYTE bit   = (BYTE)(1 << (index & 7));

    pin_flags[index] = flags;

    if (dir == DIR_OUTPUT) {
        pin_drive[index] = rx_frame[off + 3];
        pin_slew[index]  = rx_frame[off + 4];
        if (rx_frame[off + 5]) {
            out_latch[p] |= bit;
        } else {
            out_latch[p] &= (BYTE)~bit;
        }
        latch_commit(p);                /* level first, then drive: no glitch */
        port_dir_set(p, (BYTE)(port_dir_get(p) | bit));
    } else {
        port_dir_set(p, (BYTE)(port_dir_get(p) & ~bit));
        out_latch[p] &= (BYTE)~bit;
        latch_commit(p);
    }
}

/* --------------------------------------------------------------- opcodes - */

static BYTE op_get_info(WORD n)
{
    BYTE i;

    if (n != 0) {
        return ST_ERR_BAD_LENGTH;
    }
    PUT8(0, 1);                         /* proto_major                      */
    PUT8(1, 0);                         /* proto_minor                      */
    PUT8(2, FW_MAJOR);
    PUT8(3, FW_MINOR);
    PUT8(4, FW_PATCH);
    PUT8(5, PIN_COUNT);
    PUT16(6, 0);                        /* reserved                         */
    PUT32(8, CAPS_USED);
    PUT32(12, ALL_PINS_MASK);
    for (i = 0; i < 8; i++) {
        PUT8(16 + i, board_id[i]);
    }
    PUT32(24, 0);                       /* reset_iface: we have none        */
    PUT32(28, 0);                       /* reserved                         */
    g_plen = 32;
    return ST_OK;
}

static BYTE op_get_pin(WORD n)
{
    BYTE index, p, bit, dir, level;

    if (n != 1) {
        return ST_ERR_BAD_LENGTH;
    }
    index = rx_frame[PROTO_HDR];
    if (index >= PIN_COUNT) {
        return ST_ERR_BAD_PARAM;
    }
    p    = (BYTE)(index >> 3);
    bit  = (BYTE)(1 << (index & 7));
    dir  = (port_dir_get(p) & bit) ? DIR_OUTPUT : DIR_INPUT;
    level = (port_out_get(p) & bit) ? 1 : 0;   /* pad level, §4.9 style    */

    PUT8(0, index);
    PUT8(1, dir);
    PUT8(2, level);
    PUT8(3, pin_flags[index]);
    PUT8(4, pin_drive[index]);
    PUT8(5, pin_slew[index]);
    PUT16(6, 0);                        /* reserved                         */
    g_plen = 8;
    return ST_OK;
}

static BYTE op_configure_pin(WORD n)
{
    BYTE status;

    if (n != 8) {
        return ST_ERR_BAD_LENGTH;
    }
    status = cfg_validate(PROTO_HDR);
    if (status != ST_OK) {
        return status;
    }
    cfg_apply(PROTO_HDR);
    g_plen = 0;
    return ST_OK;
}

static BYTE op_set_pin(WORD n)
{
    BYTE index, level, p, bit;

    if (n != 4) {
        return ST_ERR_BAD_LENGTH;
    }
    index = rx_frame[PROTO_HDR];
    level = rx_frame[PROTO_HDR + 1];
    if (index >= PIN_COUNT || level > 1) {
        return ST_ERR_BAD_PARAM;
    }
    p   = (BYTE)(index >> 3);
    bit = (BYTE)(1 << (index & 7));
    if (!(port_dir_get(p) & bit)) {
        return ST_ERR_BAD_PARAM;        /* §4.7: pin must already be OUTPUT  */
    }
    if (level) {
        out_latch[p] |= bit;
    } else {
        out_latch[p] &= (BYTE)~bit;
    }
    latch_commit(p);
    g_plen = 0;
    return ST_OK;
}

static BYTE op_set_mask(WORD n)
{
    DWORD mask, value;

    if (n != 8) {
        return ST_ERR_BAD_LENGTH;
    }
    mask  = rd32(PROTO_HDR) & ALL_PINS_MASK;
    value = rd32(PROTO_HDR + 4) & ALL_PINS_MASK;

    /* Three stores, one per port: each port changes atomically (§4.8). */
    out_latch[0] = (BYTE)((out_latch[0] & ~(BYTE)mask)
                          | ((BYTE)value & (BYTE)mask));
    latch_commit(0);
    out_latch[1] = (BYTE)((out_latch[1] & ~(BYTE)(mask >> 8))
                          | ((BYTE)(value >> 8) & (BYTE)(mask >> 8)));
    latch_commit(1);
    out_latch[2] = (BYTE)((out_latch[2] & ~(BYTE)(mask >> 16))
                          | ((BYTE)(value >> 16) & (BYTE)(mask >> 16)));
    latch_commit(2);

    PUT32(0, latches_read_all());
    g_plen = 4;
    return ST_OK;
}

static BYTE op_read_all(WORD n)
{
    if (n != 0) {
        return ST_ERR_BAD_LENGTH;
    }
    PUT32(0, pads_read_all());
    g_plen = 4;
    return ST_OK;
}

static BYTE op_set_many(WORD n)
{
    WORD i, off;
    BYTE count;

    if (n == 0 || (n & 3) != 0) {
        return ST_ERR_BAD_LENGTH;
    }
    count = (BYTE)(n >> 2);
    if (count < 1 || count > SET_MANY_MAX) {
        return ST_ERR_BAD_LENGTH;
    }
    /* Validate everything before touching a single line. */
    for (i = 0; i < count; i++) {
        off = (WORD)(PROTO_HDR + 4 * i);
        if (rx_frame[off] >= PIN_COUNT || rx_frame[off + 1] > 1) {
            return ST_ERR_BAD_PARAM;
        }
        if (!(port_dir_get((BYTE)(rx_frame[off] >> 3))
              & (BYTE)(1 << (rx_frame[off] & 7)))) {
            return ST_ERR_BAD_PARAM;
        }
    }
    for (i = 0; i < count; i++) {
        BYTE index, p, bit;

        off   = (WORD)(PROTO_HDR + 4 * i);
        index = rx_frame[off];
        p     = (BYTE)(index >> 3);
        bit   = (BYTE)(1 << (index & 7));
        if (rx_frame[off + 1]) {
            out_latch[p] |= bit;
        } else {
            out_latch[p] &= (BYTE)~bit;
        }
        latch_commit(p);
    }
    PUT8(0, count);
    g_plen = 1;
    return ST_OK;
}

static BYTE op_configure_many(WORD n)
{
    WORD i, off;
    BYTE count, status;

    if (n == 0 || (n & 7) != 0) {
        return ST_ERR_BAD_LENGTH;
    }
    count = (BYTE)(n >> 3);
    if (count < 1 || count > CONFIGURE_MANY_MAX) {
        return ST_ERR_BAD_LENGTH;
    }
    for (i = 0; i < count; i++) {
        status = cfg_validate((WORD)(PROTO_HDR + 8 * i));
        if (status != ST_OK) {
            return status;
        }
    }
    for (i = 0; i < count; i++) {
        cfg_apply((WORD)(PROTO_HDR + 8 * i));
    }
    PUT8(0, count);
    g_plen = 1;
    return ST_OK;
}

static BYTE op_stop(WORD n)
{
    BYTE i;

    if (n != 0) {
        return ST_ERR_BAD_LENGTH;
    }
    streaming = 0;
    for (i = 0; i < 3; i++) {
        port_dir_set(i, 0x00);          /* all INPUT                          */
        out_latch[i] = 0x00;
        latch_commit(i);
    }
    for (i = 0; i < PIN_COUNT; i++) {
        pin_flags[i] = FLAG_SCHMITT;    /* RP2040 boot state, §5.1            */
        pin_drive[i] = 1;               /* 4 mA                               */
        pin_slew[i]  = 0;               /* slow                               */
    }
    g_plen = 0;
    return ST_OK;
}

static BYTE op_get_status(WORD n)
{
    if (n != 0) {
        return ST_ERR_BAD_LENGTH;
    }
    PUT8(0, streaming ? 1 : 0);
    PUT8(1, last_status);
    PUT16(2, 0);
    PUT32(4, frames_ok);
    PUT32(8, frames_err);
    PUT32(12, crc_errors);
    PUT32(16, uptime_ms());
    PUT32(20, 0);
    g_plen = 24;
    return ST_OK;
}

static BYTE op_stream(WORD n)
{
    DWORD mask, count32, mode;
    WORD i, off, t_start, ticks, toggles = 0;
    BYTE count, a, b, d, m0, m1, m2, v0, v1, v2, na, nb, nd;

    if (n < 16) {
        return ST_ERR_BAD_LENGTH;
    }
    mask    = rd32(PROTO_HDR) & ALL_PINS_MASK;
    count32 = rd32(PROTO_HDR + 4);
    mode    = rd32(PROTO_HDR + 8);

    if (mode != 0 || count32 == 0 || count32 > STREAM_COUNT_MAX) {
        return ST_ERR_BAD_PARAM;
    }
    if (n != (WORD)(16 + 4 * count32)) {
        return ST_ERR_BAD_LENGTH;
    }
    if (mask == 0) {
        return ST_ERR_BAD_PARAM;
    }

    timer_calibrate();

    count = (BYTE)count32;
    m0 = (BYTE)mask;
    m1 = (BYTE)(mask >> 8);
    m2 = (BYTE)(mask >> 16);
    a = out_latch[0];
    b = out_latch[1];
    d = out_latch[2];

    t_start = timer_read();
    off = (WORD)(PROTO_HDR + 16);
    for (i = 0; i < count; i++) {
        /* The low three bytes are all that matter: the mask has 24 bits. */
        v0 = rx_frame[off];
        v1 = rx_frame[off + 1];
        v2 = rx_frame[off + 2];
        off = (WORD)(off + 4);

        na = (BYTE)((a & ~m0) | (v0 & m0));
        nb = (BYTE)((b & ~m1) | (v1 & m1));
        nd = (BYTE)((d & ~m2) | (v2 & m2));

        if (na != a) {
            toggles = (WORD)(toggles + popcount8((BYTE)(na ^ a)));
            a = na;
        }
        if (nb != b) {
            toggles = (WORD)(toggles + popcount8((BYTE)(nb ^ b)));
            b = nb;
        }
        if (nd != d) {
            toggles = (WORD)(toggles + popcount8((BYTE)(nd ^ d)));
            d = nd;
        }
        /* One store per port, back to back: the closest thing to an atomic
         * 24-bit write this chip has (the ports are separate SFRs). */
        IOA = a;
        IOB = b;
        IOD = d;
    }
    ticks = (WORD)(timer_read() - t_start);

    out_latch[0] = a;
    out_latch[1] = b;
    out_latch[2] = d;

    streaming = 0;
    PUT32(0, (DWORD)count);
    PUT32(4, duration_us(ticks));
    PUT32(8, (DWORD)toggles);
    PUT32(12, 0);
    g_plen = 16;
    return ST_OK;
}

static BYTE op_ping(WORD n)
{
    if (n != 4) {
        return ST_ERR_BAD_LENGTH;
    }
    PUT8(0, rx_frame[PROTO_HDR]);
    PUT8(1, rx_frame[PROTO_HDR + 1]);
    PUT8(2, rx_frame[PROTO_HDR + 2]);
    PUT8(3, rx_frame[PROTO_HDR + 3]);
    g_plen = 4;
    return ST_OK;
}

static BYTE opcode_dispatch(BYTE opcode, WORD n)
{
    switch (opcode) {
    case OP_GET_INFO:       return op_get_info(n);
    case OP_GET_PIN:        return op_get_pin(n);
    case OP_CONFIGURE_PIN:  return op_configure_pin(n);
    case OP_SET_PIN:        return op_set_pin(n);
    case OP_SET_MASK:       return op_set_mask(n);
    case OP_READ_ALL:       return op_read_all(n);
    case OP_SET_MANY:       return op_set_many(n);
    case OP_CONFIGURE_MANY: return op_configure_many(n);
    case OP_STOP:           return op_stop(n);
    case OP_GET_STATUS:     return op_get_status(n);
    case OP_STREAM:         return op_stream(n);
    case OP_PING:           return op_ping(n);
    default:                return ST_ERR_UNKNOWN_OPCODE;
    }
}

/*! Run one complete request frame (already assembled in rx_frame) and build
 * the reply frame in tx_frame / tx_len.  Mirrors docs/protocol.md §2.5:
 * a frame with a bad magic or an impossible length is answered with seq = 0
 * and the receive state is reset. */
static void protocol_exec(WORD frame_len)
{
    WORD seq, payload_len, crc_frame, crc_calc;
    BYTE opcode, status;

    if (frame_len < PROTO_HDR) {
        frames_err++;
        reply_status(ST_ERR_BAD_LENGTH, 0);
        return;
    }
    if (rx_frame[0] != REQ_MAGIC) {
        frames_err++;
        reply_status(ST_ERR_MAGIC, 0);
        return;
    }
    seq = (WORD)rx_frame[2] | ((WORD)rx_frame[3] << 8);
    payload_len = (WORD)rx_frame[4] | ((WORD)rx_frame[5] << 8);
    if (payload_len > PROTO_MAX_PAYLOAD
        || (WORD)(PROTO_HDR + payload_len) != frame_len) {
        frames_err++;
        reply_status(ST_ERR_BAD_LENGTH, seq);
        return;
    }
    crc_frame = (WORD)rx_frame[6] | ((WORD)rx_frame[7] << 8);
    crc_calc = crc16_step(0xFFFF, &rx_frame[1], 5);
    crc_calc = crc16_step(crc_calc, &rx_frame[PROTO_HDR], payload_len);
    if (crc_calc != crc_frame) {
        crc_errors++;
        frames_err++;
        reply_status(ST_ERR_CRC, seq);
        return;
    }

    opcode = rx_frame[1];
    g_plen = 0;
    status = opcode_dispatch(opcode, payload_len);
    reply_frame(status, seq, g_plen);
    if (status == ST_OK) {
        frames_ok++;
    } else {
        frames_err++;
    }
}

/* --------------------------------------------------------------- bring-up - */

static void ports_init(void)
{
    /*
     * IFCONFIG[1:0] selects the external interface: keep the ports as plain
     * GPIO instead of the GPIF / slave-FIFO data bus.
     */
    IFCONFIG &= ~bmIFCFGMASK;

    /*
     * Keep PA0/PA1/PA6/PA7 as ordinary GPIO instead of the INT0/INT1/SLCS/
     * FLAGD alternate functions.
     */
    PORTACFG = 0;

    /* Reset state: every line INPUT, every output latch low. */
    out_latch[0] = 0x00;
    out_latch[1] = 0x00;
    out_latch[2] = 0x00;
    IOA = 0x00;
    IOB = 0x00;
    IOD = 0x00;
    OEA = 0x00;
    OEB = 0x00;
    OED = 0x00;
}

static void state_init(void)
{
    BYTE i;

    for (i = 0; i < PIN_COUNT; i++) {
        pin_flags[i] = FLAG_SCHMITT;
        pin_drive[i] = 1;               /* 4 mA                               */
        pin_slew[i]  = 0;               /* slow                               */
    }
    frames_ok   = 0;
    frames_err  = 0;
    crc_errors  = 0;
    sof_count   = 0;
    ticks_per_ms = 0;
    last_status = ST_OK;
    streaming   = 0;
    err_count   = 0;
    tx_len      = 0;
    g_plen      = 0;
}

/* ------------------------------------------------------------------ main - */

void main(void)
{
    REVCTL = 0;                         /* no advanced endpoint controls      */

    ports_init();
    state_init();

    SETCPUFREQ(CLK_48M);                /* the reset default is 12 MHz       */
    SETIF48MHZ();
    timer_init();

    RENUMERATE_UNCOND();                /* switch to our own descriptors      */

    USE_USB_INTS();
    ENABLE_SUDAV();
    ENABLE_SOF();
    ENABLE_USBRESET();
    ENABLE_HISPEED();

    got_sud = FALSE;
    got_reset = FALSE;

    EA = 1;

    for (;;) {
        if (got_reset) {
            /* The bus was reset: drop back to the safe state (§5.1/§5.2). */
            got_reset = FALSE;
            op_stop(0);
        }
        if (got_sud) {
            got_sud = FALSE;
            handle_setupdata();
        }
    }
}

/* ----------------------------------------------------------- interrupts --- */

void sudav_isr(void) __interrupt (ISR_SUDAV)
{
    got_sud = TRUE;
    CLEAR_SUDAV();
}

void sof_isr(void) __interrupt (ISR_SOF)
{
    sof_count++;
    CLEAR_SOF();
}

void usbreset_isr(void) __interrupt (ISR_USBRESET)
{
    handle_hispeed(FALSE);
    got_reset = TRUE;
    CLEAR_USBRESET();
}

void hispeed_isr(void) __interrupt (ISR_HISPEED)
{
    handle_hispeed(TRUE);
    CLEAR_HISPEED();
}

/* ----------------------------------------------------------- USB control - */

BOOL handle_get_descriptor(void)
{
    return FALSE;                       /* the library serves descriptors     */
}

/*! Arm EP0 for an OUT data stage and collect it into rx_frame[].
 *
 * Returns the number of bytes received, or 0xFFFF if the transfer could not
 * be started/completed.  Called from handle_vendorcommand(), i.e. after the
 * SETUP packet and before the data stage. */
static WORD ep0_receive(WORD off, WORD want)
{
    WORD guard, i, got;

    if (want == 0 || want > EP0_CHUNK) {
        return 0xFFFF;
    }
    if (off > FRAME_MAX || (WORD)(off + want) > FRAME_MAX) {
        return 0xFFFF;
    }

    /* Writing the expected length to EP0BCH:EP0BCL arms the data stage. */
    EP0BCH = SETUPDAT[7];
    EP0BCL = SETUPDAT[6];

    /* bmEPBUSY (EP0CS.1) is set until the packet has been received. */
    guard = 0;
    while (EP0CS & bmEPBUSY) {
        if (++guard > 50000u) {
            return 0xFFFF;              /* host went away: stall EP0         */
        }
    }

    got = (WORD)EP0BCL;
    if (EP0BCH != 0) {
        got = 0xFFFF;
    }
    if (got != want) {
        return 0xFFFF;
    }
    for (i = 0; i < got; i++) {
        rx_frame[off + i] = EP0BUF[i];
    }
    return got;
}

void handle_reset_ep(BYTE ep)
{
    (void)ep;                           /* no non-control endpoints          */
}

BOOL handle_vendorcommand(BYTE cmd)
{
    WORD off, n;

    switch (cmd) {
    case REQ_OUT_CHUNK: {
        /* wValue = byte offset into the frame, wIndex = byte count. */
        off = SETUP_VALUE();
        n   = SETUP_INDEX();
        if (SETUP_LENGTH() != n) {
            err_count++;
            return FALSE;
        }
        if (ep0_receive(off, n) == 0xFFFF) {
            err_count++;
            return FALSE;
        }
        return TRUE;
    }

    case REQ_EXEC: {
        WORD total = SETUP_VALUE();

        if (SETUP_LENGTH() != 0 || total < PROTO_HDR || total > FRAME_MAX) {
            err_count++;
            return FALSE;
        }
        protocol_exec(total);
        return TRUE;
    }

    case REQ_REPLY_LEN:
        EP0BUF[0] = (BYTE)(tx_len & 0xFF);
        EP0BUF[1] = (BYTE)(tx_len >> 8);
        EP0BCH = 0;
        EP0BCL = 2;
        return TRUE;

    case REQ_IN_CHUNK: {
        WORD i;

        off = SETUP_VALUE();
        n   = SETUP_INDEX();
        if (n == 0 || n > EP0_CHUNK || SETUP_LENGTH() != n) {
            err_count++;
            return FALSE;
        }
        if (off >= tx_len || (WORD)(off + n) > tx_len) {
            err_count++;
            return FALSE;
        }
        for (i = 0; i < n; i++) {
            EP0BUF[i] = tx_frame[off + i];
        }
        EP0BCH = 0;
        EP0BCL = (BYTE)n;
        return TRUE;
    }

    case REQ_DEBUG:
        EP0BUF[0]  = OEA;
        EP0BUF[1]  = OEB;
        EP0BUF[2]  = OED;
        EP0BUF[3]  = IOA;
        EP0BUF[4]  = IOB;
        EP0BUF[5]  = IOD;
        EP0BUF[6]  = err_count;
        EP0BUF[7]  = last_status;
        EP0BUF[8]  = (BYTE)(ticks_per_ms & 0xFF);
        EP0BUF[9]  = (BYTE)(ticks_per_ms >> 8);
        EP0BUF[10] = (BYTE)(sof_count & 0xFF);
        EP0BUF[11] = (BYTE)((sof_count >> 8) & 0xFF);
        EP0BUF[12] = (BYTE)((sof_count >> 16) & 0xFF);
        EP0BUF[13] = (BYTE)((sof_count >> 24) & 0xFF);
        EP0BUF[14] = streaming;
        EP0BUF[15] = HISPEED ? 1 : 0;
        EP0BCH = 0;
        EP0BCL = 16;
        return TRUE;

    case REQ_RESET_TO_BOOT:
        /*
         * Hand USB back to the EZ-USB core, which re-enumerates with
         * Cypress' default descriptors (04B4:8613) and leaves the 8051
         * reloadable from the host without a physical replug.  This has to
         * run on the 8051: a download request to CPUCS (0xE600) reaches the
         * CPU but not USBCS.
         *
         * The delay only has to cover a visible disconnect.  NOTE: fx2lib's
         * delay() is calibrated for a 12 MHz machine-cycle rate, while this
         * core runs one machine cycle in 4 clocks (4 MHz at CLK_48M), so a
         * requested delay is roughly 3x longer in real time: delay(300) is
         * about 0.9 s, which the loader's re-enumeration window covers.
         */
        EA = 0;
        USBCS |= bmDISCON;
        USBCS &= ~bmRENUM;
        delay(300);
        USBCS &= ~bmDISCON;
        return TRUE;
    }

    if (cmd >= REQ_OUT_CHUNK && cmd <= REQ_RESET_TO_BOOT) {
        err_count++;
    }
    return FALSE;                       /* unknown vendor request: stall EP0  */
}

BOOL handle_get_interface(BYTE ifc, BYTE *alt_ifc)
{
    if (ifc != 0) {
        return FALSE;
    }
    *alt_ifc = 0;
    return TRUE;
}

BOOL handle_set_interface(BYTE ifc, BYTE alt_ifc)
{
    /* A single interface with no endpoints: only alt setting 0 exists. */
    return (ifc == 0 && alt_ifc == 0) ? TRUE : FALSE;
}

BYTE handle_get_configuration(void)
{
    return 1;
}

BOOL handle_set_configuration(BYTE cfg)
{
    return cfg == 1 ? TRUE : FALSE;
}

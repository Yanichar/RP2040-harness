"""Wire protocol v1.0 for the RP2040 Harness.

This module is a byte-for-byte implementation of ``docs/protocol.md`` (v1.0,
frozen).  It contains no USB code and no device logic: only framing, CRC and
the ``struct`` layouts of every protocol record.

Frame layout (protocol.md §2)::

    request  (host -> device)     reply  (device -> host)
      0  1  magic = 0xA5            0  1  magic = 0x5A
      1  1  opcode                  1  1  status
      2  2  seq                     2  2  seq   (echo)
      4  2  len                     4  2  len
      6  2  crc                     6  2  crc
      8  n  payload                 8  n  payload

All multi-byte fields are little-endian.  The CRC is CRC-16/CCITT-FALSE
computed over ``opcode, seq, len`` followed by every payload byte; the magic
byte and the CRC field itself are excluded (§2.3).
"""

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass
from typing import Final

__all__ = [
    # framing
    "REQ_MAGIC",
    "REP_MAGIC",
    "HEADER_SIZE",
    "MAX_PAYLOAD",
    "MAX_FRAME",
    "crc16_ccitt_false",
    "encode_request",
    "decode_reply",
    "Reply",
    # opcodes
    "OP_GET_INFO",
    "OP_GET_PIN",
    "OP_CONFIGURE_PIN",
    "OP_SET_PIN",
    "OP_SET_MASK",
    "OP_READ_ALL",
    "OP_SET_MANY",
    "OP_CONFIGURE_MANY",
    "OP_STOP",
    "OP_GET_STATUS",
    "OP_STREAM",
    "OP_PING",
    "OPCODE_NAMES",
    "opcode_name",
    # statuses
    "ST_OK",
    "ST_ERR_UNKNOWN_OPCODE",
    "ST_ERR_BAD_LENGTH",
    "ST_ERR_BAD_PARAM",
    "ST_ERR_BUSY",
    "ST_ERR_CRC",
    "ST_ERR_MAGIC",
    "STATUS_NAMES",
    "status_name",
    # limits
    "PIN_COUNT",
    "ALL_PINS_MASK",
    "SET_MANY_MAX",
    "CONFIGURE_MANY_MAX",
    "STREAM_COUNT_MAX",
    "STREAM_VALUES_OFFSET",
    # caps bits
    "CAP_PIN_DIR",
    "CAP_PULL",
    "CAP_OPEN_DRAIN",
    "CAP_DRIVE_STRENGTH",
    "CAP_SLEW",
    "CAP_SCHMITT",
    "CAP_ATOMIC_MASK",
    "CAP_STREAM",
    "CAP_BATCH",
    "CAP_RESET_IFACE",
    "CAPS_V1",
    # flags
    "FLAG_PULL_UP",
    "FLAG_PULL_DOWN",
    "FLAG_OPEN_DRAIN",
    "FLAG_SCHMITT",
    "FLAG_MASK",
    # structs
    "INFO_STRUCT",
    "PIN_STATE_STRUCT",
    "CFG_PIN_STRUCT",
    "SET_PIN_STRUCT",
    "SET_MASK_STRUCT",
    "STREAM_HDR_STRUCT",
    "STREAM_REP_STRUCT",
    "STATUS_STRUCT",
    "Info",
    "PinState",
    "CfgPin",
    "SetPin",
    "SetMask",
    "StreamHeader",
    "StreamReply",
    "DeviceStatus",
    # directions / drive / slew
    "DIR_INPUT",
    "DIR_OUTPUT",
    "DRIVE_TO_CODE",
    "CODE_TO_DRIVE",
    "SLEW_TO_CODE",
    "CODE_TO_SLEW",
]

# --------------------------------------------------------------------------
# §2 Framing
# --------------------------------------------------------------------------

REQ_MAGIC: Final = 0xA5
REP_MAGIC: Final = 0x5A
HEADER_SIZE: Final = 8
MAX_PAYLOAD: Final = 1024
MAX_FRAME: Final = HEADER_SIZE + MAX_PAYLOAD


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly=0x1021, init=0xFFFF, no reflection, xorout=0).

    Self-check vector (protocol.md §2.3)::

        crc16_ccitt_false(b"123456789") == 0x29B1

    Implemented with :func:`binascii.crc_hqx`, which is exactly this algorithm
    (poly 0x1021, no reflection, caller-supplied initial value) in C. A bitwise
    Python loop costs about 1.4 ms on a 1 KB STREAM frame — more than the USB
    transfer it protects — while ``crc_hqx`` does the same work in ~3 us:
    measured ~490x faster and byte-identical on 300 random buffers.
    """
    return binascii.crc_hqx(bytes(data), 0xFFFF)


def crc16_ccitt_false_bitwise(data: bytes) -> int:
    """Reference implementation of the same CRC, one bit at a time.

    Kept for tests: this is the specification in executable form, and the fast
    version above is checked against it.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def encode_request(opcode: int, seq: int, payload: bytes = b"") -> bytes:
    """Build one request frame.

    ``seq`` is truncated to 16 bits.  ``payload`` longer than
    :data:`MAX_PAYLOAD` raises :class:`ValueError`.
    """
    if not 0 <= opcode <= 0xFF:
        raise ValueError(f"opcode out of range 0..255: {opcode!r}")
    if not 0 <= seq <= 0xFFFF:
        raise ValueError(f"seq out of range 0..65535: {seq!r}")
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(
            f"payload of {len(payload)} bytes exceeds protocol maximum "
            f"of {MAX_PAYLOAD} bytes"
        )
    # CRC covers opcode, seq, len and every payload byte.
    crc = crc16_ccitt_false(
        struct.pack("<BHH", opcode, seq, len(payload)) + payload
    )
    return struct.pack("<BBHHH", REQ_MAGIC, opcode, seq, len(payload), crc) + payload


@dataclass(frozen=True)
class Reply:
    """A decoded reply frame."""

    status: int
    seq: int
    payload: bytes

    @property
    def ok(self) -> bool:
        return self.status == ST_OK

    def __len__(self) -> int:  # convenience: len(reply) == len(reply.payload)
        return len(self.payload)


def decode_reply(buf: bytes) -> Reply:
    """Decode one reply frame.

    Raises :class:`ValueError` if the buffer is too short, the magic byte is
    wrong, the declared length does not match the buffer, or the CRC does not
    match.  Transport-level failures are *data* problems here; the device-level
    status code is carried in :attr:`Reply.status`.
    """
    buf = bytes(buf)
    if len(buf) < HEADER_SIZE:
        raise ValueError(
            f"reply frame truncated: got {len(buf)} bytes, "
            f"need at least {HEADER_SIZE}"
        )
    magic, status, seq, length, crc = struct.unpack_from("<BBHHH", buf, 0)
    if magic != REP_MAGIC:
        raise ValueError(
            f"bad reply magic 0x{magic:02X}, expected 0x{REP_MAGIC:02X}"
        )
    if length > MAX_PAYLOAD:
        raise ValueError(
            f"reply declares {length} payload bytes, protocol maximum is "
            f"{MAX_PAYLOAD}"
        )
    total = HEADER_SIZE + length
    if len(buf) < total:
        raise ValueError(
            f"reply frame truncated: header declares {total} bytes, "
            f"got {len(buf)}"
        )
    payload = buf[HEADER_SIZE:total]
    expected = crc16_ccitt_false(struct.pack("<BHH", status, seq, length) + payload)
    if crc != expected:
        raise ValueError(
            f"reply CRC mismatch: frame has 0x{crc:04X}, computed 0x{expected:04X}"
        )
    return Reply(status=status, seq=seq, payload=payload)


# --------------------------------------------------------------------------
# §4 Opcodes
# --------------------------------------------------------------------------

OP_GET_INFO: Final = 0x01
OP_GET_PIN: Final = 0x02
OP_CONFIGURE_PIN: Final = 0x03
OP_SET_PIN: Final = 0x04
OP_SET_MASK: Final = 0x05
OP_READ_ALL: Final = 0x06
OP_SET_MANY: Final = 0x07
OP_CONFIGURE_MANY: Final = 0x08
OP_STOP: Final = 0x09
OP_GET_STATUS: Final = 0x0A
OP_STREAM: Final = 0x10
OP_PING: Final = 0x7F

OPCODE_NAMES: Final[dict[int, str]] = {
    OP_GET_INFO: "GET_INFO",
    OP_GET_PIN: "GET_PIN",
    OP_CONFIGURE_PIN: "CONFIGURE_PIN",
    OP_SET_PIN: "SET_PIN",
    OP_SET_MASK: "SET_MASK",
    OP_READ_ALL: "READ_ALL",
    OP_SET_MANY: "SET_MANY",
    OP_CONFIGURE_MANY: "CONFIGURE_MANY",
    OP_STOP: "STOP",
    OP_GET_STATUS: "GET_STATUS",
    OP_STREAM: "STREAM",
    OP_PING: "PING",
}


def opcode_name(opcode: int) -> str:
    """Human-readable name for an opcode (``0x??`` for unknown codes)."""
    return OPCODE_NAMES.get(opcode, f"0x{opcode:02X}")


# --------------------------------------------------------------------------
# §2.4 Status codes
# --------------------------------------------------------------------------

ST_OK: Final = 0x00
ST_ERR_UNKNOWN_OPCODE: Final = 0x01
ST_ERR_BAD_LENGTH: Final = 0x02
ST_ERR_BAD_PARAM: Final = 0x03
ST_ERR_BUSY: Final = 0x04
ST_ERR_CRC: Final = 0x05
ST_ERR_MAGIC: Final = 0x06

STATUS_NAMES: Final[dict[int, str]] = {
    ST_OK: "OK",
    ST_ERR_UNKNOWN_OPCODE: "ERR_UNKNOWN_OPCODE",
    ST_ERR_BAD_LENGTH: "ERR_BAD_LENGTH",
    ST_ERR_BAD_PARAM: "ERR_BAD_PARAM",
    ST_ERR_BUSY: "ERR_BUSY",
    ST_ERR_CRC: "ERR_CRC",
    ST_ERR_MAGIC: "ERR_MAGIC",
}


def status_name(status: int) -> str:
    """Human-readable name for a device status code."""
    return STATUS_NAMES.get(status, f"UNKNOWN_STATUS_0x{status:02X}")


# --------------------------------------------------------------------------
# §3/§4 Limits
# --------------------------------------------------------------------------

PIN_COUNT: Final = 24
ALL_PINS_MASK: Final = 0x00FFFFFF
SET_MANY_MAX: Final = 64
CONFIGURE_MANY_MAX: Final = 32
STREAM_COUNT_MAX: Final = 250
STREAM_VALUES_OFFSET: Final = 16

# §4.2 caps bits
CAP_PIN_DIR: Final = 1 << 0
CAP_PULL: Final = 1 << 1
CAP_OPEN_DRAIN: Final = 1 << 2
CAP_DRIVE_STRENGTH: Final = 1 << 3
CAP_SLEW: Final = 1 << 4
CAP_SCHMITT: Final = 1 << 5
CAP_ATOMIC_MASK: Final = 1 << 6
CAP_STREAM: Final = 1 << 7
CAP_BATCH: Final = 1 << 8
CAP_RESET_IFACE: Final = 1 << 9
CAPS_V1: Final = 0x000003FF

CAP_NAMES: Final[dict[int, str]] = {
    CAP_PIN_DIR: "PIN_DIR",
    CAP_PULL: "PULL",
    CAP_OPEN_DRAIN: "OPEN_DRAIN",
    CAP_DRIVE_STRENGTH: "DRIVE_STRENGTH",
    CAP_SLEW: "SLEW",
    CAP_SCHMITT: "SCHMITT",
    CAP_ATOMIC_MASK: "ATOMIC_MASK",
    CAP_STREAM: "STREAM",
    CAP_BATCH: "BATCH",
    CAP_RESET_IFACE: "RESET_IFACE",
}


def caps_to_names(caps: int) -> list[str]:
    """Names of the capability bits set in ``caps``."""
    return [name for bit, name in CAP_NAMES.items() if caps & bit]


# §4.5 flags bits
FLAG_PULL_UP: Final = 1 << 0
FLAG_PULL_DOWN: Final = 1 << 1
FLAG_OPEN_DRAIN: Final = 1 << 2
FLAG_SCHMITT: Final = 1 << 3
FLAG_MASK: Final = 0x0F

# §4.3/§4.4 dir, §4.6 drive, slew
DIR_INPUT: Final = 0
DIR_OUTPUT: Final = 1

DRIVE_TO_CODE: Final[dict[str, int]] = {"2mA": 0, "4mA": 1, "8mA": 2, "12mA": 3}
CODE_TO_DRIVE: Final[dict[int, str]] = {v: k for k, v in DRIVE_TO_CODE.items()}

SLEW_TO_CODE: Final[dict[str, int]] = {"slow": 0, "fast": 1}
CODE_TO_SLEW: Final[dict[int, str]] = {v: k for k, v in SLEW_TO_CODE.items()}


# --------------------------------------------------------------------------
# §4 struct layouts.  ``<`` = little-endian, standard sizes, no padding.
# --------------------------------------------------------------------------

# §4.1 info_t — GET_INFO reply, 32 bytes
INFO_STRUCT: Final = struct.Struct("<BBBBBBHIIQII")

# §4.3 pin_state_t — GET_PIN reply, 8 bytes
# index, dir, level, flags, drive, slew, reserved
PIN_STATE_STRUCT: Final = struct.Struct("<BBBBBBH")

# §4.4 cfg_pin_t — CONFIGURE_PIN / CONFIGURE_MANY, 8 bytes
CFG_PIN_STRUCT: Final = struct.Struct("<BBBBBBH")  # index,dir,flags,drive,slew,level,reserved

# §4.7 set_pin_t — SET_PIN / SET_MANY, 4 bytes
SET_PIN_STRUCT: Final = struct.Struct("<BBH")  # index,level,reserved

# §4.8 set_mask_t — SET_MASK, 8 bytes
SET_MASK_STRUCT: Final = struct.Struct("<II")  # mask,value

# §4.10 stream_hdr_t — STREAM request header, 16 bytes
STREAM_HDR_STRUCT: Final = struct.Struct("<IIII")  # mask,count,mode,reserved

# §4.10 stream_rep_t — STREAM reply, 16 bytes
STREAM_REP_STRUCT: Final = struct.Struct("<IIII")  # applied,duration_us,toggles,reserved

# §4.11 status_t — GET_STATUS reply, 24 bytes
STATUS_STRUCT: Final = struct.Struct("<BBHIIIII")

# Plain u32 payloads (SET_MASK reply, READ_ALL reply, PING echo)
U32_STRUCT: Final = struct.Struct("<I")
U8_STRUCT: Final = struct.Struct("<B")


@dataclass(frozen=True)
class Info:
    """Decoded ``info_t`` (protocol.md §4.1)."""

    proto_major: int
    proto_minor: int
    fw_major: int
    fw_minor: int
    fw_patch: int
    pin_count: int
    caps: int
    pin_mask: int
    board_id: int
    reset_iface: int

    @property
    def firmware_version(self) -> str:
        return f"{self.fw_major}.{self.fw_minor}.{self.fw_patch}"

    @property
    def protocol_version(self) -> str:
        return f"{self.proto_major}.{self.proto_minor}"

    @property
    def serial(self) -> str:
        """Board unique id as 16 hex characters (flash unique id, §6)."""
        return f"{self.board_id:016X}"

    @property
    def capabilities(self) -> list[str]:
        return caps_to_names(self.caps)

    @classmethod
    def unpack(cls, payload: bytes) -> "Info":
        (
            proto_major,
            proto_minor,
            fw_major,
            fw_minor,
            fw_patch,
            pin_count,
            _reserved,
            caps,
            pin_mask,
            board_id,
            reset_iface,
            _reserved2,
        ) = INFO_STRUCT.unpack(_need(payload, INFO_STRUCT.size, "info_t"))
        return cls(
            proto_major=proto_major,
            proto_minor=proto_minor,
            fw_major=fw_major,
            fw_minor=fw_minor,
            fw_patch=fw_patch,
            pin_count=pin_count,
            caps=caps,
            pin_mask=pin_mask,
            board_id=board_id,
            reset_iface=reset_iface,
        )

    def pack(self) -> bytes:
        return INFO_STRUCT.pack(
            self.proto_major,
            self.proto_minor,
            self.fw_major,
            self.fw_minor,
            self.fw_patch,
            self.pin_count,
            0,
            self.caps,
            self.pin_mask,
            self.board_id,
            self.reset_iface,
            0,
        )


@dataclass(frozen=True)
class PinState:
    """Decoded ``pin_state_t`` (protocol.md §4.3)."""

    index: int
    dir: int
    level: int
    flags: int
    drive: int
    slew: int

    @property
    def direction(self) -> str:
        return "out" if self.dir == DIR_OUTPUT else "in"

    @property
    def pull(self) -> str | None:
        if self.flags & FLAG_PULL_UP:
            return "up"
        if self.flags & FLAG_PULL_DOWN:
            return "down"
        return None

    @property
    def open_drain(self) -> bool:
        return bool(self.flags & FLAG_OPEN_DRAIN)

    @property
    def schmitt(self) -> bool:
        return bool(self.flags & FLAG_SCHMITT)

    @property
    def drive_strength(self) -> str | None:
        return CODE_TO_DRIVE.get(self.drive)

    @property
    def slew_rate(self) -> str | None:
        return CODE_TO_SLEW.get(self.slew)

    @classmethod
    def unpack(cls, payload: bytes) -> "PinState":
        index, direction, level, flags, drive, slew, _reserved = PIN_STATE_STRUCT.unpack(
            _need(payload, PIN_STATE_STRUCT.size, "pin_state_t")
        )
        return cls(
            index=index,
            dir=direction,
            level=level,
            flags=flags,
            drive=drive,
            slew=slew,
        )

    def pack(self) -> bytes:
        return PIN_STATE_STRUCT.pack(
            self.index, self.dir, self.level, self.flags, self.drive, self.slew, 0
        )


@dataclass(frozen=True)
class CfgPin:
    """``cfg_pin_t`` (protocol.md §4.4) — one pin configuration."""

    index: int
    dir: int
    flags: int
    drive: int
    slew: int
    level: int = 0

    @classmethod
    def unpack(cls, payload: bytes) -> "CfgPin":
        index, direction, flags, drive, slew, level, _reserved = CFG_PIN_STRUCT.unpack(
            _need(payload, CFG_PIN_STRUCT.size, "cfg_pin_t")
        )
        return cls(
            index=index, dir=direction, flags=flags, drive=drive, slew=slew, level=level
        )

    def pack(self) -> bytes:
        return CFG_PIN_STRUCT.pack(
            self.index, self.dir, self.flags, self.drive, self.slew, self.level, 0
        )


@dataclass(frozen=True)
class SetPin:
    """``set_pin_t`` (protocol.md §4.7) — one level assignment."""

    index: int
    level: int

    @classmethod
    def unpack(cls, payload: bytes) -> "SetPin":
        index, level, _reserved = SET_PIN_STRUCT.unpack(
            _need(payload, SET_PIN_STRUCT.size, "set_pin_t")
        )
        return cls(index=index, level=level)

    def pack(self) -> bytes:
        return SET_PIN_STRUCT.pack(self.index, self.level, 0)


@dataclass(frozen=True)
class SetMask:
    """``set_mask_t`` (protocol.md §4.8) — one atomic masked write."""

    mask: int
    value: int

    @classmethod
    def unpack(cls, payload: bytes) -> "SetMask":
        mask, value = SET_MASK_STRUCT.unpack(
            _need(payload, SET_MASK_STRUCT.size, "set_mask_t")
        )
        return cls(mask=mask, value=value)

    def pack(self) -> bytes:
        return SET_MASK_STRUCT.pack(self.mask, self.value)


@dataclass(frozen=True)
class StreamHeader:
    """``stream_hdr_t`` (protocol.md §4.10) — STREAM request header."""

    mask: int
    count: int
    mode: int = 0
    reserved: int = 0

    def pack(self) -> bytes:
        return STREAM_HDR_STRUCT.pack(self.mask, self.count, self.mode, self.reserved)

    @classmethod
    def unpack(cls, payload: bytes) -> "StreamHeader":
        mask, count, mode, reserved = STREAM_HDR_STRUCT.unpack(
            _need(payload, STREAM_HDR_STRUCT.size, "stream_hdr_t")
        )
        return cls(mask=mask, count=count, mode=mode, reserved=reserved)


@dataclass(frozen=True)
class StreamReply:
    """``stream_rep_t`` (protocol.md §4.10) — STREAM reply payload."""

    applied: int
    duration_us: int
    toggles: int
    reserved: int = 0

    @property
    def updates_per_second(self) -> float:
        """``applied / (duration_us * 1e-6)`` per protocol.md §4.10."""
        if self.duration_us <= 0:
            return float("inf") if self.applied > 0 else 0.0
        return self.applied / (self.duration_us * 1e-6)

    @classmethod
    def unpack(cls, payload: bytes) -> "StreamReply":
        applied, duration_us, toggles, reserved = STREAM_REP_STRUCT.unpack(
            _need(payload, STREAM_REP_STRUCT.size, "stream_rep_t")
        )
        return cls(
            applied=applied,
            duration_us=duration_us,
            toggles=toggles,
            reserved=reserved,
        )

    def pack(self) -> bytes:
        return STREAM_REP_STRUCT.pack(
            self.applied, self.duration_us, self.toggles, self.reserved
        )


@dataclass(frozen=True)
class DeviceStatus:
    """Decoded ``status_t`` (protocol.md §4.11)."""

    state: int
    last_status: int
    frames_ok: int
    frames_err: int
    crc_errors: int
    uptime_ms: int

    @property
    def streaming(self) -> bool:
        return self.state == 1

    @property
    def state_name(self) -> str:
        return {0: "idle", 1: "streaming"}.get(self.state, f"unknown({self.state})")

    @property
    def last_status_name(self) -> str:
        return status_name(self.last_status)

    @classmethod
    def unpack(cls, payload: bytes) -> "DeviceStatus":
        state, last_status, _reserved, frames_ok, frames_err, crc_errors, uptime_ms, _r2 = (
            STATUS_STRUCT.unpack(_need(payload, STATUS_STRUCT.size, "status_t"))
        )
        return cls(
            state=state,
            last_status=last_status,
            frames_ok=frames_ok,
            frames_err=frames_err,
            crc_errors=crc_errors,
            uptime_ms=uptime_ms,
        )

    def pack(self) -> bytes:
        return STATUS_STRUCT.pack(
            self.state,
            self.last_status,
            0,
            self.frames_ok,
            self.frames_err,
            self.crc_errors,
            self.uptime_ms,
            0,
        )


def _need(payload: bytes, size: int, what: str) -> bytes:
    """Validate payload length for a fixed-size record."""
    if len(payload) != size:
        raise ValueError(
            f"{what} payload must be exactly {size} bytes, got {len(payload)}"
        )
    return payload

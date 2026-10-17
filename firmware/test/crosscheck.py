#!/usr/bin/env python3
"""Cross-check ``firmware/src/protocol.c`` against the Python host codec.

The firmware's protocol layer is compiled *natively for this machine* from the
same sources that go into the RP2040 build (see ``crosscheck.c`` and
``host_stubs.c``), then driven over stdin/stdout. Every reply is decoded with
the real ``rp2040_harness`` package, so a CRC, framing or field-layout
disagreement between the two implementations fails the run.

This is the strongest protocol verification available without hardware: the
RP2040 is not attached to this machine.

Usage::

    python firmware/test/crosscheck.py            # rebuild if needed, then run
    python firmware/test/crosscheck.py --rebuild
    python firmware/test/crosscheck.py --keep     # keep the temp binary

Exit code 0 = every check passed.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIRMWARE = HERE.parent
ROOT = FIRMWARE.parent

# Use the real host implementation for CRC and reply decoding, not a copy.
HOST_SRC = ROOT / "host" / "src"
if HOST_SRC.is_dir():
    sys.path.insert(0, str(HOST_SRC))

try:
    from rp2040_harness.protocol import (  # noqa: E402
        OP_CONFIGURE_MANY,
        OP_CONFIGURE_PIN,
        OP_GET_INFO,
        OP_GET_PIN,
        OP_GET_STATUS,
        OP_PING,
        OP_READ_ALL,
        OP_SET_MANY,
        OP_SET_MASK,
        OP_SET_PIN,
        OP_STOP,
        OP_STREAM,
        crc16_ccitt_false,
        decode_reply,
        encode_request,
    )
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        f"cannot import the host package from {HOST_SRC}: {exc}\n"
        "Install it with: pip install -e host"
    ) from exc

# --------------------------------------------------------------------------
# Independent encodings of docs/protocol.md.
#
# These structs are written straight from the document rather than imported
# from the host package, so both implementations are checked against the spec.
# --------------------------------------------------------------------------

INFO_T = struct.Struct("<BBBBBBHII8sII")        # §4.1, 32 bytes
PIN_STATE_T = struct.Struct("<BBBBBBH")          # §4.3,  8 bytes
CFG_PIN_T = struct.Struct("<BBBBBBH")            # §4.4,  8 bytes
SET_PIN_T = struct.Struct("<BBH")                # §4.7,  4 bytes
SET_MASK_T = struct.Struct("<II")                # §4.8,  8 bytes
STREAM_HDR_T = struct.Struct("<IIII")            # §4.10, 16 bytes
STREAM_REP_T = struct.Struct("<IIII")            # §4.10, 16 bytes
STATUS_T = struct.Struct("<BBHIIIII")            # §4.11, 24 bytes

assert INFO_T.size == 32
assert PIN_STATE_T.size == 8
assert CFG_PIN_T.size == 8
assert SET_PIN_T.size == 4
assert SET_MASK_T.size == 8
assert STREAM_HDR_T.size == 16
assert STREAM_REP_T.size == 16
assert STATUS_T.size == 24

# §2.4 status codes
ST_OK = 0x00
ST_ERR_UNKNOWN_OPCODE = 0x01
ST_ERR_BAD_LENGTH = 0x02
ST_ERR_BAD_PARAM = 0x03
ST_ERR_BUSY = 0x04
ST_ERR_CRC = 0x05
ST_ERR_MAGIC = 0x06

# §4.5 flags
FLAG_PULL_UP = 0x01
FLAG_PULL_DOWN = 0x02
FLAG_OPEN_DRAIN = 0x04
FLAG_SCHMITT = 0x08

DIR_IN = 0
DIR_OUT = 1

DRIVE_2MA, DRIVE_4MA, DRIVE_8MA, DRIVE_12MA = 0, 1, 2, 3
SLEW_SLOW, SLEW_FAST = 0, 1

PIN_COUNT = 24
ALL_PINS_MASK = 0x00FFFFFF
CAPS_V1 = 0x000003FF
STREAM_COUNT_MAX = 250

# Host stub board id (firmware/test/host_stubs.c)
STUB_BOARD_ID = bytes.fromhex("DEADBEEF01020304")


# --------------------------------------------------------------------------
# Test harness
# --------------------------------------------------------------------------

CHECKS: list[tuple[str, object]] = []


def check(name: str):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn

    return deco


class Failed(AssertionError):
    pass


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise Failed(msg)


def find_gcc() -> Path:
    for candidate in (
        Path(r"C:\msys64\mingw64\bin\gcc.exe"),
        Path(r"C:\msys64\ucrt64\bin\gcc.exe"),
        Path("/usr/bin/gcc"),
    ):
        if candidate.exists():
            return candidate
    found = shutil.which("gcc")
    if found:
        return Path(found)
    raise SystemExit(
        "no host C compiler found; install gcc (expected C:\\msys64\\mingw64\\bin\\gcc.exe)"
    )


def build(exe: Path) -> None:
    gcc = find_gcc()
    cmd = [
        str(gcc),
        "-std=c11",
        "-O1",
        "-Wall",
        "-Wextra",
        "-Wno-unused-parameter",
        "-I",
        str(HERE / "fake"),
        "-I",
        str(FIRMWARE / "src"),
        str(HERE / "crosscheck.c"),
        str(HERE / "host_stubs.c"),
        str(FIRMWARE / "src" / "protocol.c"),
        str(FIRMWARE / "src" / "crc16.c"),
        "-o",
        str(exe),
    ]
    print("building:", " ".join(cmd))
    # The MSYS2/MinGW gcc needs its own bin directory on PATH to find its DLLs;
    # the machine's default PATH points at an incompatible Cypress FX3 toolchain.
    env = dict(os.environ)
    env["PATH"] = str(gcc.parent) + os.pathsep + env.get("PATH", "")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    if proc.returncode != 0:
        raise SystemExit(f"host build failed (exit {proc.returncode})")


class DeviceUnderTest:
    """The firmware protocol layer, compiled natively, spoken to over pipes."""

    def __init__(self, exe: Path) -> None:
        self.proc = subprocess.Popen(
            [str(exe)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def _line(self, text: str) -> str:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()
        out = self.proc.stdout.readline()
        if out == "":
            raise Failed("firmware test binary closed its stdout unexpectedly")
        return out.strip()

    # -- low level ---------------------------------------------------------

    def crc(self, data: bytes) -> int:
        line = self._line("#crc " + data.hex().upper())
        expect(line.startswith("CRC "), f"unexpected #crc reply: {line!r}")
        return int(line.split(" ", 1)[1], 16)

    def safe(self) -> None:
        expect(self._line("#safe") == "OK", "#safe failed")

    def rx_reset(self) -> None:
        expect(self._line("#rxreset") == "OK", "#rxreset failed")

    def rx_push(self, data: bytes) -> str:
        return self._line("#rx " + data.hex().upper())

    def raw(self, frame: bytes) -> bytes:
        line = self._line(frame.hex().upper())
        expect(line.startswith("REPLY"), f"unexpected reply line: {line!r}")
        hexpart = line[len("REPLY") :].strip()
        return bytes.fromhex(hexpart) if hexpart else b""

    # -- protocol level ----------------------------------------------------

    def exchange(self, opcode: int, payload: bytes = b"", seq: int = 1) -> object:
        frame = encode_request(opcode, seq, payload)
        reply_bytes = self.raw(frame)
        try:
            reply = decode_reply(reply_bytes)
        except ValueError as exc:
            raise Failed(f"host could not decode firmware reply for opcode "
                         f"0x{opcode:02X}: {exc} (raw={reply_bytes.hex().upper()})") from exc
        expect(
            reply.seq == seq,
            f"seq echo mismatch for opcode 0x{opcode:02X}: sent {seq}, got {reply.seq}",
        )
        return reply

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.write("#quit\n")
                self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


@check("CRC-16/CCITT-FALSE matches the host implementation")
def t_crc(dev: DeviceUnderTest) -> None:
    expect(dev.crc(b"123456789") == 0x29B1, "C CRC control vector != 0x29B1")
    expect(
        crc16_ccitt_false(b"123456789") == 0x29B1,
        "host CRC control vector != 0x29B1",
    )
    rng = random.Random(0xC0FFEE)
    for _ in range(300):
        n = rng.randrange(0, 300)
        data = bytes(rng.randrange(256) for _ in range(n))
        c = dev.crc(data)
        h = crc16_ccitt_false(data)
        expect(c == h, f"CRC mismatch on {data.hex()}: C=0x{c:04X} host=0x{h:04X}")


@check("GET_INFO returns the documented info_t")
def t_get_info(dev: DeviceUnderTest) -> None:
    rep = dev.exchange(OP_GET_INFO, b"", seq=0x1234)
    expect(rep.status == ST_OK, f"status {rep.status:#x}")
    expect(len(rep.payload) == INFO_T.size, f"payload {len(rep.payload)} != 32")
    (
        proto_major,
        proto_minor,
        fw_major,
        fw_minor,
        fw_patch,
        pin_count,
        reserved0,
        caps,
        pin_mask,
        board_id,
        reset_iface,
        reserved1,
    ) = INFO_T.unpack(rep.payload)
    expect((proto_major, proto_minor) == (1, 0), f"protocol {proto_major}.{proto_minor} != 1.0")
    expect(pin_count == PIN_COUNT, f"pin_count {pin_count} != 24")
    expect(caps == CAPS_V1, f"caps {caps:#x} != {CAPS_V1:#x}")
    expect(pin_mask == ALL_PINS_MASK, f"pin_mask {pin_mask:#x}")
    expect(board_id == STUB_BOARD_ID, f"board_id {board_id.hex()}")
    expect(reset_iface == 1, f"reset_iface {reset_iface} != 1")
    expect(reserved0 == 0 and reserved1 == 0, "reserved fields must be zero")
    expect(0 <= fw_major < 256 and 0 <= fw_minor < 256 and 0 <= fw_patch < 256, "fw version")


@check("PING echoes an arbitrary nonce")
def t_ping(dev: DeviceUnderTest) -> None:
    for nonce in (0, 1, 0xDEADBEEF, 0xFFFFFFFF):
        rep = dev.exchange(OP_PING, struct.pack("<I", nonce), seq=7)
        expect(rep.status == ST_OK, f"status {rep.status:#x} for nonce {nonce:#x}")
        expect(struct.unpack("<I", rep.payload)[0] == nonce, f"nonce {nonce:#x} not echoed")


@check("CONFIGURE_PIN then GET_PIN round-trips every field")
def t_configure_pin(dev: DeviceUnderTest) -> None:
    dev.safe()
    flags = FLAG_SCHMITT
    rep = dev.exchange(
        OP_CONFIGURE_PIN,
        CFG_PIN_T.pack(3, DIR_OUT, flags, DRIVE_8MA, SLEW_FAST, 1, 0),
    )
    expect(rep.status == ST_OK, f"configure status {rep.status:#x}")

    rep = dev.exchange(OP_GET_PIN, bytes([3]))
    expect(rep.status == ST_OK, f"get_pin status {rep.status:#x}")
    index, direction, level, got_flags, drive, slew, _res = PIN_STATE_T.unpack(rep.payload)
    expect(index == 3, f"index {index}")
    expect(direction == DIR_OUT, f"dir {direction}")
    expect(level == 1, f"level {level}")
    expect(got_flags == flags, f"flags {got_flags:#x} != {flags:#x}")
    expect(drive == DRIVE_8MA, f"drive {drive}")
    expect(slew == SLEW_FAST, f"slew {slew}")

    # pull-up + open-drain on an output
    flags2 = FLAG_PULL_UP | FLAG_OPEN_DRAIN | FLAG_SCHMITT
    rep = dev.exchange(
        OP_CONFIGURE_PIN,
        CFG_PIN_T.pack(21, DIR_OUT, flags2, DRIVE_12MA, SLEW_SLOW, 0, 0),
    )
    expect(rep.status == ST_OK, f"configure #2 status {rep.status:#x}")
    rep = dev.exchange(OP_GET_PIN, bytes([21]))
    _, direction, level, got_flags, drive, slew, _ = PIN_STATE_T.unpack(rep.payload)
    expect((direction, level, got_flags, drive, slew) == (DIR_OUT, 0, flags2, DRIVE_12MA, SLEW_SLOW),
           "second configure did not round-trip")


@check("SET_PIN + READ_ALL agree")
def t_set_pin(dev: DeviceUnderTest) -> None:
    dev.safe()
    for index in (0, 7, 23):
        rep = dev.exchange(OP_CONFIGURE_PIN, CFG_PIN_T.pack(index, DIR_OUT, 0, DRIVE_4MA, SLEW_SLOW, 0, 0))
        expect(rep.status == ST_OK, f"configure {index} status {rep.status:#x}")

    rep = dev.exchange(OP_SET_PIN, SET_PIN_T.pack(0, 1, 0))
    expect(rep.status == ST_OK, "set_pin 0 -> 1")
    rep = dev.exchange(OP_SET_PIN, SET_PIN_T.pack(23, 1, 0))
    expect(rep.status == ST_OK, "set_pin 23 -> 1")

    rep = dev.exchange(OP_READ_ALL)
    levels = struct.unpack("<I", rep.payload)[0]
    expect(levels & (1 << 0), "pin 0 should read high")
    expect(levels & (1 << 23), "pin 23 should read high")
    expect(not (levels & (1 << 7)), "pin 7 should read low")

    rep = dev.exchange(OP_SET_PIN, SET_PIN_T.pack(0, 0, 0))
    expect(rep.status == ST_OK, "set_pin 0 -> 0")
    rep = dev.exchange(OP_READ_ALL)
    levels = struct.unpack("<I", rep.payload)[0]
    expect(not (levels & (1 << 0)), "pin 0 should read low")
    expect(levels & (1 << 23), "pin 23 should still read high")


@check("SET_MASK is atomic and returns the applied latch value")
def t_set_mask(dev: DeviceUnderTest) -> None:
    dev.safe()
    expect(
        dev.exchange(OP_SET_MASK, SET_MASK_T.pack(0x0000FF, 0x0000A5)).status == ST_OK,
        "set_mask failed",
    )
    rep = dev.exchange(OP_SET_MASK, SET_MASK_T.pack(0x0000FF, 0x0000A5))
    expect(struct.unpack("<I", rep.payload)[0] == 0x0000A5,
           "applied value should be 0xA5")

    # Bits outside the mask must be untouched.
    rep = dev.exchange(OP_SET_MASK, SET_MASK_T.pack(0x00FF00, 0x00FF00))
    applied = struct.unpack("<I", rep.payload)[0]
    expect(applied == 0x00FFA5, f"applied {applied:#08x} != 0x00ffa5")

    # Clearing.
    rep = dev.exchange(OP_SET_MASK, SET_MASK_T.pack(0x00FFFF, 0x000000))
    expect(struct.unpack("<I", rep.payload)[0] == 0, "clear should zero the latch")

    # Mask bits above bit 23 are ignored, never applied.
    rep = dev.exchange(OP_SET_MASK, SET_MASK_T.pack(0xFFFFFFFF, 0xFFFFFFFF))
    applied = struct.unpack("<I", rep.payload)[0]
    expect(applied == ALL_PINS_MASK, f"applied {applied:#010x} should clamp to 24 bits")


@check("SET_MANY applies a batch and reports the count")
def t_set_many(dev: DeviceUnderTest) -> None:
    dev.safe()
    for index in range(4):
        dev.exchange(OP_CONFIGURE_PIN, CFG_PIN_T.pack(index, DIR_OUT, 0, DRIVE_4MA, SLEW_SLOW, 0, 0))

    payload = b"".join(SET_PIN_T.pack(i, 1 if i % 2 == 0 else 0, 0) for i in range(4))
    rep = dev.exchange(OP_SET_MANY, payload)
    expect(rep.status == ST_OK, f"set_many status {rep.status:#x}")
    expect(rep.payload == bytes([4]), f"applied byte {rep.payload.hex()!r} != 04")

    rep = dev.exchange(OP_READ_ALL)
    levels = struct.unpack("<I", rep.payload)[0]
    expect(levels & 0b0101 == 0b0101, f"pins 0 and 2 should be high (levels={levels:#x})")
    expect(levels & 0b1010 == 0, f"pins 1 and 3 should be low (levels={levels:#x})")


@check("SET_MANY is all-or-nothing when an element is invalid")
def t_set_many_atomic(dev: DeviceUnderTest) -> None:
    dev.safe()
    dev.exchange(OP_CONFIGURE_PIN, CFG_PIN_T.pack(0, DIR_OUT, 0, DRIVE_4MA, SLEW_SLOW, 0, 0))
    # pin 1 stays an INPUT -> the whole batch must be rejected and pin 0 untouched
    payload = SET_PIN_T.pack(0, 1, 0) + SET_PIN_T.pack(1, 1, 0)
    rep = dev.exchange(OP_SET_MANY, payload)
    expect(rep.status == ST_ERR_BAD_PARAM, f"expected ERR_BAD_PARAM, got {rep.status:#x}")

    rep = dev.exchange(OP_READ_ALL)
    levels = struct.unpack("<I", rep.payload)[0]
    expect(not (levels & 1), "pin 0 must NOT have changed after a rejected batch")


@check("CONFIGURE_MANY applies a batch")
def t_configure_many(dev: DeviceUnderTest) -> None:
    dev.safe()
    payload = b"".join(
        CFG_PIN_T.pack(i, DIR_OUT, FLAG_SCHMITT, DRIVE_2MA, SLEW_FAST, 1, 0)
        for i in (5, 6, 7)
    )
    rep = dev.exchange(OP_CONFIGURE_MANY, payload)
    expect(rep.status == ST_OK, f"configure_many status {rep.status:#x}")
    expect(rep.payload == bytes([3]), f"applied {rep.payload.hex()!r} != 03")

    rep = dev.exchange(OP_READ_ALL)
    levels = struct.unpack("<I", rep.payload)[0]
    expect(levels & 0b11100000 == 0b11100000, f"pins 5,6,7 should be high ({levels:#x})")


@check("STREAM drives values and reports timing")
def t_stream(dev: DeviceUnderTest) -> None:
    dev.safe()
    for i in range(PIN_COUNT):
        dev.exchange(OP_CONFIGURE_PIN, CFG_PIN_T.pack(i, DIR_OUT, 0, DRIVE_4MA, SLEW_SLOW, 0, 0))

    values = [0x00AAAA, 0x005555, 0x00FFFF, 0x000000]
    payload = STREAM_HDR_T.pack(ALL_PINS_MASK, len(values), 0, 0) + b"".join(
        struct.pack("<I", v) for v in values
    )
    rep = dev.exchange(OP_STREAM, payload)
    expect(rep.status == ST_OK, f"stream status {rep.status:#x}")
    applied, duration_us, toggles, reserved = STREAM_REP_T.unpack(rep.payload)
    expect(applied == len(values), f"applied {applied} != {len(values)}")
    expect(reserved == 0, "stream reply reserved field must be zero")
    expect(toggles > 0, "toggles should be non-zero for a changing sequence")

    rep = dev.exchange(OP_READ_ALL)
    levels = struct.unpack("<I", rep.payload)[0]
    expect(levels == 0, f"last streamed value was 0, READ_ALL gave {levels:#x}")

    # STREAM must only touch the masked lines; the rest keep their value.
    dev.safe()
    for i in range(4):
        dev.exchange(OP_CONFIGURE_PIN, CFG_PIN_T.pack(i, DIR_OUT, 0, DRIVE_4MA, SLEW_SLOW, 0, 0))
    payload = STREAM_HDR_T.pack(0x00000F, 1, 0, 0) + struct.pack("<I", 0xFFFFFFFF)
    rep = dev.exchange(OP_STREAM, payload)
    expect(rep.status == ST_OK, f"masked stream status {rep.status:#x}")
    applied, _, _, _ = STREAM_REP_T.unpack(rep.payload)
    expect(applied == 1, f"applied {applied} != 1")
    rep = dev.exchange(OP_READ_ALL)
    levels = struct.unpack("<I", rep.payload)[0]
    expect(levels == 0x00000F, f"only bits 0..3 should be set, got {levels:#x}")


@check("STOP returns every line to INPUT")
def t_stop(dev: DeviceUnderTest) -> None:
    dev.safe()
    dev.exchange(OP_CONFIGURE_PIN, CFG_PIN_T.pack(4, DIR_OUT, FLAG_PULL_UP, DRIVE_8MA, SLEW_FAST, 1, 0))
    rep = dev.exchange(OP_READ_ALL)
    expect(struct.unpack("<I", rep.payload)[0] & (1 << 4), "pin 4 should be high before STOP")

    expect(dev.exchange(OP_STOP).status == ST_OK, "STOP failed")

    rep = dev.exchange(OP_GET_PIN, bytes([4]))
    _, direction, level, flags, _, _, _ = PIN_STATE_T.unpack(rep.payload)
    expect(direction == DIR_IN, f"pin 4 direction after STOP: {direction}")
    expect(level == 0, f"pin 4 level after STOP: {level}")
    expect(not (flags & FLAG_PULL_UP), f"pin 4 pull-up after STOP: {flags:#x}")

    rep = dev.exchange(OP_READ_ALL)
    expect(struct.unpack("<I", rep.payload)[0] == 0, "all lines should read low after STOP")


@check("GET_STATUS returns a well-formed status_t")
def t_get_status(dev: DeviceUnderTest) -> None:
    rep = dev.exchange(OP_GET_STATUS)
    expect(rep.status == ST_OK, f"status {rep.status:#x}")
    state, last_status, _res, frames_ok, frames_err, crc_errors, uptime_ms, _res2 = STATUS_T.unpack(
        rep.payload
    )
    expect(state in (0, 1), f"state {state}")
    expect(last_status <= ST_ERR_MAGIC, f"last_status {last_status:#x}")
    expect(frames_ok > 0, "frames_ok should have advanced")
    expect(uptime_ms >= 0, "uptime_ms")
    expect(crc_errors <= frames_err, "crc_errors cannot exceed frames_err")


@check("error paths return the documented status codes")
def t_errors(dev: DeviceUnderTest) -> None:
    # unknown opcode
    rep = dev.exchange(0x33)
    expect(rep.status == ST_ERR_UNKNOWN_OPCODE, f"unknown opcode -> {rep.status:#x}")

    # bad magic (seq must be zeroed)
    good = encode_request(OP_GET_INFO, 0x2222, b"")
    bad = bytes([0x00]) + good[1:]
    rep_bytes = dev.raw(bad)
    reply = decode_reply(rep_bytes)
    expect(reply.status == ST_ERR_MAGIC, f"bad magic -> {reply.status:#x}")
    expect(reply.seq == 0, f"bad magic seq should be 0, got {reply.seq}")

    # CRC failure
    corrupted = bytearray(encode_request(OP_READ_ALL, 0x33, b""))
    corrupted[0] = 0xA5
    corrupted[-1] ^= 0xFF
    reply = decode_reply(dev.raw(bytes(corrupted)))
    expect(reply.status == ST_ERR_CRC, f"bad crc -> {reply.status:#x}")

    # declared length mismatch
    frame = bytearray(encode_request(OP_READ_ALL, 1, b""))
    frame[4] = 0x10
    frame[5] = 0x00
    reply = decode_reply(dev.raw(bytes(frame)))
    expect(reply.status == ST_ERR_BAD_LENGTH, f"len mismatch -> {reply.status:#x}")

    # oversize declared length
    frame = bytearray(encode_request(OP_READ_ALL, 1, b""))
    struct.pack_into("<H", frame, 4, 5000)
    reply = decode_reply(dev.raw(bytes(frame)))
    expect(reply.status == ST_ERR_BAD_LENGTH, f"oversize len -> {reply.status:#x}")

    # parameter validation
    cases = [
        ("pin index out of range", OP_GET_PIN, bytes([24])),
        ("CONFIGURE_PIN index out of range", OP_CONFIGURE_PIN, CFG_PIN_T.pack(24, DIR_OUT, 0, 0, 0, 0, 0)),
        ("pull-up + pull-down", OP_CONFIGURE_PIN, CFG_PIN_T.pack(0, DIR_IN, FLAG_PULL_UP | FLAG_PULL_DOWN, 0, 0, 0, 0)),
        ("drive code out of range", OP_CONFIGURE_PIN, CFG_PIN_T.pack(0, DIR_OUT, 0, 4, 0, 0, 0)),
        ("slew out of range", OP_CONFIGURE_PIN, CFG_PIN_T.pack(0, DIR_OUT, 0, 0, 2, 0, 0)),
        ("open-drain on an input", OP_CONFIGURE_PIN, CFG_PIN_T.pack(0, DIR_IN, FLAG_OPEN_DRAIN, 0, 0, 0, 0)),
        ("undefined flag bits", OP_CONFIGURE_PIN, CFG_PIN_T.pack(0, DIR_OUT, 0x10, 0, 0, 0, 0)),
        ("STREAM with mask 0", OP_STREAM, STREAM_HDR_T.pack(0, 1, 0, 0) + struct.pack("<I", 1)),
        ("STREAM with mode != 0", OP_STREAM, STREAM_HDR_T.pack(1, 1, 1, 0) + struct.pack("<I", 1)),
        ("STREAM count 0", OP_STREAM, STREAM_HDR_T.pack(1, 0, 0, 0)),
        ("STREAM count too large", OP_STREAM, STREAM_HDR_T.pack(1, STREAM_COUNT_MAX + 1, 0, 0)),
    ]
    for name, opcode, payload in cases:
        rep = dev.exchange(opcode, payload)
        expect(rep.status == ST_ERR_BAD_PARAM, f"{name}: expected ERR_BAD_PARAM, got {rep.status:#x}")

    # length validation
    bad_lengths = [
        ("GET_INFO with payload", OP_GET_INFO, b"\x00"),
        ("GET_PIN without payload", OP_GET_PIN, b""),
        ("SET_PIN with 2 bytes", OP_SET_PIN, b"\x00\x01"),
        ("SET_MASK with 4 bytes", OP_SET_MASK, b"\x00\x00\x00\x00"),
        ("PING with 3 bytes", OP_PING, b"\x00\x00\x00"),
        ("SET_MANY empty", OP_SET_MANY, b""),
        ("SET_MANY not a multiple of 4", OP_SET_MANY, b"\x00" * 5),
        ("CONFIGURE_MANY not a multiple of 8", OP_CONFIGURE_MANY, b"\x00" * 9),
    ]
    for name, opcode, payload in bad_lengths:
        rep = dev.exchange(opcode, payload)
        expect(rep.status == ST_ERR_BAD_LENGTH, f"{name}: expected ERR_BAD_LENGTH, got {rep.status:#x}")

    # SET_PIN on an INPUT pin
    dev.safe()
    rep = dev.exchange(OP_SET_PIN, SET_PIN_T.pack(9, 1, 0))
    expect(rep.status == ST_ERR_BAD_PARAM, f"SET_PIN on input -> {rep.status:#x}")


@check("framing accumulator resynchronises after a bad magic byte")
def t_rx_accumulator(dev: DeviceUnderTest) -> None:
    dev.rx_reset()

    frame = encode_request(OP_READ_ALL, 5, b"")
    # A complete frame in one push.
    expect(dev.rx_push(frame) == f"RX F{len(frame)}", "single complete frame not detected")

    # A truncated frame must wait for more bytes.
    dev.rx_reset()
    out = dev.rx_push(frame[:4])
    expect(out == "RX", f"partial frame should not complete, got {out!r}")

    # Garbage before a valid frame: resync, then report the frame.
    dev.rx_reset()
    garbage = b"\x00\x11\x22"
    out = dev.rx_push(garbage + frame)
    expect("C" in out, f"expected a corrupt report, got {out!r}")
    expect(out.endswith(f"F{len(frame)}"), f"expected resync to find the frame, got {out!r}")

    # Two frames in one push.
    dev.rx_reset()
    out = dev.rx_push(frame + frame)
    expect(out == f"RX F{len(frame)} F{len(frame)}", f"expected two frames, got {out!r}")

    # Oversize declared length is rejected by the framer.
    dev.rx_reset()
    bogus = bytearray(frame)
    struct.pack_into("<H", bogus, 4, 5000)
    out = dev.rx_push(bytes(bogus))
    expect("C" in out, f"oversize length should be flagged corrupt, got {out!r}")


@check("docs/protocol.md §7 worked example reproduces exactly")
def t_worked_example(dev: DeviceUnderTest) -> None:
    dev.safe()
    payload = SET_MASK_T.pack(0x0000FF, 0x0000A5)
    frame = encode_request(OP_SET_MASK, 0x1234, payload)

    head = frame[:6]
    expect(
        head == bytes([0xA5, 0x05, 0x34, 0x12, 0x08, 0x00]),
        f"request header {head.hex().upper()} != A5053412080000",
    )
    expect(frame[8:] == bytes.fromhex("FF000000" "A5000000"), "request payload bytes")
    expect(len(frame) == 16, f"request frame length {len(frame)} != 16")

    reply = dev.raw(frame)
    expect(len(reply) == 12, f"reply length {len(reply)} != 12")
    expect(
        reply[:6] == bytes([0x5A, 0x00, 0x34, 0x12, 0x04, 0x00]),
        f"reply header {reply[:6].hex().upper()} != 5A003412040000",
    )
    expect(reply[8:] == bytes.fromhex("A5000000"), f"reply payload {reply[8:].hex()}")


@check("CONFIGURE_PIN ignores drive/slew for an INPUT line (protocol.md §4.4)")
def t_input_keeps_drive(dev: DeviceUnderTest) -> None:
    dev.safe()
    rep = dev.exchange(OP_CONFIGURE_PIN, CFG_PIN_T.pack(2, DIR_OUT, FLAG_SCHMITT, DRIVE_12MA, SLEW_FAST, 0, 0))
    expect(rep.status == ST_OK, f"configure out status {rep.status:#x}")

    # Switch to INPUT asking for 2 mA / slow: both must be ignored.
    rep = dev.exchange(OP_CONFIGURE_PIN, CFG_PIN_T.pack(2, DIR_IN, FLAG_SCHMITT, DRIVE_2MA, SLEW_SLOW, 1, 0))
    expect(rep.status == ST_OK, f"configure in status {rep.status:#x}")

    _, direction, level, _flags, drive, slew, _ = PIN_STATE_T.unpack(
        dev.exchange(OP_GET_PIN, bytes([2])).payload
    )
    expect(direction == DIR_IN, f"direction {direction}")
    expect(level == 0, f"latch must be forced low on INPUT, level={level}")
    expect(drive == DRIVE_12MA, f"drive must be retained (12 mA), got code {drive}")
    expect(slew == SLEW_FAST, f"slew must be retained (fast), got {slew}")

    # Switching back to OUTPUT applies what the request asks for.
    rep = dev.exchange(OP_CONFIGURE_PIN, CFG_PIN_T.pack(2, DIR_OUT, FLAG_SCHMITT, DRIVE_8MA, SLEW_SLOW, 1, 0))
    expect(rep.status == ST_OK, f"configure out #2 status {rep.status:#x}")
    _, direction, level, _flags, drive, slew, _ = PIN_STATE_T.unpack(
        dev.exchange(OP_GET_PIN, bytes([2])).payload
    )
    expect((direction, level, drive, slew) == (DIR_OUT, 1, DRIVE_8MA, SLEW_SLOW),
           f"re-configure to OUTPUT did not apply: dir={direction} level={level} "
           f"drive={drive} slew={slew}")

    # Invalid values are still rejected even though they would be ignored.
    rep = dev.exchange(OP_CONFIGURE_PIN, CFG_PIN_T.pack(2, DIR_IN, 0, 4, 0, 0, 0))
    expect(rep.status == ST_ERR_BAD_PARAM, f"bad drive on INPUT -> {rep.status:#x}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true", help="always rebuild the C driver")
    parser.add_argument("--keep", action="store_true", help="keep the built binary")
    args = parser.parse_args(argv)

    tmpdir = Path(tempfile.mkdtemp(prefix="harness-crosscheck-"))
    exe = tmpdir / ("crosscheck.exe" if os.name == "nt" else "crosscheck")

    sources = [
        HERE / "crosscheck.c",
        HERE / "host_stubs.c",
        FIRMWARE / "src" / "protocol.c",
        FIRMWARE / "src" / "crc16.c",
        FIRMWARE / "src" / "protocol.h",
    ]
    stale = not exe.exists() or any(
        s.exists() and s.stat().st_mtime > exe.stat().st_mtime for s in sources
    )
    if args.rebuild or stale:
        build(exe)
    else:
        print(f"reusing {exe}")

    dev = DeviceUnderTest(exe)
    failures: list[tuple[str, str]] = []
    try:
        for name, fn in CHECKS:
            try:
                fn(dev)
            except Failed as exc:
                failures.append((name, str(exc)))
                print(f"FAIL  {name}\n        {exc}")
            except Exception as exc:  # noqa: BLE001
                failures.append((name, f"{type(exc).__name__}: {exc}"))
                print(f"ERROR {name}\n        {type(exc).__name__}: {exc}")
            else:
                print(f"ok    {name}")
    finally:
        dev.close()
        if not args.keep:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"binary kept at {exe}")

    print()
    if failures:
        print(f"{len(failures)} of {len(CHECKS)} checks FAILED")
        return 1
    print(f"all {len(CHECKS)} checks passed — firmware and host agree on the wire format")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

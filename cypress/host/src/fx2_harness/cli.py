"""Command-line interface: ``python -m fx2_harness <command>``.

Commands
--------
``load [image]``
    Download the firmware ``.ihx`` into the FX2LP's RAM (default:
    ``cypress/firmware/build/harness_fx2.ihx``; override with ``$FX2_HARNESS_IMAGE``
    or the positional argument) and wait for the board to re-enumerate as
    ``04B4:8614``.
``boot``
    Ask the running firmware to hand USB back to the EZ-USB core, so the board
    is ``04B4:8613`` again (and reloadable from any host).
``info``       protocol/firmware version, pin count, caps, board id.
``read``       read all 24 line levels.
``pins``       per-pin configuration and level.
``set <index> <0|1>``   drive one OUTPUT line.
``dir <out|in>``        set the direction of all 24 lines.
``status``     device counters and uptime.
``walk``       walk a single high line through all 24 indices.
``selftest``   drive every line and read it back; exit code 1 on any failure.

Exit codes (same vocabulary as the RP2040 CLI)
=============================================  ===
0 success                                       0
1 unexpected error                              1
2 device not found                              2
4 protocol error                                4
5 timeout                                       5
6 bad command-line parameter                    6
7 device returned a non-zero status             7
=============================================  ===
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path
from typing import Final, Sequence

from rp2040_harness.errors import (
    BadParamError,
    DeviceNotFoundError,
    DeviceStatusError,
    HarnessError,
    HarnessTimeout,
    ParameterError,
    ProtocolError,
    TransportError,
)
from rp2040_harness.protocol import CAP_ATOMIC_MASK, CAP_PULL, DIR_OUTPUT, PIN_COUNT

from . import __version__
from .harness import Harness as Fx2Harness
from .loader import (
    default_image_path,
    find_app_device,
    load_firmware,
    reset_to_boot,
    wait_for,
)
from .transport import APP_PID, APP_VID, BOOT_PID, BOOT_VID, REQ_DEBUG

__all__ = ["main", "build_parser", "EXIT_CODES"]

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_NOT_FOUND: Final = 2
EXIT_PROTOCOL: Final = 4
EXIT_TIMEOUT: Final = 5
EXIT_PARAM: Final = 6
EXIT_STATUS: Final = 7

EXIT_CODES: Final[dict[str, int]] = {
    "ok": EXIT_OK,
    "error": EXIT_ERROR,
    "not-found": EXIT_NOT_FOUND,
    "protocol": EXIT_PROTOCOL,
    "timeout": EXIT_TIMEOUT,
    "parameter": EXIT_PARAM,
    "device-status": EXIT_STATUS,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fx2_harness",
        description=(
            "Control a Cypress FX2LP (CY7C68013A) 24-line harness that speaks "
            "the same wire protocol v1.0 as the RP2040 harness."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"fx2-harness {__version__}"
    )
    parser.add_argument("--vid", type=_hex_int, default=APP_VID)
    parser.add_argument("--pid", type=_hex_int, default=APP_PID)
    parser.add_argument("--timeout-ms", type=int, default=1000)

    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="download the firmware into the FX2LP RAM")
    load.add_argument(
        "image",
        nargs="?",
        default=None,
        help=f"Intel HEX image (default {default_image_path()})",
    )
    load.add_argument(
        "--no-wait", action="store_true", help="do not wait for re-enumeration"
    )
    load.add_argument(
        "--timeout", type=float, default=8.0, help="re-enumeration timeout, s"
    )

    sub.add_parser("boot", help="hand USB back to the EZ-USB core (04B4:8613)")
    sub.add_parser("info", help="protocol/firmware version, caps, board id")
    sub.add_parser("read", help="read all 24 line levels")
    sub.add_parser("pins", help="show every line's direction, level and flags")
    sub.add_parser("status", help="device counters and uptime")
    sub.add_parser("selftest", help="drive every line and read it back")
    sub.add_parser("debug", help="raw firmware diagnostics (extension request)")

    setp = sub.add_parser("set", help="drive one OUTPUT line")
    setp.add_argument("index", type=int)
    setp.add_argument("level", type=int, choices=[0, 1])

    direction = sub.add_parser("dir", help="set the direction of all 24 lines")
    direction.add_argument("which", choices=["out", "in"])

    walk = sub.add_parser("walk", help="walk one high line through all 24 indices")
    walk.add_argument("--dwell", type=int, default=120, help="ms per step")
    walk.add_argument(
        "--no-verify",
        action="store_true",
        help="do not read the levels back after every step",
    )
    return parser


def _hex_int(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from exc


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _connect(args, retries: int = 24):
    """Open the application device, retrying around a fresh re-enumeration."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return Fx2Harness.open(
                vid=args.vid, pid=args.pid, timeout_ms=args.timeout_ms
            )
        except (DeviceNotFoundError, TransportError) as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(0.25)
    assert last is not None
    raise last


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def _cmd_load(args) -> int:
    image = Path(args.image) if args.image else None
    result = load_firmware(
        image,
        timeout=args.timeout,
        wait_reenumerate=not args.no_wait,
    )
    for line in result.lines():
        print(line)
    if result.reset_from_app:
        print("boot      : application firmware released the bus")
    if result.direct_download:
        print("boot      : reprogrammed through the EZ-USB core (no replug)")
    if not result.reenumerated:
        print("state     : waiting for re-enumeration was skipped")
        return EXIT_OK
    print(f"running   : {APP_VID:04X}:{APP_PID:04X} (re-enumerated)")
    with _connect(args) as h:
        info = h.get_info(cached=False)
        h.ping()
        print(
            f"firmware  : {info.firmware_version} "
            f"proto {info.protocol_version} pins {info.pin_count} "
            f"caps 0x{info.caps:04X}"
        )
    return EXIT_OK


def _cmd_boot(args) -> int:
    device = find_app_device()
    if device is None:
        boot = wait_for(BOOT_VID, BOOT_PID, 0.5)
        if boot is not None:
            print(f"already at {BOOT_VID:04X}:{BOOT_PID:04X} (boot device)")
            return EXIT_OK
        raise DeviceNotFoundError(
            f"neither {APP_VID:04X}:{APP_PID:04X} nor "
            f"{BOOT_VID:04X}:{BOOT_PID:04X} is present"
        )
    reset_to_boot(device)
    boot = wait_for(BOOT_VID, BOOT_PID, 4.0)
    if boot is None:
        raise TransportError("the board did not come back as the boot device")
    print(f"boot      : {BOOT_VID:04X}:{BOOT_PID:04X} (re-enumerated)")
    return EXIT_OK


def _cmd_info(h, args) -> int:
    info = h.get_info(cached=False)
    print(f"device          : {h.transport.description}")
    print(f"protocol        : {info.protocol_version}")
    print(f"firmware        : {info.firmware_version}")
    print(f"pins            : {info.pin_count} (mask 0x{info.pin_mask:08X})")
    print(f"board id        : {info.serial}")
    print(f"reset interface : {info.reset_iface} (none: reset is a vendor request)")
    print(f"caps            : 0x{info.caps:08X} {', '.join(info.capabilities)}")
    print("note            : the FX2LP has no pull-ups, open-drain, drive-strength")
    print("                  or Schmitt control, so those caps are honestly clear")
    return EXIT_OK


def _cmd_read(h, args) -> int:
    value = h.read_all()
    print(f"0x{value:06X}  (0b{value:024b})")
    return EXIT_OK


def _cmd_pins(h, args) -> int:
    print("idx  line  dir   lvl  drive  slew   pull     schmitt  open-drain")
    for index in range(PIN_COUNT):
        st = h.get_pin(index)
        print(
            f"{st.index:^3}  {_line_name(st.index):<4}  {st.direction:<4}  "
            f"{st.level:^3}  {(st.drive_strength or '?'):<5}  "
            f"{(st.slew_rate or '?'):<5}  "
            f"{(st.pull or 'none'):<7}  {str(st.schmitt):<7}  "
            f"{str(st.open_drain)}"
        )
    print(
        "\n(flags/level are read back as written; the FX2LP pins have no "
        "pull/drive/slew/Schmitt hardware)"
    )
    return EXIT_OK


def _line_name(index: int) -> str:
    port = ("PA", "PB", "PD")[index >> 3]
    return f"{port}{index & 7}"


def _cmd_set(h, args) -> int:
    try:
        h.set_pin(args.index, args.level)
    except BadParamError as exc:
        raise ParameterError(
            f"{exc}; line {args.index} is not configured as OUTPUT -- run "
            "`python -m fx2_harness dir out` first (protocol.md 4.7 forbids "
            "an implicit direction change)"
        ) from exc
    print(f"line {args.index} ({_line_name(args.index)}) = {args.level}")
    print(f"read back 0x{h.read_all():06X}")
    return EXIT_OK


def _cmd_dir(h, args) -> int:
    direction = "out" if args.which == "out" else "in"
    applied = h.configure_many(
        [{"index": i, "direction": direction, "level": 0} for i in range(PIN_COUNT)]
    )
    print(f"{applied} lines configured as {direction.upper()}")
    if direction == "out":
        print(f"all lines low: read back 0x{h.read_all():06X}")
    return EXIT_OK


def _cmd_status(h, args) -> int:
    st = h.status()
    print(f"state        : {st.state_name} ({st.state})")
    print(f"last status  : {st.last_status_name} (0x{st.last_status:02X})")
    print(f"frames ok    : {st.frames_ok}")
    print(f"frames err   : {st.frames_err}")
    print(f"crc errors   : {st.crc_errors}")
    print(f"uptime       : {st.uptime_ms} ms")
    return EXIT_OK


def _cmd_debug(h, args) -> int:
    device = h.transport.device
    raw = bytes(device.ctrl_transfer(0xC0, REQ_DEBUG, 0, 0, 16, args.timeout_ms))
    if len(raw) != 16:
        raise ProtocolError(f"debug request returned {len(raw)} bytes, expected 16")
    print(f"OEA/OEB/OED  : {raw[0]:02X} {raw[1]:02X} {raw[2]:02X}")
    print(f"IOA/IOB/IOD  : {raw[3]:02X} {raw[4]:02X} {raw[5]:02X}")
    print(f"transport err: {raw[6]}")
    print(f"last status  : {raw[7]:02X}")
    print(f"ticks/ms     : {struct.unpack_from('<H', raw, 8)[0]}")
    print(f"SOF count    : {struct.unpack_from('<I', raw, 10)[0]}")
    print(f"streaming    : {raw[14]}")
    print(f"high speed   : {raw[15]}")
    return EXIT_OK


def _cmd_walk(h, args) -> int:
    if args.dwell < 0:
        raise ParameterError("--dwell must not be negative")
    h.configure_many(
        [{"index": i, "direction": "out", "level": 0} for i in range(PIN_COUNT)]
    )
    h.write_mask(0x00FFFFFF, 0x000000)
    failures = 0
    print(f"walk: 24 steps of {args.dwell} ms, all lines OUTPUT")
    try:
        for index in range(PIN_COUNT):
            h.set_pin(index, 1)
            value = h.read_all()
            ok = value == (1 << index)
            if args.no_verify:
                state = ""
            else:
                state = "ok" if ok else "MISMATCH"
                if not ok:
                    failures += 1
            print(
                f"  index {index:2d} ({_line_name(index)}) -> 0x{value:06X}"
                + (f"  {state}" if state else "")
            )
            time.sleep(max(args.dwell, 0) / 1000.0)
            h.set_pin(index, 0)
    finally:
        # Leave every line driven low: safe and still usable.
        h.write_mask(0x00FFFFFF, 0x000000)
    print(f"walk: {'OK' if failures == 0 else f'{failures} mismatches'}; lines left low")
    return EXIT_OK if failures == 0 else EXIT_STATUS


def _cmd_selftest(h, args) -> int:
    checks = 0
    failures = 0

    def check(condition, what: str) -> bool:
        nonlocal checks, failures
        checks += 1
        if condition:
            print(f"  ok   {what}")
        else:
            failures += 1
            print(f"  FAIL {what}")
        return bool(condition)

    print("identity")
    info = h.get_info(cached=False)
    check(
        info.proto_major == 1 and info.proto_minor == 0,
        f"protocol {info.protocol_version} (expected 1.0)",
    )
    check(info.pin_count == 24, f"pin_count {info.pin_count} (expected 24)")
    check(
        info.pin_mask == 0x00FFFFFF,
        f"pin_mask 0x{info.pin_mask:08X} (expected 0x00FFFFFF)",
    )
    check(bool(info.caps & CAP_ATOMIC_MASK), "caps advertise ATOMIC_MASK")
    check(not (info.caps & CAP_PULL), "caps do not claim pull-ups (honest caps)")
    nonce = 0x5A5A1234
    check(h.ping(nonce) == nonce, f"PING echoes nonce 0x{nonce:08X}")

    print("safe state")
    h.stop()
    directions = [h.get_pin(i).dir for i in range(PIN_COUNT)]
    check(
        all(d != DIR_OUTPUT for d in directions),
        "after STOP all 24 lines are INPUT",
    )

    print("directions and levels")
    applied = h.configure_many(
        [{"index": i, "direction": "out", "level": 0} for i in range(PIN_COUNT)]
    )
    check(applied == 24, f"CONFIGURE_MANY applied {applied} of 24 records")
    directions = [h.get_pin(i).dir for i in range(PIN_COUNT)]
    check(all(d == DIR_OUTPUT for d in directions), "all 24 lines read back OUTPUT")
    readback = h.read_all()
    check(readback == 0, f"all lines low (READ_ALL 0x{readback:06X})")

    for index in range(PIN_COUNT):
        h.set_pin(index, 1)
        value = h.read_all()
        check(
            value == (1 << index),
            f"index {index:2d} ({_line_name(index)}) alone high "
            f"(read 0x{value:06X})",
        )
        h.set_pin(index, 0)
        if h.read_all() != 0:
            check(False, f"index {index:2d} back low")

    print("masks and patterns")
    applied_value = h.write_mask(0x00FFFFFF, 0x00FFFFFF)
    check(applied_value == 0x00FFFFFF, f"SET_MASK all high -> 0x{applied_value:06X}")
    check(h.read_all() == 0x00FFFFFF, "READ_ALL all high")
    for pattern in (0x000000, 0x00FFFF, 0xAA55AA, 0x55AA55, 0x123456):
        h.write_mask(0x00FFFFFF, pattern)
        readback = h.read_all()
        check(
            readback == pattern,
            f"pattern 0x{pattern:06X} read back 0x{readback:06X}",
        )
    ports = 0xAA | (0x55 << 8) | (0xF0 << 16)
    h.write_mask(0x00FFFFFF, ports)
    readback = h.read_all()
    check(
        readback == ports,
        f"ports independent A=0xAA B=0x55 D=0xF0 (0x{readback:06X})",
    )
    h.write_mask(0x00FFFFFF, 0x00FFFFFF)
    h.write_mask(0x0000FF, 0x0000A5)
    readback = h.read_all()
    check(
        readback == 0xFFFFA5,
        f"masked update leaves other ports alone (0x{readback:06X})",
    )

    print("batch and streaming")
    h.write_mask(0x00FFFFFF, 0x000000)
    applied = h.set_pins({0: 1, 8: 1, 16: 1})
    check(applied == 3 and h.read_all() == 0x010101, "SET_MANY sets three lines")

    values = [0x00FFFFFF, 0x000000] * 4
    results = list(h.stream(values, 0x00FFFFFF, chunk=4))
    applied_total = sum(r.applied for r in results)
    duration_us = sum(r.duration_us for r in results)
    toggles = sum(r.toggles for r in results)
    check(
        applied_total == len(values),
        f"STREAM applied {applied_total} of {len(values)} values",
    )
    check(duration_us > 0, f"STREAM reported {duration_us} us of device time")
    check(toggles >= 24, f"STREAM counted {toggles} toggles")
    if duration_us > 0:
        print(
            f"       device rate {applied_total / (duration_us * 1e-6):,.0f} "
            f"updates/s"
        )

    print("configuration read-back")
    h.configure(5, direction="out", drive="8mA", slew="fast", level=1)
    st = h.get_pin(5)
    check(
        st.drive == 2 and st.slew == 1 and st.level == 1,
        f"GET_PIN reads back drive={st.drive_strength} slew={st.slew_rate} "
        f"level={st.level}",
    )
    h.configure(0, direction="in")
    st = h.get_pin(0)
    check(st.dir != DIR_OUTPUT, "CONFIGURE_PIN can turn a line back into INPUT")
    rejected = False
    try:
        h.set_pin(0, 1)
    except BadParamError:
        rejected = True
    check(rejected, "SET_PIN on an INPUT line is ERR_BAD_PARAM")

    print("safety")
    h.stop()
    directions = [h.get_pin(i).dir for i in range(PIN_COUNT)]
    check(
        all(d != DIR_OUTPUT for d in directions),
        "final STOP returns all 24 lines to INPUT",
    )

    print(f"\n{checks - failures}/{checks} checks passed")
    return EXIT_OK if failures == 0 else EXIT_STATUS


_COMMANDS = {
    "load": _cmd_load,
    "boot": _cmd_boot,
    "info": _cmd_info,
    "read": _cmd_read,
    "pins": _cmd_pins,
    "set": _cmd_set,
    "dir": _cmd_dir,
    "status": _cmd_status,
    "debug": _cmd_debug,
    "walk": _cmd_walk,
    "selftest": _cmd_selftest,
}

#: Commands that manage the USB device themselves (no Harness session first).
_DEVICE_COMMANDS = {"load", "boot"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = _COMMANDS[args.command]
    try:
        if args.command in _DEVICE_COMMANDS:
            return handler(args)
        with _connect(args) as harness:
            return handler(harness, args)
    except DeviceNotFoundError as exc:
        return _fail(EXIT_NOT_FOUND, exc)
    except HarnessTimeout as exc:
        return _fail(EXIT_TIMEOUT, exc)
    except DeviceStatusError as exc:
        return _fail(EXIT_STATUS, exc)
    except ProtocolError as exc:
        return _fail(EXIT_PROTOCOL, exc)
    except ParameterError as exc:
        return _fail(EXIT_PARAM, exc)
    except TransportError as exc:
        return _fail(EXIT_ERROR, exc)
    except HarnessError as exc:
        return _fail(EXIT_ERROR, exc)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


def _fail(code: int, exc: BaseException) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

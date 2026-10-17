"""Command-line interface: ``python -m rp2040_harness <command>``.

Exit codes
----------
======  ==================================================
0       success
1       unexpected error
2       device not found (:class:`DeviceNotFoundError`)
3       device busy / lock held (:class:`DeviceBusyError`)
4       protocol error (framing, CRC, seq, malformed reply)
5       timeout waiting for a reply
6       invalid command-line parameter
7       device returned a non-zero status
======  ==================================================
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Final, Sequence

from . import __version__
from . import protocol as P
from .device import DRIVE_VALUES, SLEW_VALUES, Harness, StreamResult
from .errors import (
    DeviceBusyError,
    DeviceNotFoundError,
    DeviceStatusError,
    HarnessError,
    HarnessTimeout,
    ParameterError,
    ProtocolError,
    TransportError,
)

__all__ = ["main", "build_parser", "EXIT_CODES"]

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_NOT_FOUND: Final = 2
EXIT_BUSY: Final = 3
EXIT_PROTOCOL: Final = 4
EXIT_TIMEOUT: Final = 5
EXIT_PARAM: Final = 6
EXIT_STATUS: Final = 7

EXIT_CODES: Final[dict[str, int]] = {
    "ok": EXIT_OK,
    "error": EXIT_ERROR,
    "not-found": EXIT_NOT_FOUND,
    "busy": EXIT_BUSY,
    "protocol": EXIT_PROTOCOL,
    "timeout": EXIT_TIMEOUT,
    "parameter": EXIT_PARAM,
    "device-status": EXIT_STATUS,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rp2040_harness",
        description="Control an RP2040 Harness (24 digital lines over USB).",
    )
    parser.add_argument(
        "--version", action="version", version=f"rp2040-harness {__version__}"
    )
    parser.add_argument(
        "--vid", type=_hex_int, default=0x2E8A, help="USB vendor id (default 0x2E8A)"
    )
    parser.add_argument(
        "--pid", type=_hex_int, default=0x10C0, help="USB product id (default 0x10C0)"
    )
    parser.add_argument("--serial", default=None, help="serial number to select")
    parser.add_argument(
        "--timeout-ms", type=int, default=1000, help="reply timeout (default 1000)"
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="do not take the cross-process device lock",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="show protocol/firmware version and board id")
    sub.add_parser("pins", help="show every pin's configuration and level")
    sub.add_parser("status", help="show device counters and stream state")
    sub.add_parser("stop", help="stop streaming and return all lines to INPUT")
    sub.add_parser("read", help="read all 24 input bits")

    get = sub.add_parser("get", help="show one pin")
    get.add_argument("index", type=int, help="logical pin index 0..23")

    cfg = sub.add_parser("configure", help="configure one pin")
    cfg.add_argument("index", type=int)
    cfg.add_argument("--dir", dest="direction", choices=["in", "out"], default=None)
    cfg.add_argument("--pull", choices=["up", "down", "none"], default=None)
    cfg.add_argument("--drive", choices=list(DRIVE_VALUES), default=None)
    cfg.add_argument("--slew", choices=list(SLEW_VALUES), default=None)
    cfg.add_argument("--schmitt", dest="schmitt", action="store_true", default=None)
    cfg.add_argument("--no-schmitt", dest="schmitt", action="store_false")
    cfg.add_argument("--open-drain", action="store_true", default=False)
    cfg.add_argument("--level", type=int, choices=[0, 1], default=None)

    setp = sub.add_parser("set", help="set one output pin to 0 or 1")
    setp.add_argument("index", type=int)
    setp.add_argument("level", type=int, choices=[0, 1])

    mask = sub.add_parser("mask", help="atomic masked write")
    mask.add_argument("hexmask", type=_hex_int, help="bit mask, e.g. 0x0000FF")
    mask.add_argument("hexvalue", type=_hex_int, help="new values, e.g. 0x0000A5")

    bench = sub.add_parser("bench", help="throughput probe using STREAM")
    bench.add_argument(
        "--values", type=int, default=25000, help="total values to stream"
    )
    bench.add_argument(
        "--chunk", type=int, default=250, help="values per STREAM frame (1..250)"
    )
    bench.add_argument("--mask", type=_hex_int, default=0x00FFFFFF, help="line mask")
    bench.add_argument(
        "--pin", type=int, default=None, help="stream only this pin (sets --mask)"
    )
    bench.add_argument(
        "--counter", action="store_true", help="stream an incrementing 24-bit counter"
    )
    return parser


def _hex_int(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not an integer (use decimal, 0x hex, or 0b binary)"
        ) from exc


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _cmd_info(h: Harness, args) -> int:
    info = h.get_info(cached=False)
    print(f"device          : {h.transport.description}")
    print(f"protocol        : {info.protocol_version}")
    print(f"firmware        : {info.firmware_version}")
    print(f"pins            : {info.pin_count} (mask 0x{info.pin_mask:08X})")
    print(f"board id        : {info.serial}")
    print(f"reset interface : {info.reset_iface}")
    print(f"caps            : 0x{info.caps:08X} {', '.join(info.capabilities)}")
    return EXIT_OK


def _cmd_get(h: Harness, args) -> int:
    st = h.get_pin(args.index)
    _print_pin(st)
    return EXIT_OK


def _cmd_pins(h: Harness, args) -> int:
    print(
        "idx  dir   lvl  pull  drive  slew   schmitt  open-drain"
    )
    for index in range(P.PIN_COUNT):
        st = h.get_pin(index)
        print(
            f"{st.index:^3}  {st.direction:<4}  {st.level:^3}  "
            f"{(st.pull or 'none'):<4}  {(st.drive_strength or '?'):<5}  "
            f"{(st.slew_rate or '?'):<5}  {str(st.schmitt):<7}  "
            f"{str(st.open_drain)}"
        )
    return EXIT_OK


def _print_pin(st) -> None:
    print(f"index       : {st.index}")
    print(f"direction   : {st.direction}")
    print(f"level       : {st.level}")
    print(f"pull        : {st.pull or 'none'}")
    print(f"drive       : {st.drive_strength or f'unknown({st.drive})'}")
    print(f"slew        : {st.slew_rate or f'unknown({st.slew})'}")
    print(f"schmitt     : {st.schmitt}")
    print(f"open-drain  : {st.open_drain}")
    print(f"flags       : 0x{st.flags:02X}")


def _cmd_read(h: Harness, args) -> int:
    value = h.read_all()
    print(f"0x{value:06X}  (0b{value:024b})")
    return EXIT_OK


def _cmd_configure(h: Harness, args) -> int:
    # Start from the current state, then apply only the flags the user gave.
    current = h.get_pin(args.index)
    direction = args.direction if args.direction is not None else current.direction
    pull = args.pull if args.pull is not None else current.pull
    drive = args.drive if args.drive is not None else (current.drive_strength or "4mA")
    slew = args.slew if args.slew is not None else (current.slew_rate or "slow")
    schmitt = args.schmitt if args.schmitt is not None else current.schmitt
    level = args.level if args.level is not None else current.level
    h.configure(
        args.index,
        direction=direction,
        pull=pull,
        drive=drive,
        slew=slew,
        schmitt=schmitt,
        open_drain=args.open_drain or current.open_drain,
        level=level,
    )
    _print_pin(h.get_pin(args.index))
    return EXIT_OK


def _cmd_set(h: Harness, args) -> int:
    h.set_pin(args.index, args.level)
    print(f"pin {args.index} = {args.level}")
    return EXIT_OK


def _cmd_mask(h: Harness, args) -> int:
    applied = h.write_mask(mask=args.hexmask, value=args.hexvalue)
    print(f"applied 0x{applied:06X}  (0b{applied:024b})")
    return EXIT_OK


def _cmd_status(h: Harness, args) -> int:
    st = h.status()
    print(f"state        : {st.state_name} ({st.state})")
    print(f"last status  : {st.last_status_name} (0x{st.last_status:02X})")
    print(f"frames ok    : {st.frames_ok}")
    print(f"frames err   : {st.frames_err}")
    print(f"crc errors   : {st.crc_errors}")
    print(f"uptime       : {st.uptime_ms} ms")
    return EXIT_OK


def _cmd_stop(h: Harness, args) -> int:
    h.stop()
    print("stopped; all lines are INPUT without pull-ups")
    return EXIT_OK


def _cmd_bench(h: Harness, args) -> int:
    mask = args.mask
    if args.pin is not None:
        if not 0 <= args.pin < P.PIN_COUNT:
            raise ParameterError(
                f"--pin must be 0..{P.PIN_COUNT - 1}, got {args.pin}"
            )
        mask = 1 << args.pin
    if not 1 <= args.values:
        raise ParameterError(f"--values must be positive, got {args.values}")
    if not 1 <= args.chunk <= P.STREAM_COUNT_MAX:
        raise ParameterError(
            f"--chunk must be 1..{P.STREAM_COUNT_MAX}, got {args.chunk}"
        )

    # Make every masked line an OUTPUT before streaming.
    for i in range(P.PIN_COUNT):
        if mask >> i & 1:
            h.configure(i, direction="out", drive="8mA", slew="fast")

    if args.counter:
        values = (i & P.ALL_PINS_MASK for i in range(args.values))
    else:
        # Toggle every masked line as hard as the protocol allows.
        values = (0 if i % 2 == 0 else mask for i in range(args.values))

    started = time.monotonic()
    last: StreamResult | None = None
    total = 0
    for result in h.stream(values, mask, chunk=args.chunk):
        last = result
        total = result.total_applied
        print(
            f"chunk {result.chunk_index:>3}: {result.applied:>3} updates, "
            f"{result.duration_us:>6} us, {result.toggles:>6} toggles, "
            f"{result.updates_per_second:>12,.0f} upd/s"
        )
    elapsed = time.monotonic() - started
    if last is None:
        print("no data streamed")
        return EXIT_OK
    print("-" * 72)
    print(f"device time   : {last.total_duration_us} us")
    print(f"wall time     : {elapsed * 1e6:.0f} us")
    print(f"values        : {total}")
    print(f"device rate   : {total / (last.total_duration_us * 1e-6):,.0f} updates/s")
    print(f"end-to-end    : {total / elapsed:,.0f} updates/s")
    print(f"mask          : 0x{mask:06X}")
    return EXIT_OK


_COMMANDS = {
    "info": _cmd_info,
    "pins": _cmd_pins,
    "get": _cmd_get,
    "read": _cmd_read,
    "configure": _cmd_configure,
    "set": _cmd_set,
    "mask": _cmd_mask,
    "status": _cmd_status,
    "stop": _cmd_stop,
    "bench": _cmd_bench,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.  Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = _COMMANDS[args.command]
    try:
        with Harness.open(
            vid=args.vid,
            pid=args.pid,
            serial=args.serial,
            timeout_ms=args.timeout_ms,
            lock=not args.no_lock,
        ) as harness:
            return handler(harness, args)
    except DeviceNotFoundError as exc:
        return _fail(EXIT_NOT_FOUND, exc)
    except DeviceBusyError as exc:
        return _fail(EXIT_BUSY, exc)
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

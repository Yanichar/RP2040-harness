#!/usr/bin/env python3
"""Read the harness firmware's USB state over EP0 (vendor control request 0x42).

Why this exists: when the bulk endpoints stop responding, control transfers keep
working. This is the only way to see *why* a device went deaf without a debug
probe attached, and ``--rearm`` is the corresponding recovery.

This deliberately talks to the device with raw pyusb rather than through
``rp2040_harness``: it must not take the exclusive device lock, and it must work
even when the bulk transport is broken.

Usage::

    python tests/harness_diag.py                 # print the state snapshot
    python tests/harness_diag.py --rearm         # try to re-arm the OUT endpoint
    python tests/harness_diag.py --json
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from typing import Any, Sequence

VID = 0x2E8A
PID = 0x10C0

CTRL_GET_STATE = 0x42
CTRL_REARM = 0x43
STATE_MAGIC = 0xA5

# magic, version, struct_len, then 12 bytes of flags, then 15 counters.
STATE_STRUCT = struct.Struct("<BBH12B15I")
STATE_FIELDS = [
    "magic",
    "version",
    "struct_len",
    "tud_ready",
    "tud_mounted",
    "itf_num",
    "ep_in",
    "ep_out",
    "out_armed",
    "out_hw_busy",
    "tx_busy",
    "tx_in_zlp",
    "rx_have",
    "last_result",
    "reserved0",
    "init_count",
    "open_count",
    "reset_count",
    "out_cb_count",
    "in_cb_count",
    "out_rearm_ok",
    "out_rearm_fail",
    "in_start_ok",
    "in_start_fail",
    "in_dropped",
    "rx_bytes",
    "frames_ok",
    "frames_err",
    "crc_errors",
    "uptime_ms",
]


def open_device():
    import libusb_package
    import usb.core

    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        raise SystemExit(f"no device {VID:04X}:{PID:04X} found")
    return dev


HEADER_STRUCT = struct.Struct("<BBH")


def read_state(dev) -> dict[str, Any]:
    """Read the state snapshot in two steps.

    The first request asks for only the 4-byte header, so it is always valid no
    matter which struct version the firmware implements; the second asks for
    exactly the length the device reports. Asking for more than the device has is
    what a naive single request would do, and an abandoned control IN transfer can
    leave EP0 stuck — so the length is never guessed.
    """
    header = bytes(dev.ctrl_transfer(0xC0, CTRL_GET_STATE, 0, 0, HEADER_STRUCT.size,
                                     timeout=2000))
    magic, version, struct_len = HEADER_STRUCT.unpack_from(header, 0)
    if magic != STATE_MAGIC:
        raise SystemExit(f"bad state magic 0x{magic:02X} (expected 0xA5)")

    length = min(struct_len, STATE_STRUCT.size)
    raw = bytes(dev.ctrl_transfer(0xC0, CTRL_GET_STATE, 0, 0, length, timeout=2000))
    if len(raw) < STATE_STRUCT.size:
        raw = raw + bytes(STATE_STRUCT.size - len(raw))

    state = dict(zip(STATE_FIELDS, STATE_STRUCT.unpack(raw)))
    state["_bytes_read"] = length
    state["_firmware_struct_len"] = struct_len
    return state


def rearm(dev) -> None:
    dev.ctrl_transfer(0x40, CTRL_REARM, 0, 0, None, timeout=2000)


def format_state(state: dict[str, Any]) -> str:
    flags = [
        "tud_ready",
        "tud_mounted",
        "itf_num",
        "ep_in",
        "ep_out",
        "out_armed",
        "out_hw_busy",
        "tx_busy",
        "tx_in_zlp",
        "rx_have",
        "last_result",
    ]
    counters = [
        "init_count",
        "open_count",
        "reset_count",
        "out_cb_count",
        "in_cb_count",
        "out_rearm_ok",
        "out_rearm_fail",
        "in_start_ok",
        "in_start_fail",
        "in_dropped",
        "rx_bytes",
        "frames_ok",
        "frames_err",
        "crc_errors",
        "uptime_ms",
    ]
    lines = [f"firmware USB state (struct {state['struct_len']} B, v{state['version']})"]
    for key in flags:
        value = state[key]
        shown = f"0x{value:02X}" if key in ("itf_num", "ep_in", "ep_out", "last_result") else value
        lines.append(f"  {key:14s}: {shown}")
    lines.append("  --- counters ---")
    for key in counters:
        lines.append(f"  {key:14s}: {state[key]:,}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rearm", action="store_true",
                        help="send HARNESS_CTRL_REARM before reading the state")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    dev = open_device()
    if args.rearm:
        try:
            rearm(dev)
            print("rearm request sent")
        except Exception as exc:  # noqa: BLE001
            print(f"rearm failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    state = read_state(dev)
    if args.json:
        print(json.dumps(state, indent=2))
    else:
        print(format_state(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

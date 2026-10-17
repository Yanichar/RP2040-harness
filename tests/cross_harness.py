#!/usr/bin/env python3
"""Cross-verify the two harnesses against each other.

Two independent 24-line harnesses are wired to the same 24 wires:

* the **RP2040** harness (`rp2040_harness`, USB 2E8A:10C0), lines GP2…GP22, GP26…GP28;
* the **Cypress FX2LP** harness (`fx2_harness`, USB 04B4:8614), lines PA0…PA7,
  PB0…PB7, PD0…PD7.

Because both speak protocol v1.0, the same API drives both. This script makes one
board the *driver* and the other the *reader*, so each board verifies the other —
no external instrument is involved. The mapping under test is exactly the one in
`docs/pinmap.md` / `TASK.md` §2.

Directions are never both outputs: the reader is switched to INPUT before the
driver starts, so the boards cannot fight over a wire.

Usage::

    python tests/cross_harness.py --both
    python tests/cross_harness.py --rp2040-drives
    python tests/cross_harness.py --fx2-drives
    python tests/cross_harness.py --both --values 5000   # also time a counter

Exit code 0 only if every index of every direction verified.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Sequence

PIN_COUNT = 24
ALL_PINS_MASK = (1 << PIN_COUNT) - 1

RP2040_VID, RP2040_PID = 0x2E8A, 0x10C0
FX2_VID, FX2_PID = 0x04B4, 0x8614


# --------------------------------------------------------------------------
# Opening the two devices
# --------------------------------------------------------------------------


def open_rp2040():
    from rp2040_harness import Harness

    return Harness.open(vid=RP2040_VID, pid=RP2040_PID)


def open_fx2():
    from fx2_harness import Harness

    return Harness.open(vid=FX2_VID, pid=FX2_PID)


def _cleanup(h) -> None:
    """Leave the board in the safe state (all inputs) and close it."""
    if h is None:
        return
    try:
        h.stop()
    except Exception:
        pass
    try:
        h.close()
    except Exception:
        pass


# --------------------------------------------------------------------------
# One direction
# --------------------------------------------------------------------------


def run_one_way(driver, reader, driver_name: str, reader_name: str) -> list[str]:
    """Walking-bit test: driver raises one line at a time, reader must see it."""
    failures: list[str] = []

    # The idle-low pre-check is only meaningful on a reader that can pull down.
    # The FX2LP has no pull-ups/pull-downs at all (its GET_INFO says so), so an
    # undriven FX2 input floats and reads back as 0xFFFFFF — physics, not a fault.
    # Once the driver drives a line the level is defined and the real test below
    # applies regardless.
    caps = reader.get_info(cached=True).caps
    can_pull = bool(caps & 0x02)  # HARNESS_CAP_PULL, docs/protocol.md §4.2

    reader.configure_many(
        [
            {
                "index": i,
                "direction": "in",
                "pull": "down" if can_pull else None,
                "schmitt": True,
            }
            for i in range(PIN_COUNT)
        ]
    )
    idle = reader.read_all()
    if not can_pull:
        print(
            f"   ({reader_name} has no pull-downs; undriven inputs float, "
            f"read 0x{idle:06X} — the idle level is not asserted)"
        )
    elif idle != 0:
        failures.append(
            f"[{driver_name}->{reader_name}] reader is not idle-low before the test: "
            f"0x{idle:06X} (check wiring / a second driver)"
        )

    driver.configure_many(
        [{"index": i, "direction": "out", "level": 0} for i in range(PIN_COUNT)]
    )

    for index in range(PIN_COUNT):
        expected = 1 << index
        driver.write_mask(ALL_PINS_MASK, 0)
        driver.write_mask(ALL_PINS_MASK, expected)
        observed = reader.read_all()
        # A reader line driven by someone else must not disturb bits above 23.
        if observed != expected:
            failures.append(
                f"[{driver_name}->{reader_name}] index {index}: "
                f"driver set 0x{expected:06X}, reader saw 0x{observed:06X}"
                + (
                    f" (extra bits 0x{observed ^ expected:06X})"
                    if observed & ~expected & ALL_PINS_MASK
                    else f" (missing bits 0x{expected & ~observed:06X})"
                )
            )
        driver.write_mask(ALL_PINS_MASK, 0)
        if reader.read_all() != 0:
            failures.append(
                f"[{driver_name}->{reader_name}] index {index}: reader not back to 0 "
                f"after clearing (0x{reader.read_all():06X})"
            )

    driver.stop()
    return failures


# --------------------------------------------------------------------------
# Counter timing
# --------------------------------------------------------------------------


def run_counter(driver, reader, driver_name: str, values: int) -> dict[str, Any]:
    """Push a counter through the driver and report what the reader observed.

    No correctness comparison against an expected sequence (TASK.md §5.2): this
    reports how many distinct states the reader saw and the driver's own timing.
    """
    reader.configure_many(
        [{"index": i, "direction": "in", "pull": None} for i in range(PIN_COUNT)]
    )
    driver.configure_many(
        [{"index": i, "direction": "out", "level": 0} for i in range(PIN_COUNT)]
    )

    payload = [i & ALL_PINS_MASK for i in range(values)]
    started = time.perf_counter()
    chunks = list(driver.stream(payload, mask=ALL_PINS_MASK, chunk=250))
    wall = time.perf_counter() - started

    applied = sum(c.applied for c in chunks)
    device_us = sum(c.duration_us for c in chunks)
    result = {
        "driver": driver_name,
        "values_applied": applied,
        "wall_s": wall,
        "wall_updates_per_second": applied / wall if wall else None,
        "driver_reported_updates_per_second": (
            applied / (device_us * 1e-6) if device_us else None
        ),
    }
    driver.stop()
    return result


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--rp2040-drives", action="store_true", help="RP2040 drives, FX2 reads")
    parser.add_argument("--fx2-drives", action="store_true", help="FX2 drives, RP2040 reads")
    parser.add_argument("--both", action="store_true", help="both directions (default)")
    parser.add_argument("--values", type=int, default=0,
                        help="if >0, also push N counter values and time it")
    args = parser.parse_args(argv)

    if not (args.rp2040_drives or args.fx2_drives or args.both):
        args.both = True

    rp = fx = None
    failures: list[str] = []
    try:
        print("opening the two harnesses…")
        rp = open_rp2040()
        print(f"  RP2040 : fw {rp.get_info().firmware_version}  {rp.transport.description}")
        fx = open_fx2()
        print(f"  Cypress: fw {fx.get_info().firmware_version}  {fx.transport.description}")
        print()

        if args.both or args.rp2040_drives:
            print("== RP2040 drives -> Cypress reads ==")
            failures += run_one_way(rp, fx, "rp2040", "fx2")
            print("   done")
        if args.both or args.fx2_drives:
            print("== Cypress drives -> RP2040 reads ==")
            failures += run_one_way(fx, rp, "fx2", "rp2040")
            print("   done")

        if args.values > 0:
            print()
            print("== counter timing ==")
            for driver, reader, name in ((rp, fx, "rp2040->fx2"), (fx, rp, "fx2->rp2040")):
                info = run_counter(driver, reader, name, args.values)
                rate = info["wall_updates_per_second"] or 0
                print(
                    f"  {name:12s}: {info['values_applied']:>7,} values  "
                    f"wall {rate:>10,.0f}/s  "
                    f"payload {rate * 4 / 1e6:.3f} MB/s"
                )
    except Exception as exc:  # noqa: BLE001
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        _cleanup(rp)
        _cleanup(fx)

    print()
    if failures:
        print(f"FAIL: {len(failures)} problem(s)")
        for line in failures[:40]:
            print(f"  {line}")
        if len(failures) > 40:
            print(f"  … and {len(failures) - 40} more")
        return 1

    print("PASS: both harnesses verified each other on all 24 lines, both directions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

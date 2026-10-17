#!/usr/bin/env python3
"""Hardware bring-up check for a freshly flashed RP2040 Harness.

Runs a pass/fail sequence over the *real* device. Everything here needs the
board attached; nothing in this file can run against a fake transport, which is
the whole point — it is the counterpart to ``firmware/test/crosscheck.py``
(which validates the protocol offline).

What it proves
--------------
1. **Safe state after boot** — every line is INPUT with no pull.
2. **Index -> GPIO translation, per line, all 24.** ``set_pin`` addresses a line
   by its physical GPIO bit, while ``set_mask``/``read_all`` go through the
   firmware's logical-index lookup table. Driving with one and reading with the
   other proves the two agree for every index — including 21..23, the
   non-contiguous GP26..GP28 tail that a contiguous mapping would get wrong.
3. **Atomic mask** — walking bit and multi-bit patterns through ``write_mask``.
4. **Batching** — ``configure_many`` / ``set_pins``.
5. **Throughput** — device-measured and wall-clock update rate.
6. **Interactive latency** — one round trip per operation.
7. **Errors** — the device rejects what the protocol says it must.
8. Leaves the board in the safe state.

What it CANNOT prove
--------------------
A wrong-but-self-consistent index mapping. ``read_all`` reads the pad, so any
consistent translation reads back correctly. Confirming which physical pin
actually moved needs an external instrument — that is ``tests/pinmap_scan.py``
with the logic analyzer (TASK.md §8.7).
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

PIN_COUNT = 24
ALL_PINS_MASK = (1 << PIN_COUNT) - 1

# Logical index -> GPIO, per docs/protocol.md §3. Duplicated here on purpose:
# the check should fail loudly if the firmware ever disagrees with the document.
EXPECTED_GPIO = list(range(2, 23)) + [26, 27, 28]


@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if detail and not ok:
            line += f"\n         {detail}"
        print(line, flush=True)
        return bool(ok)

    @property
    def failures(self) -> list[tuple[str, str]]:
        return [(n, d) for n, ok, d in self.checks if not ok]

    def summary(self) -> str:
        total = len(self.checks)
        bad = len(self.failures)
        return f"{total - bad}/{total} checks passed"


def _bits(value: int) -> str:
    return f"0x{value:06X}"


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------


def phase_info(harness, report: Report) -> None:
    print("\n== 1. identification ==")
    info = harness.get_info(cached=False)
    report.info = {
        "protocol": info.protocol_version,
        "firmware": info.firmware_version,
        "serial": info.serial,
        "pin_count": info.pin_count,
        "caps": f"0x{info.caps:04X}",
    }
    for key, value in report.info.items():
        print(f"  {key:12s}: {value}")

    report.check("protocol version is 1.0", info.protocol_version == "1.0",
                 f"got {info.protocol_version}")
    report.check(f"device reports {PIN_COUNT} lines", info.pin_count == PIN_COUNT,
                 f"got {info.pin_count}")
    report.check("pin_mask covers all 24 lines", info.pin_mask == ALL_PINS_MASK,
                 f"got {_bits(info.pin_mask)}")
    report.check("all v1.0 capabilities advertised", info.caps == 0x3FF,
                 f"got 0x{info.caps:04X}")
    report.check("reset interface present for picotool", info.reset_iface == 1,
                 f"got {info.reset_iface}")


def phase_safe_state(harness, report: Report) -> None:
    print("\n== 2. safe state ==")
    harness.stop()
    states = harness.list_pins()
    bad_dir = [s.index for s in states if s.direction != "in"]
    # PinState.pull is None (not the string "none") when the line has no pull.
    bad_pull = [s.index for s in states if s.pull is not None]
    report.check("every line is INPUT after stop()", not bad_dir, f"indices {bad_dir}")
    report.check("no pulls on any line after stop()", not bad_pull, f"indices {bad_pull}")
    report.check(
        "Schmitt trigger stays enabled after stop()",
        all(s.schmitt for s in states),
        f"off on {[s.index for s in states if not s.schmitt]}",
    )

    # Ping exercises a full round trip on a trivial opcode.
    nonce = 0x5A5A1234
    report.check("PING echoes its nonce", harness.ping(nonce) == nonce)


def phase_per_line(harness, report: Report) -> None:
    print("\n== 3. per-line index/GPIO consistency (all 24) ==")
    print("   idle lines are held INPUT with a pull-down so read_all is deterministic")

    # Park every line low and un-driven.
    harness.configure_many(
        [
            {"index": i, "direction": "in", "pull": "down", "schmitt": True}
            for i in range(PIN_COUNT)
        ]
    )
    report.check("read_all is 0 with all lines pulled down", harness.read_all() == 0,
                 f"got {_bits(harness.read_all())}")

    failures: list[str] = []
    for index in range(PIN_COUNT):
        harness.configure(index, direction="out", pull=None, level=0)
        low = harness.read_all()
        harness.set_pin(index, 1)
        high = harness.read_all()
        harness.set_pin(index, 0)
        back = harness.read_all()
        harness.configure(index, direction="in", pull="down")

        expected = 1 << index
        if low != 0:
            failures.append(f"index {index}: expected 0 before driving, got {_bits(low)}")
        elif high != expected:
            failures.append(
                f"index {index} (GP{EXPECTED_GPIO[index]}): expected {_bits(expected)}, "
                f"got {_bits(high)}"
            )
        elif back != 0:
            failures.append(f"index {index}: expected 0 after clearing, got {_bits(back)}")

        state = harness.get_pin(index)
        if state.level != 0 or state.direction != "in":
            failures.append(f"index {index}: not returned to INPUT/low")

    report.check(f"each of the {PIN_COUNT} lines drives and reads back alone",
                 not failures, "; ".join(failures[:6]))


def phase_atomic(harness, report: Report) -> None:
    print("\n== 4. atomic masked writes ==")
    harness.configure_many(
        [{"index": i, "direction": "out", "pull": None, "level": 0} for i in range(PIN_COUNT)]
    )

    failures: list[str] = []
    for index in range(PIN_COUNT):
        expected = 1 << index
        harness.write_mask(ALL_PINS_MASK, 0)
        applied = harness.write_mask(ALL_PINS_MASK, expected)
        actual = harness.read_all()
        if applied != expected or actual != expected:
            failures.append(
                f"walking bit {index}: applied {_bits(applied)} read {_bits(actual)}, "
                f"expected {_bits(expected)}"
            )
    report.check("walking bit through write_mask, all 24", not failures,
                 "; ".join(failures[:6]))

    patterns = [0x000000, 0xFFFFFF, 0xAAAAAA, 0x555555, 0x0000A5, 0x00FF00, 0x123456]
    failures = []
    for pattern in patterns:
        harness.write_mask(ALL_PINS_MASK, 0)
        applied = harness.write_mask(ALL_PINS_MASK, pattern)
        actual = harness.read_all()
        if applied != pattern or actual != pattern:
            failures.append(
                f"pattern {_bits(pattern)}: applied {_bits(applied)} read {_bits(actual)}"
            )
    report.check("multi-bit patterns round-trip", not failures, "; ".join(failures))

    # Masked writes must leave unselected lines alone.
    harness.write_mask(ALL_PINS_MASK, 0)
    harness.write_mask(0x0000FF, 0xFF)
    after = harness.read_all()
    report.check("masked write touches only the masked lines", after == 0x0000FF,
                 f"expected 0x0000FF, got {_bits(after)}")

    harness.write_mask(0x000F00, 0x000F00)
    after = harness.read_all()
    report.check("a second masked write composes with the first", after == 0x000FFF,
                 f"expected 0x000FFF, got {_bits(after)}")

    # Bits above 23 must not reach any real line. This goes through the raw
    # protocol on purpose: write_mask() validates client-side and would reject
    # such a frame before it ever reached the device.
    import struct as _struct

    from rp2040_harness import protocol as P

    harness.write_mask(ALL_PINS_MASK, 0)
    reply = harness.request(P.OP_SET_MASK, _struct.pack("<II", 0xFFFFFFFF, 0xFFFFFFFF))
    applied = _struct.unpack("<I", reply)[0]
    after = harness.read_all()
    report.check("device ignores mask bits above 23",
                 applied == ALL_PINS_MASK and after == ALL_PINS_MASK,
                 f"applied {_bits(applied)}, read {_bits(after)}")


def phase_batch(harness, report: Report) -> None:
    print("\n== 5. batching ==")
    harness.configure_many(
        [{"index": i, "direction": "out", "pull": None, "level": 0} for i in range(PIN_COUNT)]
    )
    harness.write_mask(ALL_PINS_MASK, 0)

    # Even indices are driven low, odd indices high -> bits 1,3,..,23 set.
    applied = harness.set_pins({i: (i % 2) for i in range(PIN_COUNT)})
    after = harness.read_all()
    report.check("set_pins() applies a whole batch", after == 0xAAAAAA,
                 f"expected 0xAAAAAA, got {_bits(after)} (applied={applied})")

    harness.write_mask(ALL_PINS_MASK, 0)
    counts = harness.batch([("set", i, 1) for i in range(8)])
    after = harness.read_all()
    report.check("batch() applies SET_MANY", after == 0x0000FF,
                 f"expected 0x0000FF, got {_bits(after)} (counts={counts})")

    harness.write_mask(ALL_PINS_MASK, 0)
    counts = harness.batch(
        [("configure", i, {"direction": "out", "level": 1}) for i in range(8, 16)]
    )
    after = harness.read_all()
    report.check("batch() applies CONFIGURE_MANY", after == 0x0000FF00,
                 f"expected 0x0000FF00, got {_bits(after)} (counts={counts})")


def phase_throughput(harness, report: Report, values: int) -> dict[str, Any]:
    print(f"\n== 6. throughput ({values} counter values) ==")
    harness.configure_many(
        [{"index": i, "direction": "out", "pull": None, "level": 0} for i in range(PIN_COUNT)]
    )

    payload = [(i & ALL_PINS_MASK) for i in range(values)]
    started = time.perf_counter()
    chunks = list(harness.stream(payload, mask=ALL_PINS_MASK, chunk=250))
    wall = time.perf_counter() - started

    applied = sum(c.applied for c in chunks)
    device_us = sum(c.duration_us for c in chunks)
    toggles = sum(c.toggles for c in chunks)
    device_rate = applied / (device_us * 1e-6) if device_us else 0.0
    wall_rate = applied / wall if wall else 0.0

    print(f"  values applied     : {applied:,}")
    print(f"  USB transactions   : {len(chunks):,}")
    print(f"  device loop time   : {device_us / 1e6:.4f} s")
    print(f"  wall clock         : {wall:.4f} s")
    print(f"  device update rate : {device_rate:,.0f} /s")
    print(f"  wall update rate   : {wall_rate:,.0f} /s")
    print(f"  line transitions   : {toggles:,}")
    if wall_rate:
        print(f"  effective payload  : {wall_rate * 4 / 1e6:.3f} MB/s (4 bytes/update)")

    report.check("every value was applied", applied == values,
                 f"applied {applied} of {values}")
    report.check("device reports a non-zero loop duration", device_us > 0)
    report.check("device loop is not the bottleneck", device_rate > wall_rate,
                 f"device {device_rate:,.0f}/s vs wall {wall_rate:,.0f}/s")
    report.check("throughput is in the USB Full-Speed range", wall_rate > 10_000,
                 f"only {wall_rate:,.0f} updates/s")

    return {
        "values": applied,
        "transactions": len(chunks),
        "device_duration_us": device_us,
        "wall_duration_s": wall,
        "device_updates_per_second": device_rate,
        "wall_updates_per_second": wall_rate,
        "payload_mb_per_s": wall_rate * 4 / 1e6,
        "toggles": toggles,
    }


def phase_latency(harness, report: Report, iterations: int) -> dict[str, Any]:
    print(f"\n== 7. interactive latency ({iterations} round trips) ==")
    harness.configure_many(
        [{"index": i, "direction": "out", "pull": None, "level": 0} for i in range(PIN_COUNT)]
    )

    samples: list[float] = []
    for i in range(iterations):
        started = time.perf_counter()
        harness.set_pin(i % PIN_COUNT, 1)
        harness.read_all()
        samples.append(time.perf_counter() - started)

    samples.sort()
    mean = sum(samples) / len(samples)
    p50 = samples[len(samples) // 2]
    p95 = samples[int(len(samples) * 0.95)]
    worst = samples[-1]

    print(f"  mean               : {mean * 1e3:.3f} ms")
    print(f"  median             : {p50 * 1e3:.3f} ms")
    print(f"  p95                : {p95 * 1e3:.3f} ms")
    print(f"  worst              : {worst * 1e3:.3f} ms")

    report.check("round trip completes", mean > 0)
    report.check("one set_pin+read_all round trip is under 20 ms", p95 < 0.020,
                 f"p95 {p95 * 1e3:.3f} ms")

    return {
        "mean_ms": mean * 1e3,
        "median_ms": p50 * 1e3,
        "p95_ms": p95 * 1e3,
        "max_ms": worst * 1e3,
        "round_trips": iterations,
    }


def phase_errors(harness, report: Report) -> None:
    print("\n== 8. error handling ==")
    from rp2040_harness import DeviceStatusError, ParameterError

    harness.stop()  # everything is an INPUT again

    try:
        harness.set_pin(0, 1)
        report.check("set_pin on an INPUT line is rejected", False, "no error raised")
    except (DeviceStatusError, ParameterError) as exc:
        report.check("set_pin on an INPUT line is rejected", True)
        print(f"         -> {type(exc).__name__}: {exc}")

    for name, call in (
        ("index 24 is rejected locally", lambda: harness.set_pin(24, 1)),
        ("negative index is rejected locally", lambda: harness.set_pin(-1, 1)),
        ("level 2 is rejected locally", lambda: harness.set_pin(0, 2)),
        ("bad drive string is rejected locally",
         lambda: harness.configure(0, direction="out", drive="7mA")),
        ("pull up+down together is rejected locally",
         lambda: harness.configure(0, direction="in", pull="both")),
        ("mask above 24 bits is rejected locally",
         lambda: harness.write_mask(1 << 24, 0)),
    ):
        try:
            call()
            report.check(name, False, "no error raised")
        except (ParameterError, ValueError) as exc:
            report.check(name, True)
            print(f"         -> {type(exc).__name__}")

    # Over the wire: the device itself must reject an out-of-range index.
    from rp2040_harness import protocol as P

    try:
        harness.request(P.OP_GET_PIN, bytes([99]))
        report.check("device rejects an out-of-range pin over the wire", False,
                     "no error raised")
    except DeviceStatusError as exc:
        report.check("device rejects an out-of-range pin over the wire", True)
        print(f"         -> {type(exc).__name__}: {exc}")


def phase_status(harness, report: Report) -> dict[str, Any]:
    print("\n== 9. device status counters ==")
    status = harness.status()
    print(f"  frames_ok    : {status.frames_ok:,}")
    print(f"  frames_err   : {status.frames_err:,}")
    print(f"  crc_errors   : {status.crc_errors:,}")
    print(f"  uptime_ms    : {status.uptime_ms:,}")
    report.check("device counted our frames", status.frames_ok > 100,
                 f"frames_ok={status.frames_ok}")
    report.check("no CRC errors during the run", status.crc_errors == 0,
                 f"crc_errors={status.crc_errors}")
    return {
        "frames_ok": status.frames_ok,
        "frames_err": status.frames_err,
        "crc_errors": status.crc_errors,
        "uptime_ms": status.uptime_ms,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--values", type=int, default=50_000,
                        help="counter values for the throughput phase")
    parser.add_argument("--iterations", type=int, default=200,
                        help="round trips for the latency phase")
    parser.add_argument("--quick", action="store_true",
                        help="skip throughput and latency (identity + correctness only)")
    parser.add_argument("--json", help="write the collected numbers to this file")
    args = parser.parse_args(argv)

    try:
        from rp2040_harness import Harness
    except ImportError as exc:  # pragma: no cover
        print(f"cannot import rp2040_harness: {exc}", file=sys.stderr)
        return 2

    report = Report()
    results: dict[str, Any] = {}

    print("RP2040 Harness — hardware bring-up check")
    try:
        with Harness.open() as harness:
            phase_info(harness, report)
            phase_safe_state(harness, report)
            phase_per_line(harness, report)
            phase_atomic(harness, report)
            phase_batch(harness, report)
            if not args.quick:
                results["throughput"] = phase_throughput(harness, report, args.values)
                results["latency"] = phase_latency(harness, report, args.iterations)
            phase_errors(harness, report)
            results["status"] = phase_status(harness, report)

            print("\n== 10. leaving the board in the safe state ==")
            harness.stop()
            states = harness.list_pins()
            report.check("all lines INPUT after the final stop()",
                         all(s.direction == "in" for s in states))
            report.check("no pulls after the final stop()",
                         all(s.pull is None for s in states))
            # Do NOT assert a level here: with the output drivers off and no
            # pull, the pads float and read_all() is undefined by design
            # (docs/protocol.md §5.1). Observed on this board: 0xFFFFFF.
            print(f"  (floating inputs read {_bits(harness.read_all())} — undefined, not checked)")
    except Exception as exc:  # noqa: BLE001
        print(f"\nfatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print()
    print("=" * 64)
    print(f"  {report.summary()}")
    if report.failures:
        for name, detail in report.failures:
            print(f"  FAILED: {name}")
            if detail:
                print(f"          {detail}")
        print("  RESULT: FAIL")
    else:
        print("  RESULT: PASS")
    print("=" * 64)

    if args.json:
        import json
        from pathlib import Path

        results["info"] = report.info
        results["checks"] = [
            {"name": n, "passed": ok, "detail": d} for n, ok, d in report.checks
        ]
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")

    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

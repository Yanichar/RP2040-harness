#!/usr/bin/env python3
"""Parallel 24-bit counter over the harness lines (TASK.md §5.2).

The script drives a 24-bit counter across all 24 lines, incrementing it as fast
as the link allows. **Every value is generated on the PC** through the Python
API; nothing is generated in the firmware.

What it deliberately does NOT do
--------------------------------
TASK.md §5.2 and §10 fix this: there is **no automatic comparison against the
expected sequence**. The script emits the pattern and saves the capture; judging
the capture is `measure.py`'s job, and that is a *measurement*, not a
correctness check. In ``--mode manual`` the read-back mismatch count is reported
for information only and never changes the exit code.

Typical runs
------------
::

    # no hardware: show exactly what would be emitted
    python tests/counter.py --dry-run

    # drive the lines and measure throughput through the Python API
    python tests/counter.py --values 50000

    # drive the lines while the logic analyzer records them
    python tests/counter.py --capture --port COM14 --values 100000

    # exercise the interactive API instead of the batch path
    python tests/counter.py --mode manual --values 500

Artefacts land in ``tests/captures/counter_<timestamp>/``.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

REPO_ROOT = HERE.parent
DEFAULT_OUT_ROOT = HERE / "captures"

PIN_COUNT = 24
ALL_PINS_MASK = (1 << PIN_COUNT) - 1


# --------------------------------------------------------------------------
# Sequence generation (pure — no hardware, fully testable)
# --------------------------------------------------------------------------


def counter_sequence(
    count: int,
    *,
    start: int = 0,
    step: int = 1,
    mask: int = ALL_PINS_MASK,
    bit_order: str = "lsb",
) -> list[int]:
    """Build the sequence of 24-bit words that will be sent to the lines.

    ``bit_order`` is the one genuinely open question from TASK.md §11.4:

    * ``"lsb"`` (default) — logical index 0 carries the least significant bit,
      which is what ``docs/protocol.md`` §3 means by "bit *i* = logical line *i*".
      This is the natural reading and the default everywhere else in the project.
    * ``"msb"`` — logical index 0 carries the most significant bit, for wiring
      where the analyzer's channel 1 ended up on the counter's MSB.

    Only bits inside ``mask`` are meaningful to the device.
    """
    if count < 0:
        raise ValueError("count must not be negative")
    if bit_order not in ("lsb", "msb"):
        raise ValueError(f"bit_order must be 'lsb' or 'msb', got {bit_order!r}")
    if mask < 0 or mask > ALL_PINS_MASK:
        raise ValueError(f"mask must fit in {PIN_COUNT} bits, got {mask:#x}")

    values: list[int] = []
    for i in range(count):
        raw = (start + i * step) & ALL_PINS_MASK
        values.append(reverse_bits(raw) if bit_order == "msb" else raw)
    return values


def reverse_bits(value: int, width: int = PIN_COUNT) -> int:
    """Reverse the low ``width`` bits of ``value``."""
    out = 0
    for i in range(width):
        if value & (1 << i):
            out |= 1 << (width - 1 - i)
    return out


def changed_pins(previous: int, current: int, mask: int = ALL_PINS_MASK) -> Iterator[int]:
    """Yield the logical indices whose level differs between two words."""
    diff = (previous ^ current) & mask
    for index in range(PIN_COUNT):
        if diff & (1 << index):
            yield index


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


@dataclass
class CounterPlan:
    mode: str
    values: list[int]
    mask: int
    bit_order: str
    chunk: int
    capture: bool
    port: str | None
    frequency: int
    pre_trigger_samples: int
    post_trigger_samples: int
    trigger: str
    out_dir: Path

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def first(self) -> int:
        return self.values[0] if self.values else 0

    @property
    def last(self) -> int:
        return self.values[-1] if self.values else 0

    def total_transitions(self) -> int:
        return sum(
            len(list(changed_pins(a, b, self.mask)))
            for a, b in zip(self.values, self.values[1:])
        )

    def describe(self) -> str:
        lines = [
            "RP2040 Harness — parallel counter plan",
            f"  mode            : {self.mode}",
            f"  values          : {self.count}",
            f"  start           : 0x{self.first:06X}",
            f"  last            : 0x{self.last:06X}",
            f"  mask            : 0x{self.mask:06X}",
            f"  bit order       : {self.bit_order} "
            f"(logical index 0 = {'LSB' if self.bit_order == 'lsb' else 'MSB'})",
            f"  line transitions: {self.total_transitions()}",
            f"  stream chunk    : {self.chunk} values per USB transaction",
            f"  output dir      : {self.out_dir}",
        ]
        if self.capture:
            lines += [
                "  analyzer capture: yes",
                f"    port          : {self.port}",
                f"    sample rate   : {self.frequency / 1e6:g} MHz",
                f"    pre / post    : {self.pre_trigger_samples} / {self.post_trigger_samples}",
                f"    trigger       : {self.trigger}",
                "  NOTE: the analyzer does not know the pin map yet. If the trigger",
                "  channel is wrong the capture will time out; run tests/pinmap_scan.py",
                "  first, or pass --trigger edge:<analyzer channel>.",
            ]
        else:
            lines.append("  analyzer capture: no (throughput run only)")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Device driving
# --------------------------------------------------------------------------


def configure_outputs(harness, *, drive: str, slew: str, schmitt: bool) -> None:
    """Configure all 24 lines as outputs, all driving low."""
    harness.configure_many(
        [
            {
                "index": index,
                "direction": "out",
                "pull": None,
                "drive": drive,
                "slew": slew,
                "schmitt": schmitt,
                "open_drain": False,
                "level": 0,
            }
            for index in range(PIN_COUNT)
        ]
    )


def drive_stream(harness, plan: CounterPlan) -> dict[str, Any]:
    """Fast path: chunked STREAM transactions, generated entirely on the PC."""
    started = time.perf_counter()
    chunks = list(harness.stream(plan.values, mask=plan.mask, chunk=plan.chunk))
    wall_s = time.perf_counter() - started

    device_us = sum(c.duration_us for c in chunks)
    applied = sum(c.applied for c in chunks)
    toggles = sum(c.toggles for c in chunks)

    return {
        "mode": "stream",
        "values_requested": plan.count,
        "values_applied": applied,
        "chunks": len(chunks),
        "device_duration_us": device_us,
        "device_updates_per_second": (applied / (device_us * 1e-6)) if device_us else None,
        "wall_duration_s": wall_s,
        "wall_updates_per_second": (applied / wall_s) if wall_s else None,
        "line_transitions": toggles,
        "per_chunk": [
            {
                "applied": c.applied,
                "duration_us": c.duration_us,
                "toggles": c.toggles,
                "updates_per_second": c.updates_per_second,
            }
            for c in chunks[:16]
        ],
    }


def drive_manual(harness, plan: CounterPlan) -> dict[str, Any]:
    """Interactive path: set_pin per changed line, read back every value.

    This is the "manual API" scenario from TASK.md §11.4. It measures one
    round-trip per changed line, which is exactly what the streaming path
    avoids. Mismatches are reported, never asserted (§5.2: no automatic
    comparison).
    """
    previous = plan.first & plan.mask
    # Bring the lines to the starting value first.
    harness.set_pins({i: (previous >> i) & 1 for i in range(PIN_COUNT)})

    started = time.perf_counter()
    set_ops = 0
    read_backs = 0
    mismatches: list[dict[str, Any]] = []
    for value in plan.values:
        for index in changed_pins(previous, value, plan.mask):
            harness.set_pin(index, (value >> index) & 1)
            set_ops += 1
        actual = harness.read_all() & plan.mask
        read_backs += 1
        if actual != (value & plan.mask):
            mismatches.append(
                {
                    "expected": f"0x{value & plan.mask:06X}",
                    "actual": f"0x{actual:06X}",
                }
            )
        previous = value
    wall_s = time.perf_counter() - started

    return {
        "mode": "manual",
        "values_requested": plan.count,
        "values_applied": len(plan.values),
        "set_pin_calls": set_ops,
        "read_all_calls": read_backs,
        "wall_duration_s": wall_s,
        "round_trips_per_second": (set_ops + read_backs) / wall_s if wall_s else None,
        "readback_mismatch_count": len(mismatches),
        "readback_mismatch_samples": mismatches[:8],
        "note": (
            "read-back comparison is informational only — TASK.md §5.2 forbids "
            "treating it as a pass/fail gate"
        ),
    }


# --------------------------------------------------------------------------
# Analyzer capture (optional, runs alongside the drive)
# --------------------------------------------------------------------------


@dataclass
class CaptureHandle:
    thread: threading.Thread | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def join(self, timeout: float) -> None:
        if self.thread is not None:
            self.thread.join(timeout)


def start_capture(plan: CounterPlan, settings_path: Path, lac_path: Path, timeout: float) -> CaptureHandle:
    """Arm the analyzer in a background thread.

    ``analyzer.capture`` blocks until the trigger fires, so it must never run on
    the thread that drives the pins — the trigger IS our own pattern.
    """
    handle = CaptureHandle()

    def target() -> None:
        _run_capture(plan, settings_path, lac_path, timeout, handle)

    handle.thread = threading.Thread(target=target, daemon=True)
    handle.thread.start()
    return handle


def _run_capture(
    plan: CounterPlan,
    settings_path: Path,
    lac_path: Path,
    timeout: float,
    handle: CaptureHandle | None = None,
) -> None:
    import analyzer

    result: dict[str, Any] = {}
    try:
        captured = analyzer.capture(plan.port, settings_path, lac_path, timeout=timeout)
        result = captured.as_dict()
    except Exception as exc:  # noqa: BLE001 - reported, never fatal to the drive
        if handle is not None:
            handle.error = f"{type(exc).__name__}: {exc}"
        return
    if handle is not None:
        handle.result = result


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def build_plan(args: argparse.Namespace) -> CounterPlan:
    values = counter_sequence(
        args.values,
        start=args.start,
        step=args.step,
        mask=args.mask,
        bit_order=args.bit_order,
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / f"counter_{stamp}" if args.out else DEFAULT_OUT_ROOT / f"counter_{stamp}"
    return CounterPlan(
        mode=args.mode,
        values=values,
        mask=args.mask,
        bit_order=args.bit_order,
        chunk=args.chunk,
        capture=args.capture,
        port=args.port,
        frequency=args.frequency,
        pre_trigger_samples=args.pre,
        post_trigger_samples=args.post,
        trigger=args.trigger,
        out_dir=out_dir,
    )


def run(args: argparse.Namespace) -> int:
    plan = build_plan(args)

    print(plan.describe())
    print()

    if args.dry_run:
        print("--dry-run: no device and no analyzer were touched.")
        print(f"first 8 values: {[f'0x{v:06X}' for v in plan.values[:8]]}")
        print(f"last  8 values: {[f'0x{v:06X}' for v in plan.values[-8:]]}")
        print(f"would write artefacts under: {plan.out_dir}")
        return 0

    try:
        from rp2040_harness import Harness
    except ImportError as exc:
        print(
            f"cannot import the host package: {exc}\n"
            "Install it with: pip install -e host",
            file=sys.stderr,
        )
        return 2

    plan.out_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "plan": {
            "mode": plan.mode,
            "count": plan.count,
            "start": f"0x{plan.first:06X}",
            "last": f"0x{plan.last:06X}",
            "mask": f"0x{plan.mask:06X}",
            "bit_order": plan.bit_order,
            "chunk": plan.chunk,
        },
        "analyzer": None,
        "drive": None,
    }

    handle: CaptureHandle | None = None
    lac_path = plan.out_dir / "counter.lac"
    settings_path = plan.out_dir / "counter.tcs"

    if plan.capture:
        if not plan.port:
            print("--capture requires --port <analyzer COM port>", file=sys.stderr)
            return 2
        import analyzer

        analyzer.build_settings(
            settings_path,
            channels="1-24",
            frequency=plan.frequency,
            pre_trigger_samples=plan.pre_trigger_samples,
            post_trigger_samples=plan.post_trigger_samples,
            trigger=plan.trigger,
            names={i: f"L{i - 1} (logical {i - 1})" for i in range(1, 25)},
        )
        print(f"armed capture: {settings_path}")
        handle = start_capture(plan, settings_path, lac_path, args.capture_timeout)
        # Let TerminalCapture open the port and get into its wait loop.
        time.sleep(args.arm_delay)

    try:
        with Harness.open() as harness:
            info = harness.get_info()
            print(f"device: fw {info.firmware_version}, serial {info.serial}")
            configure_outputs(harness, drive=args.drive, slew=args.slew, schmitt=not args.no_schmitt)

            if args.mode == "stream":
                drive = drive_stream(harness, plan)
            else:
                drive = drive_manual(harness, plan)

            # Return to the documented safe state before releasing the device.
            harness.all_inputs()
    except Exception as exc:  # noqa: BLE001
        print(f"drive failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if handle is not None:
            handle.join(args.capture_timeout + 5)
        return 3

    summary["drive"] = drive

    print()
    print("drive summary")
    for key in (
        "values_applied",
        "device_updates_per_second",
        "wall_updates_per_second",
        "line_transitions",
        "wall_duration_s",
        "set_pin_calls",
        "read_all_calls",
        "round_trips_per_second",
        "readback_mismatch_count",
    ):
        if key in drive and drive[key] is not None:
            value = drive[key]
            value = f"{value:,.1f}" if isinstance(value, float) else value
            print(f"  {key:28s}: {value}")

    if handle is not None:
        print()
        print("waiting for the analyzer capture to finish…")
        handle.join(args.capture_timeout + 5)
        if handle.error:
            print(f"capture failed: {handle.error}", file=sys.stderr)
            summary["analyzer"] = {"ok": False, "error": handle.error}
        else:
            summary["analyzer"] = handle.result
            print(f"capture: {json.dumps(handle.result)[:400]}")
            if lac_path.is_file():
                import la_lib
                import measure

                vcd_path = plan.out_dir / "counter.vcd"
                capture = la_lib.read_lac(lac_path, require_samples=True)
                la_lib.write_vcd(vcd_path, capture)
                print(f"wrote {vcd_path}")

                measurement = measure.measure(capture, label="counter")
                report_path = plan.out_dir / "counter.measure.md"
                report_path.write_text(
                    measure.format_markdown(measurement, title="Parallel counter"),
                    encoding="utf-8",
                )
                (plan.out_dir / "counter.measure.json").write_text(
                    measurement.to_json(), encoding="utf-8"
                )
                print()
                print(measure.format_report(measurement))
                print(f"wrote {report_path}")

    (plan.out_dir / "counter.summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print()
    print(f"artefacts in {plan.out_dir}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive a parallel 24-bit counter on the harness lines (TASK.md §5.2).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--mode", choices=("stream", "manual"), default="stream",
                        help="'stream' uses chunked batch writes; 'manual' uses set_pin/read_all per line")
    parser.add_argument("--values", type=int, default=20000, help="number of counter values to emit")
    parser.add_argument("--start", type=lambda s: int(s, 0), default=0, help="first counter value")
    parser.add_argument("--step", type=lambda s: int(s, 0), default=1, help="counter increment")
    parser.add_argument("--mask", type=lambda s: int(s, 0), default=ALL_PINS_MASK,
                        help="which logical lines carry the counter (24-bit mask)")
    parser.add_argument("--bit-order", choices=("lsb", "msb"), default="lsb",
                        help="which logical index carries the least significant bit (TASK.md §11.4)")
    parser.add_argument("--chunk", type=int, default=250,
                        help="values per USB transaction in stream mode (protocol max 250)")
    parser.add_argument("--drive", default="4mA", help="pad drive strength for all lines")
    parser.add_argument("--slew", default="slow", help="pad slew rate for all lines")
    parser.add_argument("--no-schmitt", action="store_true", help="disable the Schmitt trigger")

    parser.add_argument("--capture", action="store_true", help="also record the lines with the logic analyzer")
    parser.add_argument("--port", default=None, help="analyzer serial port, e.g. COM14 (required with --capture)")
    parser.add_argument("--frequency", type=int, default=20_000_000, help="analyzer sample rate in Hz")
    parser.add_argument("--pre", type=int, default=512, help="analyzer pre-trigger samples")
    parser.add_argument("--post", type=int, default=4000, help="analyzer post-trigger samples")
    parser.add_argument("--trigger", default="edge:1", help="analyzer trigger, e.g. edge:3")
    parser.add_argument("--capture-timeout", type=float, default=30.0, help="seconds to wait for the trigger")
    parser.add_argument("--arm-delay", type=float, default=0.5, help="seconds between arming and driving")

    parser.add_argument("--out", default=None, help="parent directory for artefacts")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; touch nothing")

    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

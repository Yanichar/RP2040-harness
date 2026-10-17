"""pinmap_scan — determine the GP <-> analyzer-channel map empirically.

TASK.md §2: the analyzer's PA0-7/PB0-7/PD0-7 header ports do not carry channel
numbers, so the wiring is only knowable by observation. This tool drives a
"stepping" pattern -- **one line high at a time**, through all 24 logical pin
indices -- while a capture is running, then matches each logical index to the
analyzer channel that moved.

Mapping facts used here (TASK.md §2, docs/protocol.md §3):

* the harness exposes 24 *logical* indices ``0..23``;
* index ``i`` is GPIO ``2+i`` for ``i < 21``, then GP26, GP27, GP28 for
  ``i = 21, 22, 23``;
* bit ``i`` of a mask addresses logical index ``i``.

Usage::

    python pinmap_scan.py --port COMx --probe          # ask the device first
    python pinmap_scan.py --port COMx                  # real scan
    python pinmap_scan.py --dry-run                    # print the intended table

``--dry-run`` never touches hardware and never writes ``docs/pinmap.md``; it
prints the protocol-level table with every channel still unconfirmed.
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from la_lib import Capture, FormatError  # noqa: E402

# ----------------------------------------------------------------- constants

PIN_COUNT = 24
#: TASK.md §2 / docs/protocol.md §3: logical index -> GPIO number.
LOGICAL_TO_GPIO: tuple[int, ...] = tuple(list(range(2, 23)) + [26, 27, 28])
assert len(LOGICAL_TO_GPIO) == PIN_COUNT

#: Where the empirical result belongs (TASK.md §8 item 7).
DEFAULT_PINMAP_PATH = Path(__file__).resolve().parent.parent / "docs" / "pinmap.md"

# Default capture geometry. 24-channel mode caps the buffer at 32768 samples
# (max pre 3276, max post 32766), so this is well inside the driver's limits.
DEFAULT_FREQUENCY = 100_000
DEFAULT_PRE = 256
DEFAULT_POST = 10_000
DEFAULT_STEP_S = 0.002


class PinmapError(RuntimeError):
    """The scan could not be set up or produced no usable capture."""


# --------------------------------------------------------------- host adapter


@dataclass
class HarnessAdapter:
    """Thin, defensive adapter over the ``rp2040_harness`` API.

    The package does not exist yet (``host/`` is unwritten), and its exact method
    signatures are still being agreed in TASK.md §11, so nothing here is imported
    at module import time and every call resolves by capability name with
    alternatives. If a method is absent, the error names what was looked for and
    where, instead of raising a bare ``AttributeError``.
    """

    handle: Any
    package: str = "rp2040_harness"

    @classmethod
    def open(cls, package: str = "rp2040_harness", **kwargs: Any) -> "HarnessAdapter":
        """``from rp2040_harness import Harness; Harness.open()`` — imported lazily."""
        try:
            module = importlib.import_module(package)
        except ImportError as exc:
            raise PinmapError(
                f"cannot import {package!r} ({exc}). The harness Python package is not "
                "installed/implemented yet; pinmap_scan needs it to drive the pins. "
                "Use --dry-run to see the intended table without hardware."
            ) from exc
        harness_cls = getattr(module, "Harness", None)
        if harness_cls is None:
            raise PinmapError(f"{package}.Harness not found in module {package!r}")
        opener = getattr(harness_cls, "open", None)
        if not callable(opener):
            raise PinmapError(f"{package}.Harness has no open() classmethod")
        try:
            handle = opener(**kwargs) if kwargs else opener()
        except TypeError:
            handle = opener()
        return cls(handle=handle, package=package)

    # -- capability lookup -------------------------------------------------

    def _method(self, *names: str) -> Callable[..., Any]:
        for name in names:
            candidate = getattr(self.handle, name, None)
            if callable(candidate):
                return candidate
        raise PinmapError(
            f"none of {names} is available on {type(self.handle).__name__} "
            f"({self.package}); the harness API differs from TASK.md §4.1"
        )

    # -- operations --------------------------------------------------------

    def configure_output(self, index: int, **kwargs: Any) -> None:
        """Configure one logical index as an output, no pulls, per the spec shape."""
        configure = self._method("configure", "configure_pin")
        parameters = {
            "direction": "out",
            "drive": "4mA",
            "slew": "slow",
            "schmitt": True,
            "pull": None,
            "open_drain": False,
        }
        parameters.update(kwargs)
        try:
            configure(index, **parameters)
        except TypeError:
            # Older/looser signature: at least get the direction right.
            configure(index, "out")

    def write_mask(self, mask: int, value: int) -> None:
        """Atomic 24-bit update: ``mask`` selects the bits, ``value`` their new levels."""
        write = self._method("write_mask", "set_mask", "set_pins_atomic")
        write(mask=mask, value=value)

    def read_all(self) -> int:
        read = self._method("read_all", "read_pins")
        return int(read())

    def close(self) -> None:
        for name in ("close", "__exit__"):
            candidate = getattr(self.handle, name, None)
            if callable(candidate):
                try:
                    if name == "__exit__":
                        candidate(None, None, None)
                    else:
                        candidate()
                except Exception:  # noqa: BLE001 - best effort on teardown
                    pass
                return

    def __enter__(self) -> "HarnessAdapter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ------------------------------------------------------------ stepping pattern


def build_stepping_plan(
    *, pre_steps: int = 8, tail_steps: int = 8
) -> list[tuple[int, int]]:
    """The mask/value sequence that makes each logical line move on its own.

    Returns ``(mask, value)`` pairs to hand to :meth:`HarnessAdapter.write_mask`.
    Layout, all on the full 24-bit mask:

    1. ``pre_steps`` of all-low, to establish a known starting level (a capture
       that starts mid-transition otherwise has no baseline to compare against);
    2. one step with index 0 high — **this rising edge on index 0 is the capture
       trigger** (``--trigger edge:1``), so the capture is guaranteed to fire and
       the pre-trigger region shows the all-low baseline;
    3. one step per logical index, each with exactly one line high;
    4. ``tail_steps`` of all-low after the last index, so the final line's falling
       edge is inside the capture window.

    One line at a time means each step produces exactly one rising edge, on a
    different analyzer channel — that is what makes the mapping unambiguous.
    """
    full_mask = (1 << PIN_COUNT) - 1
    plan: list[tuple[int, int]] = [(full_mask, 0) for _ in range(max(pre_steps, 0))]
    plan.append((full_mask, 1 << 0))
    for index in range(PIN_COUNT):
        plan.append((full_mask, 1 << index))
    plan += [(full_mask, 0) for _ in range(max(tail_steps, 0))]
    return plan


@dataclass
class ScanPlan:
    """Resolved parameters for one scan, printable before it runs."""

    port: str
    frequency: int
    pre: int
    post: int
    step_delay_s: float
    timeout_s: float
    trigger: str
    steps: list[tuple[int, int]]

    @property
    def trigger_index(self) -> int:
        """0-based logical index whose rising edge is the trigger."""
        return 0

    @property
    def first_step_sample(self) -> int:
        """Sample index at which index 0's trigger pulse ends and stepping begins.

        The trigger fires at sample ``pre``; step *k* of the stepping loop is
        driven ``k * step_delay_s`` after it.
        """
        return self.pre

    def expected_rising_sample(self, logical_index: int) -> float:
        """Where logical index ``logical_index`` is expected to go high.

        Step layout is: pre-steps, trigger pulse (index 0), then one step per
        index starting at 0. So the *last* occurrence of index ``i`` is the
        trigger pulse for ``i == 0`` and the stepping step otherwise — but both
        are index 0 going high, so index 0 is measured from the trigger itself.
        """
        trigger_sample = self.pre
        # advance past the pre-steps => trigger sample. Then each index's step is
        # i * step_delay after the trigger pulse step.
        return trigger_sample + logical_index * self.step_delay_s * self.frequency


# ------------------------------------------------------------------- matching


@dataclass
class MatchResult:
    logical_index: int
    gpio: int
    analyzer_channel: int | None
    expected_sample: float
    observed_sample: float | None
    delta_samples: float | None
    confidence: str
    note: str = ""

    @property
    def confirmed(self) -> bool:
        return self.analyzer_channel is not None and self.confidence in ("high", "medium")


def _rising_edges(samples: bytes) -> list[int]:
    edges = []
    for index in range(1, len(samples)):
        if samples[index] and not samples[index - 1]:
            edges.append(index)
    return edges


def match_indices(capture: Capture, plan: ScanPlan, *, tolerance_steps: float = 0.5) -> list[MatchResult]:
    """Match each logical index to the analyzer channel whose rising edge fits.

    Matching is by **expected position**, not by a fixed channel order: for each
    logical index we predict the sample at which it should rise (the trigger at
    ``PreTriggerSamples``, then one step per index) and look for the analyzer
    channel with a rising edge inside ``tolerance_steps * step_delay`` of it.

    A channel claimed by two logical indices means the wiring or the step timing
    is wrong, so the later claim is downgraded to ``ambiguous`` rather than
    silently reported as a map. This is why a real capture is required to write
    ``docs/pinmap.md`` — with no sample data there is nothing to match against.
    """
    if not capture.has_data:
        raise FormatError("match_indices: capture has no sample data to match")

    window = max(1.0, tolerance_steps * plan.step_delay_s * plan.frequency)
    channels = capture.data_channels()
    rising = {c.number: _rising_edges(c.samples) for c in channels}

    results: list[MatchResult] = []
    claimed: dict[int, int] = {}

    for index in range(PIN_COUNT):
        expected = plan.expected_rising_sample(index)
        best: tuple[float, int, float] | None = None
        for number, edges in rising.items():
            for edge in edges:
                delta = edge - expected
                if abs(delta) <= window and (best is None or abs(delta) < abs(best[0])):
                    best = (delta, number, float(edge))

        if best is None:
            results.append(
                MatchResult(
                    logical_index=index,
                    gpio=LOGICAL_TO_GPIO[index],
                    analyzer_channel=None,
                    expected_sample=expected,
                    observed_sample=None,
                    delta_samples=None,
                    confidence="none",
                    note="no analyzer channel has a rising edge near the expected sample",
                )
            )
            continue

        delta, number, observed = best
        # Reserve the channel once *any* index binds to it, before classifying.
        # If only high-confidence bindings were remembered, an index that bound
        # marginally (medium) would not reserve the channel, and the next index
        # could bind the same channel and also report "high" — two logical indices
        # presented as two confident, different facts from one wire.
        owner = claimed.get(number)
        claimed.setdefault(number, index)

        confidence = "high"
        note = ""
        if owner is not None:
            confidence = "ambiguous"
            note = f"channel also matched logical index {owner}"
        elif abs(delta) > window / 2:
            confidence = "medium"
            note = "edge is near the tolerance limit"

        results.append(
            MatchResult(
                logical_index=index,
                gpio=LOGICAL_TO_GPIO[index],
                analyzer_channel=number,
                expected_sample=expected,
                observed_sample=observed,
                delta_samples=delta,
                confidence=confidence,
                note=note,
            )
        )
    return results


def summarize(results: Sequence[MatchResult]) -> dict[str, int]:
    counts = {"high": 0, "medium": 0, "ambiguous": 0, "none": 0}
    for result in results:
        counts[result.confidence] = counts.get(result.confidence, 0) + 1
    return counts


# ------------------------------------------------------------------ rendering


def render_table(results: Sequence[MatchResult]) -> str:
    """The GP <-> analyzer-channel table as markdown, with a confirmation column."""
    lines = [
        "| Logical index | GPIO | Analyzer channel | Confirmed empirically |",
        "|---|---|---|---|",
    ]
    for result in results:
        channel = (
            str(result.analyzer_channel + 1)
            if result.analyzer_channel is not None
            else "—"
        )
        if result.confidence in ("high", "medium"):
            confirmed = "yes"
        elif result.confidence == "ambiguous":
            confirmed = f"ambiguous ({result.note})"
        else:
            confirmed = "no"
        lines.append(f"| {result.logical_index} | GP{result.gpio} | {channel} | {confirmed} |")
    return "\n".join(lines)


PINMAP_HEADER = """# Pin map — GPIO <-> logic-analyzer channel

**Status column semantics**

* `yes` — the channel moved exactly when this logical index was driven high during
  a real capture, within half a step interval. This is empirical evidence.
* `ambiguous` — two logical indices matched the same analyzer channel; the wiring
  or the step timing needs checking before this row can be trusted.
* `no` — no capture has confirmed this row. The values may come from the
  protocol document (index -> GPIO) or the analyzer's own channel numbering, but
  **the wiring has not been observed**.

Generated by `tests/pinmap_scan.py`. This file is only ever rewritten after a
real capture; `--dry-run` prints its table instead of writing.

"""


def render_document(
    results: Sequence[MatchResult],
    *,
    plan: ScanPlan | None,
    dry_run: bool,
    extra_notes: Sequence[str] = (),
) -> str:
    parts = [PINMAP_HEADER, render_table(results), ""]
    if dry_run or plan is None:
        parts += [
            "> **NOT EMPIRICALLY CONFIRMED.** This is the intended table produced by "
            "`pinmap_scan.py --dry-run`. No capture was run, so no row is evidence.",
            "",
            "Intended measurement settings (to be confirmed on hardware):",
            "",
            "| Setting | Value |",
            "|---|---|",
        ]
        if plan is not None:
            parts += [
                f"| Analyzer port | {plan.port} |",
                f"| Sample rate | {plan.frequency:,} Hz |",
                f"| Pre-trigger | {plan.pre} samples |",
                f"| Post-trigger | {plan.post} samples |",
                f"| Step delay | {plan.step_delay_s * 1000:.1f} ms |",
                f"| Trigger | `{plan.trigger}` (rising edge on logical index 0) |",
            ]
        else:
            parts += ["| (no plan) | — |"]
        parts.append("")
    else:
        parts += [
            "Measurement settings (from the capture that produced this table):",
            "",
            "| Setting | Value |",
            "|---|---|",
            f"| Analyzer port | {plan.port} |",
            f"| Sample rate | {plan.frequency:,} Hz |",
            f"| Pre-trigger | {plan.pre} samples |",
            f"| Post-trigger | {plan.post} samples |",
            f"| Step delay | {plan.step_delay_s * 1000:.1f} ms |",
            f"| Trigger | `{plan.trigger}` |",
            "",
            "Each row above was produced by one capture of the stepping pattern "
            "(`build_stepping_plan()`): one logical line high at a time, all 24 "
            "indices, matched by expected sample position.",
            "",
        ]
    if extra_notes:
        parts += ["Notes:", ""]
        parts += [f"- {note}" for note in extra_notes]
        parts.append("")
    return "\n".join(parts)


# ------------------------------------------------------------------------ CLI


def _parse_trigger(spec: str) -> tuple[int, bool]:
    match = re.fullmatch(r"edge:(\d+)(:neg)?", spec.strip())
    if not match:
        raise SystemExit(f"bad --trigger {spec!r}; this tool only drives edge:<channel>[:neg]")
    return int(match.group(1)), bool(match.group(2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pinmap_scan.py",
        description=(
            "Determine the GPIO <-> logic-analyzer channel mapping empirically by "
            "driving a stepping pattern while capturing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python pinmap_scan.py --dry-run\n"
            "  python pinmap_scan.py --probe --port COMx\n"
            "  python pinmap_scan.py --port COMx --write\n"
        ),
    )
    parser.add_argument("--port", help="analyzer COM port")
    parser.add_argument("--install", help="all-in-one install directory (default: known path)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the intended table; touch no hardware and write no file",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="run probe-device.ps1 first and take the real buffer/frequency limits from the device",
    )
    parser.add_argument("--frequency", type=int, default=DEFAULT_FREQUENCY, help="sample rate Hz")
    parser.add_argument("--pre", type=int, default=DEFAULT_PRE, help="pre-trigger samples")
    parser.add_argument("--post", type=int, default=DEFAULT_POST, help="post-trigger samples")
    parser.add_argument(
        "--step-delay",
        type=float,
        default=DEFAULT_STEP_S,
        help="seconds each logical line is held high",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="hard capture timeout in seconds (default: derived from the step count)",
    )
    parser.add_argument(
        "--trigger",
        default="edge:1",
        help="capture trigger; index 0 is pulsed as the trigger (default edge:1)",
    )
    parser.add_argument("--output", help="where to write the capture (.lac); default tests/captures/")
    parser.add_argument(
        "--write",
        action="store_true",
        help="write docs/pinmap.md (only honoured after a real capture)",
    )
    parser.add_argument("--pinmap", default=str(DEFAULT_PINMAP_PATH), help="pinmap.md path")
    args = parser.parse_args(argv)

    try:
        trigger_channel, trigger_negated = _parse_trigger(args.trigger)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    steps = build_stepping_plan()
    default_timeout = len(steps) * args.step_delay + 10.0

    plan = ScanPlan(
        port=args.port or "<port>",
        frequency=args.frequency,
        pre=args.pre,
        post=args.post,
        step_delay_s=args.step_delay,
        timeout_s=args.timeout or default_timeout,
        trigger=args.trigger,
        steps=steps,
    )

    if trigger_channel != 1 or trigger_negated:
        print(
            f"warning: the stepping plan pulses logical index 0 as the trigger, so "
            f"--trigger {args.trigger} will not fire on it as written. "
            "Use edge:1 (rising) or supply your own trigger source.",
            file=sys.stderr,
        )

    if args.probe:
        if not args.port:
            print("error: --probe needs --port", file=sys.stderr)
            return 2
        import analyzer as analyzer_mod

        try:
            info = analyzer_mod.probe(args.port)
        except analyzer_mod.AnalyzerError as exc:
            print(f"probe failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"device: {info.get('version')} on {info.get('port')} — "
            f"{info.get('channelCount')} channels, buffer {info.get('bufferSize')}, "
            f"max {info.get('maxFrequency')} Hz"
        )
        if info.get("recovered"):
            print("(device was armed for a trigger; abort/reopen recovered it)")
        for limit in info.get("limits") or []:
            print(
                f"  {limit.get('Mode')}: total<={limit.get('MaxTotalSamples')}, "
                f"pre<={limit.get('MaxPreSamples')}, post<={limit.get('MaxPostSamples')}"
            )
        if args.frequency > int(info.get("maxFrequency") or 0):
            print(
                f"error: --frequency {args.frequency} exceeds the device maximum "
                f"{info.get('maxFrequency')}",
                file=sys.stderr,
            )
            return 2

    # ---------------------------------------------------------------- dry run
    if args.dry_run:
        results = [
            MatchResult(
                logical_index=index,
                gpio=LOGICAL_TO_GPIO[index],
                analyzer_channel=None,
                expected_sample=plan.expected_rising_sample(index),
                observed_sample=None,
                delta_samples=None,
                confidence="none",
                note="dry run — no capture performed",
            )
            for index in range(PIN_COUNT)
        ]
        print(render_document(results, plan=plan, dry_run=True).replace("\n\n\n", "\n\n"))
        print(
            "\n(--dry-run: no capture was started and docs/pinmap.md was not written)",
            file=sys.stderr,
        )
        return 0

    # ------------------------------------------------------------ real capture
    if not args.port:
        print("error: --port is required for a real scan (or use --dry-run)", file=sys.stderr)
        return 2

    import analyzer as analyzer_mod
    from la_lib import SettingsError, device_limits

    if args.output:
        output = Path(args.output)
    else:
        output = Path(__file__).resolve().parent / "captures" / "pinmap_scan.lac"
    output.parent.mkdir(parents=True, exist_ok=True)
    settings_path = output.with_suffix(".tcs")

    # All 24 channels, because any of them may be the one that moved.
    try:
        built = analyzer_mod.build_settings(
            settings_path,
            channels=tuple(range(1, PIN_COUNT + 1)),
            frequency=args.frequency,
            pre_trigger_samples=args.pre,
            post_trigger_samples=args.post,
            trigger=args.trigger,
            names={n: f"idx{n - 1}=GP{LOGICAL_TO_GPIO[n - 1]}" for n in range(1, PIN_COUNT + 1)},
        )
    except SettingsError as exc:
        print("settings rejected by the driver's own limits:", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        limits = device_limits(
            range(PIN_COUNT), max_frequency=DEFAULT_FREQUENCY, buffer_size=131_072
        )
        print(
            f"\n24-channel mode allows pre<= {limits.max_pre}, post<= {limits.max_post}, "
            f"total<= {limits.total}",
            file=sys.stderr,
        )
        return 2
    print(f"settings: {built['session']['TotalSamples']} samples at {args.frequency} Hz")
    print(f"trigger:  {built['triggerText']}")

    adapter: HarnessAdapter | None = None
    result = None
    try:
        adapter = HarnessAdapter.open()
        print(f"harness:  {type(adapter.handle).__name__} opened")
        for index in range(PIN_COUNT):
            adapter.configure_output(index)
        print(f"configured {PIN_COUNT} pins as outputs (drive 4mA, slew slow, schmitt on)")

        # Start the capture FIRST: it blocks until the trigger, and the trigger is
        # the pulse below. Starting the pins first would fire the trigger into a
        # capture that has not been armed yet.
        print(f"starting capture on {args.port} (timeout {plan.timeout_s:.1f}s)...")
        started = time.monotonic()

        def driver() -> None:
            # Give TerminalCapture time to arm and open the port before the pulse.
            time.sleep(1.0)
            for mask, value in plan.steps:
                adapter.write_mask(mask=mask, value=value)
                time.sleep(plan.step_delay_s)

        import threading

        thread = threading.Thread(target=driver, name="pinmap-driver", daemon=True)
        thread.start()
        result = analyzer_mod.capture(
            args.port,
            settings_path,
            output,
            timeout=plan.timeout_s,
            install=args.install,
        )
        thread.join(timeout=2.0)
        print(f"driving took {time.monotonic() - started:.1f}s")
    except Exception as exc:  # noqa: BLE001 - report, then restore pins
        print(f"error: {exc}", file=sys.stderr)
        if adapter is not None:
            try:
                adapter.write_mask(mask=(1 << PIN_COUNT) - 1, value=0)
                print("all lines driven low before exit", file=sys.stderr)
            except Exception as restore_exc:  # noqa: BLE001
                print(f"could not drive lines low: {restore_exc}", file=sys.stderr)
        return 1
    finally:
        if adapter is not None:
            adapter.close()

    if result is None or not result.ok:
        detail = result.detail if result else "no result"
        print(f"capture failed: {detail}", file=sys.stderr)
        if result and result.recovery:
            print(f"recovery: {result.recovery}", file=sys.stderr)
        return 1

    capture = result.capture
    assert capture is not None
    print(f"captured {capture.sample_count} samples -> {result.output}")
    for warning in capture.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    matches = match_indices(capture, plan)
    counts = summarize(matches)
    print()
    print(render_table(matches))
    print()
    print(
        f"matched: {counts.get('high', 0)} high confidence, "
        f"{counts.get('medium', 0)} medium, {counts.get('ambiguous', 0)} ambiguous, "
        f"{counts.get('none', 0)} unmatched"
    )

    notes = [
        f"Capture: `{result.output.name}` — {capture.sample_count} samples at "
        f"{capture.frequency} Hz, trigger {capture.trigger_text}.",
        f"Matching: rising edge within {0.5 * plan.step_delay_s * 1000:.1f} ms of the "
        "expected step position.",
    ]
    if counts.get("ambiguous") or counts.get("none"):
        notes.append(
            "Unmatched or ambiguous rows mean the wiring or the step timing needs "
            "checking; do not treat them as a map."
        )

    if args.write:
        target = Path(args.pinmap)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            render_document(matches, plan=plan, dry_run=False, extra_notes=notes),
            encoding="utf-8",
        )
        print(f"wrote {target}")
    else:
        print("(pass --write to update docs/pinmap.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

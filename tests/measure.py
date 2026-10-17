"""measure — turn a capture into throughput numbers (TASK.md §3.2, §5.2).

    ####################################################################
    #  THIS IS A MEASUREMENT, NOT A CORRECTNESS CHECK.                  #
    #                                                                  #
    #  TASK.md §5.2 is explicit: there is no automatic comparison       #
    #  against an expected sequence. This tool counts how often the     #
    #  observed state actually changed and reports the timing of those  #
    #  changes. It does not know what the pattern "should" have been,   #
    #  so a stream that skipped every other value still measures as a   #
    #  clean fast signal. Correctness evidence must come from elsewhere #
    #  (the counter script's own log, a known-divisor check, ...).      #
    ####################################################################

Inputs: ``.lac`` (the real thing), ``.vcd`` (what :func:`la_lib.write_vcd` wrote),
or ``.csv`` (what ``TerminalCapture`` wrote — samples only, so the sample rate
must be supplied with ``--frequency``).

Reported per TASK §3.2 ("фактическая частота обновлений, латентность одиночной
команды, джиттер") and §5.2:

* distinct states observed on the driven lines;
* update frequency (Hz) = state changes / elapsed time between first and last change;
* mean / median / min / max interval between changes;
* standard deviation and peak-to-peak jitter of those intervals.

"State" is the bit pattern of the selected channels in file order (bit 0 = first
data channel). A *change* is any sample where that pattern differs from the
previous sample — so every increment of a parallel counter counts once, and a
glitch that reverts within one sample counts twice.
"""

from __future__ import annotations

import argparse
import csv as csv_module
import json
import re
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from la_lib import Capture, FormatError, read_lac  # noqa: E402

__all__ = [
    "NOT_A_CORRECTNESS_CHECK",
    "Measurement",
    "load_capture",
    "read_vcd",
    "read_csv",
    "measure",
    "format_report",
    "format_markdown",
]

#: Printed verbatim in every report, because §5.2 says the distinction matters.
NOT_A_CORRECTNESS_CHECK = (
    "THIS IS A MEASUREMENT, NOT A CORRECTNESS CHECK — TASK.md §5.2 defines no "
    "automatic comparison against an expected sequence, so a match or mismatch "
    "with the intended pattern is not evaluated here."
)


@dataclass
class Measurement:
    """One measured run."""

    source: str
    frequency: int
    sample_count: int
    channel_numbers: list[int]
    channels: list[str]
    elapsed_us: float
    changes: int
    distinct_states: int
    update_hz: float
    span_rate_hz: float
    period_us: float
    region: str
    intervals_us: list[float] = field(default_factory=list)
    stats: dict[str, float | None] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["disclaimer"] = NOT_A_CORRECTNESS_CHECK
        return data

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), indent=indent)


# ------------------------------------------------------------------- loaders


def read_vcd(path: str | Path, *, sample_period_ps: float | None = None) -> Capture:
    """Read a VCD written by :func:`la_lib.write_vcd`.

    **The convention that makes this recoverable.** ``write_vcd`` declares
    ``$timescale <round(1e12 / Frequency)> ps $end`` — exactly one sample period —
    and stamps a change at sample *i* as ``#i``. One timescale unit is therefore
    one sample, a timestamp is directly a sample index, and the sample rate is the
    timescale. This is the only self-consistent choice: the writer emits
    timestamps only for *changed* samples, so the smallest gap between timestamps
    is a multiple of the sample period, not the period itself. Reading a generic
    VCD's sample rate back out is not possible; ``sample_period_ps`` overrides the
    assumption for a foreign file.

    Handles one-bit ``$var wire 1`` declarations. Not a general VCD parser:
    vectors and ``$dumpoff``/``$dumpon`` x/z states are not interpreted (x/z read
    as 0).

    Tokenising is directive-aware: everything between a ``$``-keyword and its
    ``$end`` is skipped, *except* the dump commands, whose contents are value
    changes. That distinction matters — ``$date 2026-10-17T00:32:01.988178``
    contains a digit run that a naive tokeniser would read as a timestamp.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")

    scale_to_ps = {"s": 1e12, "ms": 1e9, "us": 1e6, "ns": 1e3, "ps": 1.0}
    timescale_ps = 1.0
    # Longest unit first: `[munpf]?s` alone would not match a bare `s`.
    match = re.search(r"\$timescale\s+(\d+)\s*(ms|us|ns|ps|s)\s*\$end", text)
    if match:
        timescale_ps = float(match.group(1)) * scale_to_ps[match.group(2)]

    period_ps = float(sample_period_ps) if sample_period_ps else timescale_ps
    if period_ps <= 0:
        raise FormatError(f"{path}: cannot determine the sample period")
    # Ratio from file timestamps to sample indices.
    units_per_sample = period_ps / timescale_ps

    id_to_channel: dict[str, int] = {}
    names: dict[int, str] = {}
    for var in re.finditer(r"\$var\s+wire\s+(\d+)\s+(\S+)\s+([^\s$]+)\s+\$end", text):
        width, ident, name = var.group(1), var.group(2), var.group(3)
        if width != "1":
            raise FormatError(f"{path}: only 1-bit $var supported (got {width} bits for {name})")
        number = len(id_to_channel)
        id_to_channel[ident] = number
        names[number] = name
    if not id_to_channel:
        raise FormatError(f"{path}: no 1-bit $var declarations found")

    levels: dict[int, list[tuple[int, int]]] = {n: [] for n in id_to_channel.values()}
    time = 0
    in_directive = False
    scalar_re = re.compile(r"^([01xzXZ])(\S+)$")
    for token in text.split():
        if in_directive:
            if token == "$end":
                in_directive = False
            continue
        if token.startswith("$"):
            # Most directives swallow tokens until `$end`. The dump commands are
            # the exception: their *contents are value changes*, which is why the
            # initial `$dumpvars` block must be parsed rather than skipped.
            in_directive = token not in ("$end", "$dumpvars", "$dumpall", "$dumpon", "$dumpoff")
            continue
        if token.startswith("#"):
            digits = token[1:]
            if digits.isdigit():
                # Subtract the "zero" time so a file starting at #N still maps its
                # first sample to index 0.
                time = int(digits)
            continue
        scalar = scalar_re.match(token)
        if not scalar:
            continue
        ident = scalar.group(2)
        number = id_to_channel.get(ident)
        if number is not None:
            # Store as a SAMPLE INDEX immediately; every later step is in indices.
            levels[number].append((time / units_per_sample if units_per_sample else time,
                                   0 if scalar.group(1) in "0xXzZ" else 1))

    all_indices = sorted({t for series in levels.values() for t, _ in series})
    if not all_indices:
        raise FormatError(f"{path}: no value changes found")

    # Timestamps may start anywhere; rebase so the first sample is index 0.
    origin = all_indices[0]
    count = int(round(all_indices[-1] - origin)) + 1

    # VCD cannot represent trailing samples that never change, so our writer
    # records the true length in a comment. Honour it when present: it makes the
    # export/import round trip lossless instead of silently trimming the tail.
    declared = re.search(r"\$comment\s+la_total_samples\s+(\d+)\s+\$end", text)
    if declared:
        count = max(count, int(declared.group(1)))

    from la_lib import Channel  # local import keeps la_lib's public surface tidy

    channels = []
    for number in sorted(id_to_channel.values()):
        data = bytearray(count)
        cursor = 0
        current = 0
        for index, value in levels[number]:
            offset = int(round(index - origin))
            if offset >= count:
                break
            while cursor < offset:
                data[cursor] = current
                cursor += 1
            current = value
        while cursor < count:
            data[cursor] = current
            cursor += 1
        channels.append(Channel(number=number, name=names.get(number, ""), samples=bytes(data)))

    frequency = int(round(1e12 / period_ps)) if period_ps else 0
    return Capture(
        frequency=frequency,
        pre_trigger_samples=0,
        post_trigger_samples=count,
        channels=channels,
        source=str(path.resolve()),
        warnings=[
            "read from VCD: the sample rate is the $timescale (the writer's convention); "
            "pre/post-trigger split and trigger config are not recoverable"
        ],
    )


def read_csv(path: str | Path, *, frequency: int) -> Capture:
    """Read a TerminalCapture ``.csv`` (samples only — hence the mandatory rate).

    The header is one column per channel. Names are used when present, otherwise
    ``Channel <position+1>``.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        rows = list(csv_module.reader(handle))
    if not rows:
        raise FormatError(f"{path}: empty CSV")
    header, body = rows[0], [r for r in rows[1:] if r]
    if not body:
        raise FormatError(f"{path}: CSV has a header but no sample rows")

    width = len(header)
    from la_lib import Channel

    channels = []
    for column in range(width):
        data = bytearray()
        for row in body:
            cell = row[column].strip() if column < len(row) else "0"
            data.append(1 if cell == "1" else 0)
        name = header[column].strip()
        channels.append(
            Channel(
                number=column,
                name="" if re.fullmatch(r"Channel \d+", name) else name,
                samples=bytes(data),
            )
        )
    return Capture(
        frequency=int(frequency),
        pre_trigger_samples=0,
        post_trigger_samples=len(body),
        channels=channels,
        source=str(path.resolve()),
        warnings=["read from CSV: the sample rate came from --frequency, not the file"],
    )


def load_capture(
    path: str | Path,
    *,
    frequency: int | None = None,
    channels: Sequence[int] | None = None,
) -> Capture:
    """Load a ``.lac`` / ``.vcd`` / ``.csv`` by extension.

    ``channels`` selects **0-based** channel numbers (the ``.lac`` convention).
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".lac":
        capture = read_lac(path, require_samples=True)
    elif suffix == ".vcd":
        capture = read_vcd(path)
    elif suffix == ".csv":
        if not frequency:
            raise FormatError(f"{path}: CSV carries no sample rate — pass --frequency <Hz>")
        capture = read_csv(path, frequency=frequency)
    else:
        raise FormatError(f"{path}: unsupported extension {suffix!r} (expected .lac, .vcd or .csv)")

    if channels is not None:
        wanted = set(channels)
        missing = wanted - {c.number for c in capture.channels}
        if missing:
            raise FormatError(f"{path}: no such channel(s) {sorted(missing)} in the capture")
        capture.channels = [c for c in capture.channels if c.number in wanted]
    return capture


# ------------------------------------------------------------------ measuring


def measure(
    capture: Capture,
    *,
    label: str | None = None,
    region: str = "post",
    max_intervals_kept: int = 10_000,
) -> Measurement:
    """Compute update rate, intervals and jitter for one capture.

    ``region`` selects which samples are analysed:

    * ``"post"`` (default) — from the trigger sample (``PreTriggerSamples``)
      onward. This is where the driven pattern lives; the pre-trigger region of a
      real capture is whatever the lines happened to be doing before the trigger,
      so including it would bias the rate with an unrelated signal.
    * ``"all"``  — the whole capture, including the pre-trigger region.
    * ``"pre"``  — only the pre-trigger region.

    **Rate estimate.** The update frequency is ``1 / mean(interval)``, where the
    intervals are the gaps between consecutive state changes. It is deliberately
    *not* ``changes / (last - first)``: with *N* changes spanning only *N-1*
    intervals, that expression counts *N* events where *N-1* occurred and reads
    **high** by ``N/(N-1)`` — 3 % low on a few dozen updates, converging as the
    capture lengthens. The alternative is reported as ``span_rate_hz`` so the
    difference is visible.

    Jitter, with intervals :math:`t_i`:

    * ``jitter_pp_us``  = ``max(t) - min(t)`` — peak-to-peak.
    * ``jitter_rms_us`` = population standard deviation of ``t``.
    * ``cv_percent``    = ``stddev / mean * 100`` — relative jitter.

    Fewer than two changes yields no intervals, hence no jitter — reported
    explicitly instead of as zeros. Fewer than three intervals makes the jitter
    numbers statistically meaningless, which is also said out loud.
    """
    channels = capture.data_channels()
    if not channels:
        raise FormatError("measure: capture has no sample data")
    if not capture.frequency:
        raise FormatError("measure: capture has no sample rate; cannot convert samples to time")

    count = min(len(c.samples) for c in channels)
    warnings = list(capture.warnings)

    start, stop = _region_bounds(capture, region, count)
    warnings.append(
        f"analysed samples [{start}, {stop}) of {count} — region {region!r}"
        + (" (post-trigger)" if region == "post" else "")
    )

    # Pack every selected channel into one integer state (bit 0 = first channel).
    # The width is not capped at 8: 24 channels is a legitimate bus here, and
    # truncating would corrupt the distinct-state count.
    states: list[int] = []
    for index in range(start, stop):
        value = 0
        for bit, channel in enumerate(channels):
            if channel.samples[index]:
                value |= 1 << bit
        states.append(value)

    span = stop - start
    change_indices = [i for i in range(1, span) if states[i] != states[i - 1]]
    distinct = len(set(states))

    sample_period_us = 1e6 / capture.frequency
    intervals = [
        (b - a) * sample_period_us for a, b in zip(change_indices, change_indices[1:])
    ]
    elapsed_us = (
        (change_indices[-1] - change_indices[0]) * sample_period_us if len(change_indices) >= 2 else 0.0
    )

    stats: dict[str, float | None] = {
        "mean_us": None, "median_us": None, "min_us": None, "max_us": None,
        "stddev_us": None, "jitter_pp_us": None, "jitter_rms_us": None, "cv_percent": None,
    }
    if intervals:
        stats["mean_us"] = statistics.fmean(intervals)
        stats["median_us"] = statistics.median(intervals)
        stats["min_us"] = min(intervals)
        stats["max_us"] = max(intervals)
        stats["stddev_us"] = statistics.pstdev(intervals) if len(intervals) > 1 else 0.0
        stats["jitter_pp_us"] = stats["max_us"] - stats["min_us"]
        stats["jitter_rms_us"] = stats["stddev_us"]
        stats["cv_percent"] = (
            100.0 * stats["stddev_us"] / stats["mean_us"] if stats["mean_us"] else None
        )

    update_hz = 1e6 / stats["mean_us"] if stats["mean_us"] else 0.0
    span_rate_hz = (len(change_indices) / (elapsed_us * 1e-6)) if elapsed_us > 0 else 0.0

    if len(change_indices) < 2:
        warnings.append(
            f"only {len(change_indices)} state change(s) observed — no intervals, so no rate or jitter"
        )
    elif len(intervals) < 3:
        warnings.append(
            f"only {len(intervals)} interval(s) — the jitter figures are not meaningful yet"
        )
    if len(channels) > 16:
        warnings.append(
            f"{len(channels)} channels are packed into one state value; 'distinct states' is "
            "exact but grows as 2^channels, so treat it as a sweep indicator, not a count of "
            "useful values"
        )
    if capture.sample_count < capture.total_samples:
        warnings.append(
            f"capture holds {capture.sample_count} of the {capture.total_samples} requested samples"
        )

    return Measurement(
        source=label or (capture.source or "<memory>"),
        frequency=capture.frequency,
        sample_count=span,
        channel_numbers=[c.number for c in channels],
        channels=[c.label for c in channels],
        elapsed_us=elapsed_us,
        changes=len(change_indices),
        distinct_states=distinct,
        update_hz=update_hz,
        span_rate_hz=span_rate_hz,
        period_us=(1e6 / update_hz) if update_hz else 0.0,
        region=region,
        intervals_us=intervals[:max_intervals_kept],
        stats=stats,
        warnings=warnings,
    )


def _region_bounds(capture: Capture, region: str, count: int) -> tuple[int, int]:
    """Resolve a region name to a ``[start, stop)`` sample range."""
    if region == "all":
        return 0, count
    if region == "pre":
        return 0, min(capture.pre_trigger_samples, count)
    if region == "post":
        start = min(capture.pre_trigger_samples, count)
        if start >= count:
            # A capture shorter than its own pre-trigger window: use everything
            # rather than measuring an empty region.
            return 0, count
        return start, count
    raise FormatError(f"measure: unknown region {region!r} (use post/all/pre)")


# ------------------------------------------------------------------ reporting


def _fmt(value: float | None, unit: str = "", digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}{unit}"


def _fmt_int(value: float) -> str:
    return f"{int(round(value)):,}"


def format_report(measurement: Measurement) -> str:
    """Human-readable report for the console."""
    m = measurement
    lines = [
        f"capture:  {m.source}",
        f"sampled:  {_fmt_int(m.sample_count)} samples at {m.frequency:,} Hz "
        f"({_fmt(1e6 / m.frequency if m.frequency else None, ' us')}/sample), region {m.region!r}",
        f"channels: {len(m.channels)} ({', '.join(m.channels)})",
        "",
        f"distinct states observed : {_fmt_int(m.distinct_states)}",
        f"state changes            : {_fmt_int(m.changes)}",
        f"observed span            : {_fmt(m.elapsed_us, ' us')}",
        f"update frequency         : {_fmt(m.update_hz, ' Hz')}"
        + (f"  ({m.update_hz / 1000:.3f} kHz)" if m.update_hz >= 1000 else ""),
        f"  = 1 / mean interval ({_fmt(m.stats['mean_us'], ' us')})",
        f"span rate (changes/span) : {_fmt(m.span_rate_hz, ' Hz')}"
        "  — reads high by changes/(changes-1); see measure() docstring",
        f"mean update period       : {_fmt(m.period_us, ' us')}",
        "",
        "interval between changes:",
        f"  mean                   : {_fmt(m.stats['mean_us'], ' us')}",
        f"  median                 : {_fmt(m.stats['median_us'], ' us')}",
        f"  min                    : {_fmt(m.stats['min_us'], ' us')}",
        f"  max                    : {_fmt(m.stats['max_us'], ' us')}",
        f"  stddev                 : {_fmt(m.stats['stddev_us'], ' us')}",
        f"  peak-to-peak jitter    : {_fmt(m.stats['jitter_pp_us'], ' us')}",
        f"  RMS jitter (stddev)    : {_fmt(m.stats['jitter_rms_us'], ' us')}",
        f"  coefficient of variation: {_fmt(m.stats['cv_percent'], ' %')}",
    ]
    for warning in m.warnings:
        lines.append(f"warning:  {warning}")
    lines += ["", NOT_A_CORRECTNESS_CHECK]
    return "\n".join(lines)


def format_markdown(measurement: Measurement, *, title: str = "Measurement") -> str:
    """A markdown section suitable for pasting into ``docs/measurements.md``.

    Written to stdout (or ``--markdown <path>``); it is never written into
    ``docs/`` automatically, because ``tests/`` is the only directory this
    tooling owns.
    """
    m = measurement
    rows = [
        ("Source", f"`{m.source}`"),
        ("Sample rate", f"{m.frequency:,} Hz"),
        ("Region analysed", f"`{m.region}`"),
        ("Samples analysed", _fmt_int(m.sample_count)),
        ("Channels", f"{len(m.channels)} — {', '.join(m.channels)}"),
        ("Distinct states observed", _fmt_int(m.distinct_states)),
        ("State changes", _fmt_int(m.changes)),
        ("Observed span (first to last change)", _fmt(m.elapsed_us, " µs")),
        ("**Update frequency** (1 / mean interval)", f"**{_fmt(m.update_hz, ' Hz')}**"),
        ("Span rate (changes / span)", _fmt(m.span_rate_hz, " Hz")),
        ("Mean update period", _fmt(m.period_us, " µs")),
        ("Mean interval", _fmt(m.stats["mean_us"], " µs")),
        ("Median interval", _fmt(m.stats["median_us"], " µs")),
        ("Min interval", _fmt(m.stats["min_us"], " µs")),
        ("Max interval", _fmt(m.stats["max_us"], " µs")),
        ("Standard deviation", _fmt(m.stats["stddev_us"], " µs")),
        ("Peak-to-peak jitter", _fmt(m.stats["jitter_pp_us"], " µs")),
        ("RMS jitter", _fmt(m.stats["jitter_rms_us"], " µs")),
        ("Coefficient of variation", _fmt(m.stats["cv_percent"], " %")),
    ]
    out = [f"## {title}", "", "| Metric | Value |", "|---|---|"]
    out += [f"| {name} | {value} |" for name, value in rows]
    if m.warnings:
        out += ["", "Warnings:", ""]
        out += [f"- {w}" for w in m.warnings]
    out += [
        "",
        f"> {NOT_A_CORRECTNESS_CHECK}",
        "",
    ]
    return "\n".join(out)


# ----------------------------------------------------------------------- CLI


def _parse_channel_spec(spec: str | None) -> list[int] | None:
    """Parse ``"1,2,3"`` / ``"1-8"`` into **1-based** numbers (converted by caller)."""
    if not spec:
        return None
    numbers: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if match:
            numbers.extend(range(int(match.group(1)), int(match.group(2)) + 1))
        elif token.isdigit():
            numbers.append(int(token))
        else:
            raise SystemExit(f"bad channel spec: {token!r}")
    return sorted(set(numbers))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="measure.py",
        description=(
            "Measure update rate, intervals and jitter from a logic-analyzer capture. "
            "This is a MEASUREMENT, not a correctness check (TASK.md §5.2)."
        ),
        epilog=(
            "examples:\n"
            "  python measure.py capture.lac\n"
            "  python measure.py capture.lac --json\n"
            "  python measure.py capture.lac --channels 1-8 --markdown out.md\n"
            "  python measure.py capture.csv --frequency 20000000\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("capture", help=".lac / .vcd / .csv capture")
    parser.add_argument("--channels", help="1-based channels to treat as the bus, e.g. 1-8")
    parser.add_argument("--frequency", type=int, help="sample rate in Hz (required for .csv)")
    parser.add_argument(
        "--region",
        choices=("post", "all", "pre"),
        default="post",
        help=(
            "samples to analyse: 'post' (default) = from the trigger onward, where the "
            "driven pattern lives; 'all' = whole capture; 'pre' = pre-trigger only"
        ),
    )
    parser.add_argument("--label", help="name to show as the capture source")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--markdown", help="also write a markdown section to this path")
    args = parser.parse_args(argv)

    selected = _parse_channel_spec(args.channels)
    # CLI is 1-based; the capture's channels are 0-based.
    zero_based = [n - 1 for n in selected] if selected else None

    try:
        capture = load_capture(args.capture, frequency=args.frequency, channels=zero_based)
        result = measure(capture, label=args.label, region=args.region)
    except (FormatError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(result.to_json())
    else:
        print(format_report(result))

    if args.markdown:
        target = Path(args.markdown)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            format_markdown(result, title=f"Measurement — {Path(args.capture).name}"),
            encoding="utf-8",
        )
        print(f"\nwrote markdown report to {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

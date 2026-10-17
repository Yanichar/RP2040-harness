"""Offline tests for :mod:`measure` — update rate, intervals and jitter.

These are the tests that make the claim "measure.py recovers a known frequency"
checkable: the fixture is a counter whose update period is fixed by
``fixture_gen``'s constants, so the measured rate must come back within tolerance.

No hardware is involved. The same tests also pin down the *non*-claim: every
report must say it is a measurement, not a correctness check.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import fixture_gen  # noqa: E402
import la_lib  # noqa: E402
import measure  # noqa: E402
from la_lib import Capture, Channel, FormatError  # noqa: E402
from measure import NOT_A_CORRECTNESS_CHECK, load_capture, measure as measure_capture  # noqa: E402


# ---------------------------------------------------------- frequency recovery


def test_measure_recovers_the_known_update_frequency(
    flat_capture: Capture, expected_metrics: dict[str, float]
) -> None:
    """The headline requirement: a known pattern must measure to its known rate.

    ``flat.lac`` is a counter that changes every 8 samples at 20 MHz, i.e. every
    400 ns. ``measure`` must recover 2.5 MHz from the file within 1 %.
    """
    result = measure_capture(flat_capture)

    expected_hz = expected_metrics["update_hz"]  # 2.5 MHz for the flat fixture
    assert result.update_hz == pytest.approx(expected_hz, rel=0.01), (
        f"expected ~{expected_hz:,.0f} Hz, measured {result.update_hz:,.0f} Hz"
    )
    assert result.frequency == expected_metrics["frequency"]
    assert result.changes == int(expected_metrics["changes"])
    assert result.distinct_states == int(expected_metrics["distinct_states"])
    assert result.region == "post"
    assert result.sample_count == int(expected_metrics["analysed_samples"])
    assert result.elapsed_us == pytest.approx(expected_metrics["elapsed_us"], rel=1e-9)


def test_span_rate_and_interval_rate_agree_on_a_long_capture(
    flat_capture: Capture, expected_metrics: dict[str, float]
) -> None:
    """The two rate estimates differ only by the half-period end effect.

    ``changes / span`` over *N* changes covering *N-1* intervals counts *N* where
    the true count is *N-1*, so it reads **high** by ``N/(N-1)`` — 3 % here, and
    shrinking as the capture lengthens. ``1 / mean interval`` is the correct one.
    """
    result = measure_capture(flat_capture)

    assert result.changes == 31
    assert result.span_rate_hz > result.update_hz
    assert result.span_rate_hz == pytest.approx(
        result.update_hz * result.changes / (result.changes - 1), rel=1e-9
    )
    true_hz = expected_metrics["update_hz"]
    assert result.update_hz == pytest.approx(true_hz, rel=1e-9)
    assert result.span_rate_hz == pytest.approx(true_hz, rel=0.05)


def test_update_period_matches_the_hold_time(
    flat_capture: Capture, expected_metrics: dict[str, float]
) -> None:
    result = measure_capture(flat_capture)

    assert result.period_us == pytest.approx(expected_metrics["update_period_us"], rel=0.01)
    assert result.stats["mean_us"] == pytest.approx(expected_metrics["update_period_us"], rel=1e-6)
    assert result.stats["median_us"] == pytest.approx(expected_metrics["update_period_us"], rel=1e-6)
    assert result.stats["min_us"] == pytest.approx(expected_metrics["update_period_us"], rel=1e-6)
    assert result.stats["max_us"] == pytest.approx(expected_metrics["update_period_us"], rel=1e-6)


def test_a_sample_aligned_fixture_has_zero_jitter(
    flat_capture: Capture,
) -> None:
    """The synthetic counter steps exactly every 8 samples, so jitter must be 0."""
    result = measure_capture(flat_capture)

    assert result.stats["stddev_us"] == pytest.approx(0.0, abs=1e-9)
    assert result.stats["jitter_pp_us"] == pytest.approx(0.0, abs=1e-9)
    assert result.stats["cv_percent"] == pytest.approx(0.0, abs=1e-9)


def test_intervals_count_is_changes_minus_one(
    flat_capture: Capture,
) -> None:
    result = measure_capture(flat_capture)
    assert len(result.intervals_us) == result.changes - 1
    assert result.changes == int(fixture_gen.expected_flat_metrics()["changes"])
    assert len(result.intervals_us) == int(fixture_gen.expected_flat_metrics()["intervals"])
    assert all(interval > 0 for interval in result.intervals_us)


def test_measure_works_through_the_lac_reader(
    flat_lac_path: Path, expected_metrics: dict[str, float]
) -> None:
    """End to end: file -> read_lac -> measure, recovering the same rate."""
    capture = load_capture(flat_lac_path)
    result = measure_capture(capture)
    assert result.update_hz == pytest.approx(expected_metrics["update_hz"], rel=0.01)


def test_channel_selection_changes_what_is_measured(
    flat_lac_path: Path, expected_metrics: dict[str, float]
) -> None:
    """Selecting only bit 0 halves nothing here — it must still find the same period.

    Bit 0 toggles on every counter step, so the change *rate* is the same as for
    the whole bus; the distinct-state count drops to 2.
    """
    capture = load_capture(flat_lac_path, channels=[0])
    result = measure_capture(capture)

    assert result.channel_numbers == [0]
    assert result.distinct_states == 2
    assert result.update_hz == pytest.approx(expected_metrics["update_hz"], rel=0.01)


# ------------------------------------------------------------ jitter handling


def test_jitter_is_measured_on_an_uneven_pattern() -> None:
    """Alternate a 1-sample gap and a 3-sample gap -> 2 and 6 us at 500 kHz."""
    samples = bytearray()
    level = 0
    # changes at indices 0, 1, 4, 5, 8, 9, ... -> intervals of 1 and 3 samples
    index = 0
    while index < 200:
        samples.extend([level] * (1 if len(samples) % 2 == 0 else 3))
        level ^= 1
        index += 1
    capture = Capture(
        frequency=500_000,
        pre_trigger_samples=0,
        post_trigger_samples=len(samples),
        channels=[Channel(0, "CLK", bytes(samples))],
    )
    result = measure_capture(capture)

    assert result.stats["min_us"] == pytest.approx(2.0, rel=1e-6)
    assert result.stats["max_us"] == pytest.approx(6.0, rel=1e-6)
    assert result.stats["jitter_pp_us"] == pytest.approx(4.0, rel=1e-6)
    # 99 gaps of 2 us and 99 of 6 us: population stddev is exactly 2.0 us.
    assert result.stats["stddev_us"] == pytest.approx(2.0, rel=1e-9)
    assert result.stats["mean_us"] == pytest.approx(4.0, rel=1e-6)
    assert result.stats["cv_percent"] == pytest.approx(50.0, rel=1e-6)


def test_a_single_change_yields_no_rate_and_says_so() -> None:
    capture = Capture(
        frequency=1_000_000,
        pre_trigger_samples=0,
        post_trigger_samples=100,
        channels=[Channel(0, "one-shot", bytes([0] * 50 + [1] * 50))],
    )
    result = measure_capture(capture)

    assert result.changes == 1
    assert result.update_hz == 0.0
    assert result.stats["mean_us"] is None
    assert result.stats["jitter_pp_us"] is None
    assert any("no intervals" in w for w in result.warnings)


def test_a_constant_capture_reports_zero_changes() -> None:
    capture = Capture(
        frequency=1_000_000,
        pre_trigger_samples=0,
        post_trigger_samples=64,
        channels=[Channel(0, "idle", bytes(64))],
    )
    result = measure_capture(capture)
    assert result.changes == 0
    assert result.distinct_states == 1
    assert result.update_hz == 0.0
    assert result.stats["median_us"] is None


def test_measure_rejects_a_capture_without_a_sample_rate() -> None:
    capture = Capture(
        frequency=0,
        pre_trigger_samples=0,
        post_trigger_samples=4,
        channels=[Channel(0, "x", bytes([0, 1, 0, 1]))],
    )
    with pytest.raises(FormatError):
        measure_capture(capture)


def test_measure_rejects_a_settings_only_capture(tmp_path: Path) -> None:
    path = tmp_path / "s.tcs"
    la_lib.write_tcs(path, channels="1-4", pre_trigger_samples=16, post_trigger_samples=64)
    with pytest.raises(FormatError):
        load_capture(path)


# --------------------------------------------------------------- 24-channel bus


def test_distinct_states_are_not_truncated_above_eight_channels() -> None:
    """A 24-bit bus must count all of its states, not just the low byte."""
    total = 4096
    samples = [bytearray(total) for _ in range(24)]
    for index in range(total):
        value = index  # a full 12-bit sweep
        for bit in range(24):
            samples[bit][index] = (value >> bit) & 1
    capture = Capture(
        frequency=1_000_000,
        pre_trigger_samples=0,
        post_trigger_samples=total,
        channels=[Channel(n, f"b{n}", bytes(samples[n])) for n in range(24)],
    )
    result = measure_capture(capture)

    assert result.distinct_states == total, "every value of the sweep is a distinct state"
    assert result.changes == total - 1
    assert any("packed into one state value" in w for w in result.warnings)


# -------------------------------------------------------------------- loaders


def test_load_capture_reads_vcd_and_recovers_the_rate(
    tmp_path: Path, expected_metrics: dict[str, float]
) -> None:
    capture = fixture_gen.flat_capture()
    la_lib.write_vcd(tmp_path / "flat.vcd", capture)

    loaded = load_capture(tmp_path / "flat.vcd")
    result = measure_capture(loaded)

    assert loaded.frequency == pytest.approx(fixture_gen.FLAT_FREQUENCY, rel=1e-6)
    assert result.update_hz == pytest.approx(expected_metrics["update_hz"], rel=0.05)


def test_load_capture_reads_csv_only_with_a_frequency(
    tmp_path: Path, expected_metrics: dict[str, float]
) -> None:
    capture = fixture_gen.flat_capture()
    la_lib.write_csv(tmp_path / "flat.csv", capture)

    with pytest.raises(FormatError, match="frequency"):
        load_capture(tmp_path / "flat.csv")

    loaded = load_capture(tmp_path / "flat.csv", frequency=fixture_gen.FLAT_FREQUENCY)
    result = measure_capture(loaded)
    assert result.update_hz == pytest.approx(expected_metrics["update_hz"], rel=0.01)
    assert any("CSV" in w for w in result.warnings)


def test_load_capture_rejects_an_unknown_extension(tmp_path: Path) -> None:
    path = tmp_path / "capture.what"
    path.write_text("", encoding="utf-8")
    with pytest.raises(FormatError, match="unsupported extension"):
        load_capture(path)


def test_load_capture_rejects_a_missing_channel(flat_lac_path: Path) -> None:
    with pytest.raises(FormatError, match="no such channel"):
        load_capture(flat_lac_path, channels=[99])


# ------------------------------------------------------------------- reports


def test_report_states_it_is_not_a_correctness_check(flat_capture: Capture) -> None:
    result = measure_capture(flat_capture)
    text = measure.format_report(result)
    markdown = measure.format_markdown(result)

    assert NOT_A_CORRECTNESS_CHECK in text
    assert NOT_A_CORRECTNESS_CHECK in markdown
    assert "not a correctness check" in NOT_A_CORRECTNESS_CHECK.lower()
    assert "Update frequency" in markdown or "update frequency" in text


def test_report_contains_the_headline_numbers(
    flat_capture: Capture, expected_metrics: dict[str, float]
) -> None:
    result = measure_capture(flat_capture)
    text = measure.format_report(result)

    assert "distinct states observed" in text
    assert "peak-to-peak jitter" in text
    assert "coefficient of variation" in text.lower()
    assert f"{int(expected_metrics['changes']):,}" in text


def test_json_output_is_serialisable_and_complete(flat_capture: Capture) -> None:
    result = measure_capture(flat_capture)
    payload = json.loads(result.to_json())

    assert payload["update_hz"] > 0
    assert payload["disclaimer"] == NOT_A_CORRECTNESS_CHECK
    for key in ("frequency", "sample_count", "channels", "changes", "distinct_states", "stats"):
        assert key in payload
    assert set(payload["stats"]) == {
        "mean_us", "median_us", "min_us", "max_us",
        "stddev_us", "jitter_pp_us", "jitter_rms_us", "cv_percent",
    }


def test_markdown_report_is_a_table(flat_capture: Capture) -> None:
    markdown = measure.format_markdown(measure_capture(flat_capture), title="Run 1")
    lines = markdown.splitlines()

    assert lines[0] == "## Run 1"
    assert "| Metric | Value |" in markdown
    assert "|---|---|" in markdown
    assert any("Update frequency" in line for line in lines)


# ------------------------------------------------------------------------ CLI


def test_cli_prints_a_report(flat_lac_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert measure.main([str(flat_lac_path)]) == 0
    out = capsys.readouterr().out
    assert "update frequency" in out
    assert NOT_A_CORRECTNESS_CHECK in out


def test_cli_json_output(flat_lac_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert measure.main([str(flat_lac_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changes"] == int(fixture_gen.expected_flat_metrics()["changes"])


def test_cli_accepts_one_based_channel_selection(
    flat_lac_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert measure.main([str(flat_lac_path), "--channels", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["channel_numbers"] == [0], "--channels is 1-based on the CLI"
    assert payload["distinct_states"] == 2


def test_cli_writes_a_markdown_report(
    flat_lac_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "measurements.md"
    assert measure.main([str(flat_lac_path), "--markdown", str(target)]) == 0
    capsys.readouterr()
    text = target.read_text(encoding="utf-8")
    assert text.startswith("## Measurement")
    assert NOT_A_CORRECTNESS_CHECK in text


def test_cli_reports_a_bad_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.lac"
    bad.write_text("not json", encoding="utf-8")
    assert measure.main([str(bad)]) == 1
    assert "error:" in capsys.readouterr().err


# ------------------------------------------------------------- synthetic sanity


def test_fixture_constants_are_self_consistent() -> None:
    """Guard the numbers the other tests rely on rather than trusting them blindly."""
    metrics = fixture_gen.expected_flat_metrics()
    assert fixture_gen.FLAT_POST % fixture_gen.FLAT_HOLD_SAMPLES == 0
    assert math.isclose(
        metrics["update_hz"], fixture_gen.FLAT_FREQUENCY / fixture_gen.FLAT_HOLD_SAMPLES
    )
    # One change fewer than hold periods: the region's first sample has no
    # predecessor inside the region.
    assert metrics["changes"] == fixture_gen.FLAT_POST / fixture_gen.FLAT_HOLD_SAMPLES - 1
    assert metrics["intervals"] == metrics["changes"] - 1

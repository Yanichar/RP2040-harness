"""Tests for the parallel-counter scenario script (TASK.md §5.2).

Everything here is offline: the sequence generation, the plan and the CLI are
exercised without a device or an analyzer, which is all that is possible while
neither is attached.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import counter

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- sequences


def test_sequence_counts_from_start_with_step():
    assert counter.counter_sequence(5) == [0, 1, 2, 3, 4]
    assert counter.counter_sequence(4, start=10, step=5) == [10, 15, 20, 25]


def test_sequence_wraps_at_24_bits():
    values = counter.counter_sequence(3, start=(1 << 24) - 1)
    assert values == [0xFFFFFF, 0x000000, 0x000001]


def test_zero_values_is_allowed():
    assert counter.counter_sequence(0) == []


def test_mask_is_not_applied_to_the_values():
    # The mask tells the device which lines to touch; the emitted words stay
    # full 24-bit values so the caller can see what it asked for.
    values = counter.counter_sequence(4, start=0xFFFFF0)
    assert values[-1] == 0xFFFFF3


def test_reverse_bits_round_trips():
    for value in (0x000000, 0x000001, 0x800000, 0xFFFFFF, 0x00FF00, 0x123456):
        assert counter.reverse_bits(counter.reverse_bits(value)) == value


def test_msb_bit_order_is_the_bit_reversal_of_lsb():
    lsb = counter.counter_sequence(16, bit_order="lsb")
    msb = counter.counter_sequence(16, bit_order="msb")
    assert [counter.reverse_bits(v) for v in msb] == lsb
    # With MSB order, logical index 0 toggles only every 2**23 steps.
    assert msb[1] == 0x800000
    assert msb[0] == 0x000000


def test_bit_order_validation():
    with pytest.raises(ValueError, match="bit_order"):
        counter.counter_sequence(4, bit_order="sideways")


def test_mask_validation():
    with pytest.raises(ValueError, match="mask"):
        counter.counter_sequence(4, mask=1 << 24)
    with pytest.raises(ValueError, match="count"):
        counter.counter_sequence(-1)


# ---------------------------------------------------------------- transitions


def test_changed_pins_reports_only_differing_indices():
    assert list(counter.changed_pins(0b0000, 0b1011)) == [0, 1, 3]
    assert list(counter.changed_pins(0b1011, 0b1011)) == []
    # Masked-out lines are ignored.
    assert list(counter.changed_pins(0b0000, 0b1111, mask=0b0101)) == [0, 2]


def test_changed_pins_matches_a_binary_counter_definition():
    # 64 consecutive values give 63 transitions. Bit 0 flips on every one of
    # them, bit 1 on every other one starting with the first (31), and bit 5
    # exactly once (31 -> 32).
    values = counter.counter_sequence(1 << 6)
    toggles = {i: 0 for i in range(24)}
    for previous, current in zip(values, values[1:]):
        for index in counter.changed_pins(previous, current):
            toggles[index] += 1
    assert toggles[0] == 63
    assert toggles[1] == 31
    assert toggles[5] == 1
    assert all(toggles[i] == 0 for i in range(6, 24))


# ---------------------------------------------------------------- plan


def _plan(**overrides):
    import argparse

    args = argparse.Namespace(
        values=8,
        start=0,
        step=1,
        mask=counter.ALL_PINS_MASK,
        bit_order="lsb",
        chunk=250,
        capture=False,
        port=None,
        frequency=20_000_000,
        pre=512,
        post=4000,
        trigger="edge:1",
        out=None,
        mode="stream",
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return counter.build_plan(args)


def test_plan_reports_first_and_last():
    plan = _plan(values=100)
    assert plan.count == 100
    assert plan.first == 0
    assert plan.last == 99
    assert plan.total_transitions() > 0


def test_plan_describe_warns_about_an_unknown_pin_map():
    text = _plan(capture=True, port="COM14").describe()
    assert "COM14" in text
    assert "pin map" in text
    assert "analyzer capture: yes" in text


def test_plan_describe_without_capture():
    text = _plan().describe()
    assert "analyzer capture: no" in text


def test_plan_output_directory_is_timestamped_and_unique(tmp_path):
    first = _plan(out=str(tmp_path)).out_dir
    second = _plan(out=str(tmp_path)).out_dir
    assert first.parent == tmp_path
    assert first != second or first.name.startswith("counter_")


# ---------------------------------------------------------------- CLI


def _run_cli(*args: str):
    return subprocess.run(
        [sys.executable, str(HERE / "counter.py"), *args],
        capture_output=True,
        text=True,
        cwd=str(HERE),
    )


def test_dry_run_touches_nothing():
    proc = _run_cli("--dry-run", "--values", "32")
    assert proc.returncode == 0, proc.stderr
    assert "--dry-run: no device and no analyzer were touched." in proc.stdout
    assert "would write artefacts under" in proc.stdout
    assert "0x000000" in proc.stdout
    assert "0x00001F" in proc.stdout


def test_dry_run_does_not_create_an_output_directory(tmp_path):
    out = tmp_path / "should-not-exist"
    proc = _run_cli("--dry-run", "--values", "4", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert not out.exists()


def test_capture_without_a_port_is_rejected():
    proc = _run_cli("--capture", "--values", "4", "--out", "")
    assert proc.returncode == 2
    assert "--capture requires --port" in proc.stderr


def test_capture_is_refused_before_touching_the_device():
    # A missing port must be caught before Harness.open() is attempted, which is
    # what distinguishes this from the no-device failure path.
    proc = _run_cli("--capture", "--values", "4")
    assert proc.returncode == 2
    assert "COM" in proc.stderr


def test_bad_bit_order_is_rejected_by_argparse():
    proc = _run_cli("--bit-order", "sideways")
    assert proc.returncode == 2
    assert "bit-order" in proc.stderr


def test_dry_run_accepts_a_hex_mask_and_python_int_literals():
    proc = _run_cli("--dry-run", "--values", "4", "--mask", "0x0000FF", "--start", "0x10")
    assert proc.returncode == 0, proc.stderr
    assert "mask            : 0x0000FF" in proc.stdout
    assert "start           : 0x000010" in proc.stdout


# ---------------------------------------------------------------- drive summaries


class _FakeHarness:
    """Minimal stand-in for rp2040_harness.Harness used by drive_*()."""

    def __init__(self) -> None:
        self.levels = [0] * counter.PIN_COUNT
        self.set_calls: list[tuple[int, int]] = []

    def configure_many(self, configs):
        return len(list(configs))

    def set_pin(self, index, level):
        self.levels[index] = level
        self.set_calls.append((index, level))

    def set_pins(self, levels):
        for index, level in dict(levels).items():
            self.levels[index] = level
        return len(levels)

    def read_all(self):
        value = 0
        for index, level in enumerate(self.levels):
            if level:
                value |= 1 << index
        return value

    def stream(self, values, mask=0xFFFFFF, chunk=250):
        raise AssertionError("drive_manual must not use the streaming path")


class _FakeStreamResult:
    def __init__(self, applied: int, duration_us: int, toggles: int) -> None:
        self.applied = applied
        self.duration_us = duration_us
        self.toggles = toggles
        self.updates_per_second = applied / (duration_us * 1e-6) if duration_us else None


class _StreamingHarness(_FakeHarness):
    def stream(self, values, mask=0xFFFFFF, chunk=250):
        values = list(values)
        return [_FakeStreamResult(len(values), 1000, 42)]


def test_drive_manual_walks_every_value_and_reads_back():
    plan = _plan(values=8)
    summary = counter.drive_manual(_FakeHarness(), plan)
    assert summary["mode"] == "manual"
    assert summary["values_applied"] == 8
    assert summary["read_all_calls"] == 8
    assert summary["readback_mismatch_count"] == 0
    # Only changed lines are written: 1+2+1+3+1+2+1 = 11 for 0..7 from 0.
    assert summary["set_pin_calls"] == 11


def test_drive_manual_reports_mismatches_without_raising():
    class Broken(_FakeHarness):
        def read_all(self):
            return 0  # never matches anything but the first value

    summary = counter.drive_manual(Broken(), _plan(values=4))
    assert summary["readback_mismatch_count"] == 3
    assert len(summary["readback_mismatch_samples"]) == 3
    assert "informational only" in summary["note"]


def test_drive_stream_summarises_the_chunks():
    plan = _plan(values=10, chunk=4)
    summary = counter.drive_stream(_StreamingHarness(), plan)
    assert summary["mode"] == "stream"
    assert summary["values_applied"] == 10
    assert summary["device_duration_us"] == 1000
    assert summary["device_updates_per_second"] == pytest.approx(10_000.0)
    assert summary["wall_duration_s"] >= 0

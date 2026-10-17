"""Offline tests for :mod:`pinmap_scan` — the stepping plan and the matcher.

The scan itself needs hardware (a running capture plus the RP2040 harness package,
which does not exist yet). What *can* be tested without either is the part that
carries the logic: the stepping plan, the predicted trigger positions, and the
matcher that turns a capture into a GP <-> channel table. The matching tests build
synthetic captures with edges at the positions the plan predicts, so they verify
the algorithm rather than the wiring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import pinmap_scan  # noqa: E402
from la_lib import Capture, Channel  # noqa: E402
from pinmap_scan import (  # noqa: E402
    LOGICAL_TO_GPIO,
    PIN_COUNT,
    ScanPlan,
    build_stepping_plan,
    match_indices,
    render_document,
    render_table,
    summarize,
)

# --------------------------------------------------------------- pin mapping


def test_logical_index_to_gpio_follows_the_protocol_document() -> None:
    """docs/protocol.md §3: indices 0-20 -> GP2-GP22, then 21-23 -> GP26-GP28."""
    assert list(LOGICAL_TO_GPIO) == list(range(2, 23)) + [26, 27, 28]
    assert LOGICAL_TO_GPIO[0] == 2
    assert LOGICAL_TO_GPIO[20] == 22
    assert LOGICAL_TO_GPIO[21] == 26
    assert LOGICAL_TO_GPIO[23] == 28
    # GP23-25 and GP29 are occupied by the board and must not appear.
    assert not ({23, 24, 25, 29} & set(LOGICAL_TO_GPIO))
    assert len(set(LOGICAL_TO_GPIO)) == PIN_COUNT


# ------------------------------------------------------------ stepping plan


def test_stepping_plan_drives_one_line_at_a_time() -> None:
    plan = build_stepping_plan()
    mask, value = plan[0]
    assert mask == (1 << PIN_COUNT) - 1, "every step must address all 24 bits atomically"

    # Skip the leading all-low steps; then each stepping step must have exactly
    # one bit set, and the bits must cover every index exactly once.
    stepping = [v for _, v in plan if bin(v).count("1") == 1]
    bits = [v.bit_length() - 1 for v in stepping]
    assert sorted(set(bits)) == list(range(PIN_COUNT)), "every index must be driven high"
    assert bits.count(0) == 2, "index 0 is pulsed twice: once as the trigger, once as its step"
    assert all(bin(v).count("1") == 1 for v in stepping)


def test_stepping_plan_starts_and_ends_low() -> None:
    plan = build_stepping_plan(pre_steps=3, tail_steps=4)
    assert [v for _, v in plan[:3]] == [0, 0, 0]
    assert [v for _, v in plan[-4:]] == [0, 0, 0, 0]


def test_stepping_plan_trigger_pulse_comes_before_the_sweep() -> None:
    plan = build_stepping_plan(pre_steps=2, tail_steps=0)
    values = [v for _, v in plan]
    # layout: [0, 0] + [trigger=bit0] + [bit0..bit23]
    assert values[:3] == [0, 0, 1]
    assert values[3] == 1
    assert values[4] == 2
    assert values[-1] == 1 << 23


def test_stepping_plan_step_count() -> None:
    plan = build_stepping_plan(pre_steps=8, tail_steps=8)
    assert len(plan) == 8 + 1 + PIN_COUNT + 8


# --------------------------------------------------------- expected positions


def _plan(**kwargs: object) -> ScanPlan:
    defaults = dict(
        port="COM_TEST",
        frequency=100_000,
        pre=256,
        post=10_000,
        step_delay_s=0.002,
        timeout_s=30.0,
        trigger="edge:1",
        steps=build_stepping_plan(),
    )
    defaults.update(kwargs)
    return ScanPlan(**defaults)  # type: ignore[arg-type]


def test_expected_rising_sample_is_anchored_on_the_trigger() -> None:
    plan = _plan()
    # 2 ms at 100 kHz = 200 samples per step.
    assert plan.expected_rising_sample(0) == 256
    assert plan.expected_rising_sample(1) == 256 + 200
    assert plan.expected_rising_sample(5) == 256 + 1000
    assert plan.expected_rising_sample(23) == 256 + 4600


# ------------------------------------------------------------------ matcher


def _capture_with_steps(plan: ScanPlan, *, mapped: dict[int, int], shift: int = 0) -> Capture:
    """Build a capture where logical index i moves on analyzer channel mapped[i].

    ``shift`` offsets every edge by that many samples, to exercise the tolerance.
    """
    total = plan.pre + plan.post
    step_samples = int(round(plan.step_delay_s * plan.frequency))
    channels = [
        Channel(number=number, name=f"AN{number + 1}", samples=bytearray(total))
        for number in range(PIN_COUNT)
    ]
    for logical_index, analyzer_channel in mapped.items():
        start = int(round(plan.expected_rising_sample(logical_index))) + shift
        for index in range(start, total):
            channels[analyzer_channel].samples[index] = 1
    return Capture(
        frequency=plan.frequency,
        pre_trigger_samples=plan.pre,
        post_trigger_samples=plan.post,
        channels=[Channel(c.number, c.name, bytes(c.samples)) for c in channels],
        trigger_channel=0,
    )


def test_match_indices_recovers_an_identity_mapping() -> None:
    plan = _plan()
    capture = _capture_with_steps(plan, mapped={i: i for i in range(PIN_COUNT)})

    results = match_indices(capture, plan)

    assert [r.analyzer_channel for r in results] == list(range(PIN_COUNT))
    assert all(r.confidence == "high" for r in results)
    assert all(r.confirmed for r in results)
    assert [r.gpio for r in results] == list(LOGICAL_TO_GPIO)


def test_match_indices_recovers_a_known_shuffled_wiring() -> None:
    """The realistic case: the header ports are wired in some arbitrary order."""
    plan = _plan()
    # Reverse the mapping, then make it a rotation with a gap so it is not a
    # symmetrical permutation (a reversal plus identity would hide index bugs).
    mapping = {i: (i * 7 + 3) % PIN_COUNT for i in range(PIN_COUNT)}
    assert len(set(mapping.values())) == PIN_COUNT, "the test wiring must be a permutation"
    capture = _capture_with_steps(plan, mapped=mapping)

    results = match_indices(capture, plan)

    assert {r.logical_index: r.analyzer_channel for r in results} == mapping
    assert all(r.confidence == "high" for r in results)
    assert summarize(results)["high"] == PIN_COUNT


def test_match_indices_tolerates_a_small_timing_shift() -> None:
    plan = _plan()
    capture = _capture_with_steps(plan, mapped={i: i for i in range(PIN_COUNT)}, shift=40)
    results = match_indices(capture, plan)

    assert all(r.confirmed for r in results)
    assert all(r.delta_samples == pytest.approx(40) for r in results)


def test_match_indices_marks_an_out_of_tolerance_shift_as_unmatched() -> None:
    """Beyond half a step interval the edge is not this index's edge."""
    plan = _plan()
    step_samples = int(round(plan.step_delay_s * plan.frequency))  # 200
    capture = _capture_with_steps(plan, mapped={i: i for i in range(PIN_COUNT)}, shift=150)
    results = match_indices(capture, plan)

    # Shifted by 150 of a 200-sample window: still inside the +/-100 window only
    # for the shifted position of the *neighbouring* index, so claims collide.
    assert summarize(results)["high"] + summarize(results)["medium"] < PIN_COUNT


def test_match_indices_never_reports_one_channel_as_two_indices() -> None:
    """A channel bound for one index must be unavailable to another.

    The invariant that matters: at most one logical index can hold a high (or
    medium) confidence claim on a given analyzer channel. If the reservation step
    only remembered high-confidence bindings, an index that bound marginally and
    the next index that bound the same channel would both read as confident —
    two different facts asserted from one wire.
    """
    plan = _plan()
    mappings = [
        {i: i for i in range(PIN_COUNT)},
        {i: (i * 7 + 3) % PIN_COUNT for i in range(PIN_COUNT)},
        {i: i % 8 for i in range(PIN_COUNT)},  # deliberately non-injective wiring
    ]
    for mapping in mappings:
        capture = _capture_with_steps(plan, mapped=mapping)
        results = match_indices(capture, plan)
        confident = [
            r.analyzer_channel for r in results if r.confidence in ("high", "medium")
        ]
        assert len(confident) == len(set(confident)), (
            f"mapping {mapping} produced two confident claims on one channel: {confident}"
        )


def test_match_indices_flags_a_marginal_binding_as_medium() -> None:
    """An edge beyond half a window is 'medium' and say so, not silently 'high'."""
    plan = _plan()
    window = 0.5 * plan.step_delay_s * plan.frequency  # 100 samples
    capture = _capture_with_steps(
        plan, mapped={i: i for i in range(PIN_COUNT)}, shift=int(window * 0.75)
    )
    results = match_indices(capture, plan)

    assert all(r.confidence == "medium" for r in results)
    assert all("tolerance limit" in r.note for r in results)
    assert all(r.confirmed for r in results), "medium still counts as confirmed"
    assert summarize(results)["medium"] == PIN_COUNT


def test_match_indices_leaves_an_index_unmatched_when_its_edge_is_missing() -> None:
    """A line that never moved is reported as unmatched, not guessed."""
    plan = _plan()
    capture = _capture_with_steps(plan, mapped={7: 7})
    results = match_indices(capture, plan)

    by_index = {r.logical_index: r for r in results}
    assert by_index[7].analyzer_channel == 7
    assert by_index[7].confidence == "high"
    assert by_index[0].confidence == "none"
    assert by_index[0].analyzer_channel is None
    assert "no analyzer channel" in by_index[0].note
    assert summarize(results)["none"] == PIN_COUNT - 1


def test_match_indices_reports_indices_with_no_edge() -> None:
    plan = _plan()
    capture = _capture_with_steps(plan, mapped={1: 1, 2: 2})
    results = match_indices(capture, plan)

    by_index = {r.logical_index: r for r in results}
    assert by_index[0].confidence == "none"
    assert by_index[0].analyzer_channel is None
    assert "no analyzer channel" in by_index[0].note
    assert by_index[1].analyzer_channel == 1
    assert by_index[2].analyzer_channel == 2


def test_match_indices_rejects_a_capture_without_samples() -> None:
    plan = _plan()
    empty = Capture(frequency=1000, pre_trigger_samples=0, post_trigger_samples=0)
    with pytest.raises(Exception, match="no sample data"):
        match_indices(empty, plan)


# ------------------------------------------------------------------ reports


def test_render_table_has_a_running_confirmation_column() -> None:
    plan = _plan()
    capture = _capture_with_steps(plan, mapped={i: i for i in range(PIN_COUNT)})
    table = render_table(match_indices(capture, plan))

    assert "| Logical index | GPIO | Analyzer channel | Confirmed empirically |" in table
    assert "| 0 | GP2 | 1 | yes |" in table
    assert "| 21 | GP26 | 22 | yes |" in table
    assert len(table.splitlines()) == PIN_COUNT + 2


def test_render_document_dry_run_says_it_is_not_empirical() -> None:
    plan = _plan()
    results = [
        pinmap_scan.MatchResult(
            logical_index=i,
            gpio=LOGICAL_TO_GPIO[i],
            analyzer_channel=None,
            expected_sample=plan.expected_rising_sample(i),
            observed_sample=None,
            delta_samples=None,
            confidence="none",
            note="dry run",
        )
        for i in range(PIN_COUNT)
    ]
    document = render_document(results, plan=plan, dry_run=True)

    assert "NOT EMPIRICALLY CONFIRMED" in document
    assert "no capture was run" in document.lower()
    assert "| 0 | GP2 | — | no |" in document
    assert "| Analyzer port | COM_TEST |" in document


def test_render_document_real_run_states_the_settings() -> None:
    plan = _plan()
    capture = _capture_with_steps(plan, mapped={i: i for i in range(PIN_COUNT)})
    document = render_document(
        match_indices(capture, plan), plan=plan, dry_run=False, extra_notes=["Capture: x.lac"]
    )

    assert "NOT EMPIRICALLY CONFIRMED" not in document
    assert "Measurement settings" in document
    assert "Capture: x.lac" in document
    assert "| 23 | GP28 | 24 | yes |" in document


# ------------------------------------------------------------- host adapter


def test_harness_adapter_reports_a_missing_package_clearly() -> None:
    """`host/` is unwritten, so the import must fail with a usable message."""
    with pytest.raises(pinmap_scan.PinmapError) as excinfo:
        pinmap_scan.HarnessAdapter.open("a_package_that_does_not_exist")
    assert "cannot import" in str(excinfo.value)
    assert "--dry-run" in str(excinfo.value)


def test_harness_adapter_names_the_missing_capability() -> None:
    class Bare:
        pass

    adapter = pinmap_scan.HarnessAdapter(handle=Bare(), package="fake")
    with pytest.raises(pinmap_scan.PinmapError) as excinfo:
        adapter.write_mask(mask=1, value=1)
    assert "write_mask" in str(excinfo.value)
    assert "fake" in str(excinfo.value)


def test_harness_adapter_calls_an_alternative_method_name() -> None:
    class Loose:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def set_mask(self, mask: int, value: int) -> None:
            self.calls.append(("set_mask", (mask, value)))

    handle = Loose()
    adapter = pinmap_scan.HarnessAdapter(handle=handle, package="fake")
    adapter.write_mask(mask=0xFF, value=0x0A)
    assert handle.calls == [("set_mask", (0xFF, 0x0A))]


def test_harness_adapter_falls_back_to_a_positional_configure() -> None:
    class Strict:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def configure(self, index: int, direction: str) -> None:
            self.calls.append((index, direction))

    handle = Strict()
    adapter = pinmap_scan.HarnessAdapter(handle=handle, package="fake")
    adapter.configure_output(7)
    assert handle.calls == [(7, "out")]


# -------------------------------------------------------------------- CLI


def test_cli_dry_run_prints_a_table_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    target = tmp_path / "pinmap.md"
    assert pinmap_scan.main(["--dry-run", "--pinmap", str(target)]) == 0

    out = capsys.readouterr()
    assert "| Logical index | GPIO | Analyzer channel |" in out.out
    assert "NOT EMPIRICALLY CONFIRMED" in out.out
    assert not target.exists(), "--dry-run must never write docs/pinmap.md"


def test_cli_dry_run_via_main_with_write_flag_still_writes_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--write is only honoured after a real capture, not in a dry run."""
    target = tmp_path / "pinmap.md"
    assert pinmap_scan.main(["--dry-run", "--write", "--pinmap", str(target)]) == 0
    capsys.readouterr()
    assert not target.exists()


def test_cli_requires_a_port_for_a_real_scan(capsys: pytest.CaptureFixture[str]) -> None:
    assert pinmap_scan.main([]) == 2
    assert "--port is required" in capsys.readouterr().err


def test_cli_rejects_a_trigger_it_cannot_drive(capsys: pytest.CaptureFixture[str]) -> None:
    assert pinmap_scan.main(["--dry-run", "--trigger", "complex:1:101"]) == 2
    assert "bad --trigger" in capsys.readouterr().err


def test_cli_warns_when_the_trigger_channel_is_not_index_zero(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert pinmap_scan.main(["--dry-run", "--trigger", "edge:4", "--pinmap", str(tmp_path / "p.md")]) == 0
    captured = capsys.readouterr()
    # The plan pulses logical index 0, so a trigger on another channel cannot fire.
    assert "will not fire" in captured.err

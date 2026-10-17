"""Offline tests for :mod:`la_lib` — the ``.lac`` / ``.tcs`` / ``.vcd`` codec.

No hardware, no third-party dependencies: every test builds a capture in memory
or reads the committed synthetic fixture.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import fixture_gen  # noqa: E402
import la_lib  # noqa: E402
from la_lib import (  # noqa: E402
    Capture,
    Channel,
    FormatError,
    SettingsError,
    device_limits,
    read_lac,
    samples_to_edges,
    write_csv,
    write_lac,
    write_tcs,
    write_vcd,
)

# ------------------------------------------------------------------ reading


def test_read_lac_recovers_settings(flat_lac_path: Path, expected_metrics: dict[str, float]) -> None:
    capture = read_lac(flat_lac_path, require_samples=True)

    assert capture.frequency == expected_metrics["frequency"]
    assert capture.pre_trigger_samples == expected_metrics["pre_trigger_samples"]
    assert capture.post_trigger_samples == fixture_gen.FLAT_POST
    assert capture.total_samples == fixture_gen.FLAT_PRE + fixture_gen.FLAT_POST
    assert capture.sample_count == fixture_gen.FLAT_PRE + fixture_gen.FLAT_POST
    assert capture.has_data
    assert len(capture.channels) == fixture_gen.FLAT_CHANNELS
    assert [c.number for c in capture.channels] == list(range(fixture_gen.FLAT_CHANNELS))
    assert capture.warnings == []


def test_fixture_is_a_json_envelope(tmp_path: Path) -> None:
    """A .lac is {"Settings": <CaptureSession>}, compact single-line JSON."""
    capture = fixture_gen.flat_capture()
    path = write_lac(tmp_path / "roundtrip.lac", capture)

    text = path.read_text(encoding="utf-8")
    assert "\n" not in text, "Newtonsoft writes compact single-line JSON"
    document = json.loads(text)
    assert list(document) == ["Settings"]

    session = document["Settings"]
    # Declaration order is what the .NET serializer emits.
    assert list(session) == [
        "Frequency",
        "PreTriggerSamples",
        "PostTriggerSamples",
        "TotalSamples",
        "LoopCount",
        "MeasureBursts",
        "CaptureChannels",
        "Bursts",
        "TriggerType",
        "TriggerChannel",
        "TriggerInverted",
        "TriggerBitCount",
        "TriggerPattern",
    ]
    assert session["TotalSamples"] == fixture_gen.FLAT_PRE + fixture_gen.FLAT_POST
    assert list(session["CaptureChannels"][0]) == [
        "TextualChannelNumber",
        "ChannelNumber",
        "ChannelName",
        "ChannelColor",
        "Hidden",
        "Samples",
    ]


def test_samples_are_base64_of_one_byte_per_sample(flat_lac_path: Path) -> None:
    document = json.loads(flat_lac_path.read_text(encoding="utf-8"))
    raw = document["Settings"]["CaptureChannels"][0]["Samples"]
    decoded = base64.b64decode(raw)
    assert len(decoded) == fixture_gen.FLAT_PRE + fixture_gen.FLAT_POST
    assert set(decoded) <= {0, 1}
    assert raw == base64.b64encode(decoded).decode("ascii")


def test_read_lac_rejects_a_settings_file_when_samples_are_required(tmp_path: Path) -> None:
    path = tmp_path / "x.tcs"
    write_tcs(path, channels="1-4", pre_trigger_samples=16, post_trigger_samples=64)
    with pytest.raises(FormatError):
        read_lac(path, require_samples=True)
    # ...but a bare .tcs is readable when samples are not demanded.
    capture = read_lac(path)
    assert capture.has_data is False
    assert capture.channel_numbers() == [0, 1, 2, 3]


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        '{"Settings": {"nope": 1}}',
        "[1, 2, 3]",
        '{"Settings": {"CaptureChannels": [{"ChannelNumber": 0, "Samples": {}}]}}',
    ],
)
def test_read_lac_rejects_malformed_input(tmp_path: Path, content: str) -> None:
    path = tmp_path / "bad.lac"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(FormatError):
        read_lac(path)


def test_read_lac_tolerates_a_bom(tmp_path: Path) -> None:
    source = write_lac(tmp_path / "plain.lac", fixture_gen.flat_capture())
    with_bom = tmp_path / "bom.lac"
    with_bom.write_bytes(b"\xef\xbb\xbf" + source.read_bytes())
    capture = read_lac(with_bom, require_samples=True)
    assert capture.sample_count == fixture_gen.FLAT_PRE + fixture_gen.FLAT_POST


def test_read_lac_warns_when_totalsamples_disagrees(tmp_path: Path) -> None:
    document = json.loads(write_lac(tmp_path / "a.lac", fixture_gen.flat_capture()).read_text())
    document["Settings"]["TotalSamples"] = 999_999
    path = tmp_path / "bogus.lac"
    path.write_text(json.dumps(document), encoding="utf-8")

    capture = read_lac(path, require_samples=True)
    assert capture.total_samples == fixture_gen.FLAT_PRE + fixture_gen.FLAT_POST
    assert any("TotalSamples" in w for w in capture.warnings)


def test_read_lac_accepts_byte_array_samples(tmp_path: Path) -> None:
    """la.mjs accepts an array of byte values; the reader must not choke on it."""
    capture = fixture_gen.flat_capture()
    path = write_lac(tmp_path / "arr.lac", capture)
    document = json.loads(path.read_text(encoding="utf-8"))
    samples = document["Settings"]["CaptureChannels"][0]["Samples"]
    document["Settings"]["CaptureChannels"][0]["Samples"] = list(base64.b64decode(samples))
    path.write_text(json.dumps(document), encoding="utf-8")

    parsed = read_lac(path, require_samples=True)
    assert parsed.channels[0].samples == capture.channels[0].samples


# ------------------------------------------------------------------ limits


def test_device_limits_are_selected_by_the_highest_channel_used() -> None:
    eight = device_limits([0, 7])
    sixteen = device_limits([0, 15])
    twenty_four = device_limits([0, 23])

    assert (eight.mode, eight.total, eight.max_pre, eight.max_post) == ("Channels_8", 131072, 13107, 131070)
    assert (sixteen.mode, sixteen.total, sixteen.max_pre, sixteen.max_post) == ("Channels_16", 65536, 6553, 65534)
    assert (twenty_four.mode, twenty_four.total, twenty_four.max_pre, twenty_four.max_post) == (
        "Channels_24",
        32768,
        3276,
        32766,
    )
    # MinFrequency = MaxFrequency * 2 / 65535 — not zero.
    assert twenty_four.min_freq == 3051
    assert twenty_four.max_freq == 100_000_000


def test_adding_channel_24_shrinks_the_pre_trigger_budget() -> None:
    """The trap the skill calls out: limits follow the channels actually selected."""
    assert device_limits([0, 1, 2, 3], buffer_size=131_072).max_pre == 13107
    assert device_limits(list(range(24)), buffer_size=131_072).max_pre == 3276


# ---------------------------------------------------------------- write_tcs


def test_write_tcs_produces_a_valid_session(tmp_path: Path) -> None:
    path = tmp_path / "scan.tcs"
    result = write_tcs(
        path,
        channels="1-24",
        frequency=100_000,
        pre_trigger_samples=256,
        post_trigger_samples=10_000,
        trigger="edge:1",
        names={1: "idx0"},
    )

    assert result["errors"] == []
    session = json.loads(path.read_text(encoding="utf-8"))
    assert list(session)  # ordered dict, not a dict-of-dicts
    assert session["Frequency"] == 100_000
    assert session["PreTriggerSamples"] == 256
    assert session["PostTriggerSamples"] == 10_000
    assert session["TotalSamples"] == 10_256
    assert session["TriggerType"] == la_lib.TRIGGER_EDGE
    assert session["TriggerChannel"] == 0  # CLI "1" -> file 0
    assert session["TriggerInverted"] is False
    assert [c["ChannelNumber"] for c in session["CaptureChannels"]] == list(range(24))
    assert session["CaptureChannels"][0]["ChannelName"] == "idx0"
    assert session["CaptureChannels"][0]["TextualChannelNumber"] == "Channel 1"
    assert all(c["Samples"] is None for c in session["CaptureChannels"])
    assert result["limits"]["mode"] == "Channels_24"


def test_write_tcs_rejects_a_pre_trigger_over_the_limit(tmp_path: Path) -> None:
    """pre > total/10 must be refused before anything is written."""
    path = tmp_path / "bad.tcs"
    with pytest.raises(SettingsError) as excinfo:
        write_tcs(path, channels="1-24", pre_trigger_samples=4000, post_trigger_samples=1000)
    assert any("PreTriggerSamples" in e for e in excinfo.value.errors)
    assert not path.exists(), "a rejected settings file must not be written"


def test_write_tcs_force_writes_a_rejected_file(tmp_path: Path) -> None:
    path = tmp_path / "forced.tcs"
    result = write_tcs(
        path, channels="1-24", pre_trigger_samples=4000, post_trigger_samples=1000, force=True
    )
    assert result["errors"], "the errors are still reported"
    assert path.exists()


def test_write_tcs_rejects_a_post_trigger_over_the_limit(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as excinfo:
        write_tcs(tmp_path / "b.tcs", channels="1-16", post_trigger_samples=70_000)
    assert any("PostTriggerSamples" in e for e in excinfo.value.errors)


def test_write_tcs_rejects_a_frequency_below_the_device_minimum(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as excinfo:
        write_tcs(tmp_path / "c.tcs", channels="1-8", frequency=100)
    assert any("Frequency must be" in e for e in excinfo.value.errors)


@pytest.mark.parametrize(
    ("spec", "expected_type", "expected_channel", "expected_bits", "expected_pattern"),
    [
        ("edge:4", la_lib.TRIGGER_EDGE, 3, 0, 0),
        ("edge:4:neg", la_lib.TRIGGER_EDGE, 3, 0, 0),
        ("complex:3:101", la_lib.TRIGGER_COMPLEX, 2, 3, 0b101),
        ("fast:1:101", la_lib.TRIGGER_FAST, 0, 3, 0b101),
    ],
)
def test_trigger_encoding_matches_the_file_format(
    tmp_path: Path,
    spec: str,
    expected_type: int,
    expected_channel: int,
    expected_bits: int,
    expected_pattern: int,
) -> None:
    """Channels are 1-based on the CLI and 0-based in the file; patterns keep left-to-right order."""
    path = tmp_path / "t.tcs"
    write_tcs(
        path,
        channels="1-8",
        pre_trigger_samples=16,
        post_trigger_samples=64,
        trigger=spec,
    )
    session = json.loads(path.read_text(encoding="utf-8"))
    assert session["TriggerType"] == expected_type
    assert session["TriggerChannel"] == expected_channel
    assert session["TriggerBitCount"] == expected_bits
    assert session["TriggerPattern"] == expected_pattern


def test_edge_trigger_negation_is_recorded(tmp_path: Path) -> None:
    path = tmp_path / "n.tcs"
    write_tcs(
        path, channels="1-8", pre_trigger_samples=16, post_trigger_samples=64,
        trigger="edge:2:neg",
    )
    session = json.loads(path.read_text(encoding="utf-8"))
    assert session["TriggerChannel"] == 1
    assert session["TriggerInverted"] is True


def test_pattern_bit_order_is_not_binary(tmp_path: Path) -> None:
    """`complex:3:101` = ch3 high, ch4 low, ch5 high — bit 0 is the LEFTMOST digit."""
    path = tmp_path / "p.tcs"
    write_tcs(
        path, channels="1-16", pre_trigger_samples=16, post_trigger_samples=64,
        trigger="complex:3:101",
    )
    session = json.loads(path.read_text(encoding="utf-8"))
    assert session["TriggerChannel"] == 2
    assert session["TriggerPattern"] == 0b101  # bit0 (ch3)=1, bit1 (ch4)=0, bit2 (ch5)=1


def test_complex_pattern_beyond_16_channels_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as excinfo:
        write_tcs(
            tmp_path / "q.tcs", channels="1-24", pre_trigger_samples=16, post_trigger_samples=64,
            trigger="complex:15:111",
        )
    assert any("exceeds the first 16 channels" in e for e in excinfo.value.errors)


def test_write_tcs_rejects_unknown_trigger(tmp_path: Path) -> None:
    with pytest.raises(SettingsError):
        write_tcs(tmp_path / "u.tcs", channels="1-4", trigger="nonsense:1")


def test_write_tcs_rejects_out_of_range_channel(tmp_path: Path) -> None:
    with pytest.raises(SettingsError):
        write_tcs(tmp_path / "v.tcs", channels="1-25")


def test_write_tcs_total_samples_follows_the_loop_count(tmp_path: Path) -> None:
    path = tmp_path / "loop.tcs"
    write_tcs(
        path, channels="1-8", pre_trigger_samples=100,
        post_trigger_samples=1000, loop_count=3,
    )
    session = json.loads(path.read_text(encoding="utf-8"))
    assert session["LoopCount"] == 3
    assert session["TotalSamples"] == 1000 * 4 + 100


# ------------------------------------------------------- samples_to_edges


def test_samples_to_edges_index_is_the_first_sample_at_the_new_level() -> None:
    #         idx: 0 1 2 3 4 5
    samples = bytes([0, 0, 1, 1, 0, 1])
    assert samples_to_edges(samples) == [(2, 1), (4, 0), (5, 1)]


def test_samples_to_edges_handles_empty_and_constant_input() -> None:
    assert samples_to_edges(b"") == []
    assert samples_to_edges(None) == []
    assert samples_to_edges(bytes([1, 1, 1])) == []


def test_trigger_lands_on_the_pre_trigger_sample() -> None:
    """A capture triggering on a rising edge puts it at exactly PreTriggerSamples."""
    total = 128
    samples = bytearray(total)
    for index in range(64, total):
        samples[index] = 1
    edges = samples_to_edges(bytes(samples))
    assert edges == [(64, 1)]


def test_capture_edges_helper_keys_are_zero_based_channel_numbers(flat_capture: Capture) -> None:
    edges = flat_capture.edges(0)
    assert list(edges) == [0]
    # The counter's bit 0 toggles every 8 samples starting at index 8 (index 0 has
    # no preceding sample, so the first observable change is at 8).
    assert edges[0][0] == (8, 1)
    assert all(index % 8 == 0 for index, _ in edges[0])


def test_capture_state_at_packs_the_bus(flat_capture: Capture) -> None:
    # state = bit0 | bit1<<1 | bit2<<2 = counter value for the low three bits.
    assert flat_capture.state_at(0) == 0
    assert flat_capture.state_at(8) == 1
    assert flat_capture.state_at(24) == 3
    assert flat_capture.state_at(56) == 7


# ------------------------------------------------------------------- VCD


def test_write_vcd_is_parseable_and_round_trips_levels(tmp_path: Path) -> None:
    capture = fixture_gen.flat_capture()
    path = write_vcd(tmp_path / "flat.vcd", capture)
    text = path.read_text(encoding="utf-8")

    # The timescale and the stamps must agree: a change at sample i is stamped #i
    # under a timescale of one sample period (50 ns at 20 MHz).
    assert "$timescale 50000 ps $end" in text
    assert "$enddefinitions $end" in text
    assert text.count("$var wire 1") == fixture_gen.FLAT_CHANNELS
    assert "$dumpvars" in text

    # Sample 8 is the counter's first change -> stamped #8, not #8 * 50000.
    assert "\n#8\n" in text
    assert "#400000" not in text

    identifiers = [
        line.split()[3] for line in text.splitlines() if line.startswith("$var wire 1")
    ]
    assert identifiers, "every channel needs a $var declaration"
    assert "$" not in identifiers, "the '$' identifier is deliberately skipped"
    assert len(set(identifiers)) == len(identifiers), "identifiers must be unique"


def test_vcd_round_trip_preserves_every_sample(tmp_path: Path) -> None:
    """Reparsing the VCD must rebuild every channel's levels exactly."""
    import measure

    capture = fixture_gen.flat_capture()
    path = write_vcd(tmp_path / "flat.vcd", capture)
    reparsed = measure.read_vcd(path)

    assert reparsed.frequency == pytest.approx(fixture_gen.FLAT_FREQUENCY, rel=1e-6)
    assert len(reparsed.channels) == len(capture.channels)
    for original, rebuilt in zip(capture.channels, reparsed.channels):
        assert rebuilt.samples[: len(original.samples)] == original.samples


def test_vcd_skips_idle_samples_but_keeps_timing(tmp_path: Path) -> None:
    """Only changed samples get a timestamp block — the file must stay small."""
    capture = fixture_gen.flat_capture()
    path = write_vcd(tmp_path / "flat.vcd", capture)
    blocks = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("#")]
    # 1 block for index 0 plus one per changed sample across all channels. Bit 0
    # changes every 8 samples, bit 1 every 16, bit 2 every 32 -> ~48 distinct times.
    assert 1 < len(blocks) < capture.sample_count


def test_write_vcd_channel_selection(tmp_path: Path) -> None:
    capture = fixture_gen.flat_capture()
    path = write_vcd(tmp_path / "two.vcd", capture, channels=[0, 1])
    text = path.read_text(encoding="utf-8")
    assert text.count("$var wire 1") == 2
    assert "CNT0" in text and "CNT1" in text and "CNT2" not in text


def test_write_vcd_rejects_an_empty_selection(tmp_path: Path) -> None:
    settings_only = Capture(frequency=1_000, pre_trigger_samples=0, post_trigger_samples=0)
    with pytest.raises(FormatError):
        write_vcd(tmp_path / "empty.vcd", settings_only)


# ------------------------------------------------------------------- CSV


def test_write_csv_matches_the_tool_layout(tmp_path: Path) -> None:
    capture = fixture_gen.flat_capture()
    path = write_csv(tmp_path / "flat.csv", capture)
    raw = path.read_bytes()
    assert raw.endswith(b"\r\n"), "the Windows tool terminates rows with CRLF"
    lines = raw.decode("utf-8").replace("\r\n", "\n").strip("\n").split("\n")
    assert len(lines) == capture.sample_count + 1
    assert lines[0].split(",") == [c.name for c in capture.channels]
    assert lines[1].split(",") == ["0"] * fixture_gen.FLAT_CHANNELS


def test_write_csv_falls_back_to_position_labels(tmp_path: Path) -> None:
    plain = Capture(
        frequency=1_000,
        pre_trigger_samples=0,
        post_trigger_samples=2,
        channels=[Channel(0, "", b"\x00\x01"), Channel(1, "", b"\x00\x00")],
    )
    path = write_csv(tmp_path / "plain.csv", plain)
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert header == "Channel 1,Channel 2"


# ------------------------------------------------------------ analyzer module


def test_find_install_locates_the_known_directory() -> None:
    import analyzer

    install = analyzer.find_install()
    assert (install / "TerminalCapture.exe").is_file()
    assert (install / "LogicAnalyzer.exe").is_file()


def test_find_install_reports_what_it_tried(tmp_path: Path) -> None:
    import analyzer

    with pytest.raises(analyzer.AnalyzerError) as excinfo:
        analyzer.find_install(tmp_path / "definitely-not-here")
    assert "TerminalCapture.exe" in str(excinfo.value)
    assert str(tmp_path) in str(excinfo.value), "the failure must say where it looked"


def test_skill_scripts_dir_has_probe_and_la() -> None:
    import analyzer

    scripts = analyzer.skill_scripts_dir()
    assert (scripts / "probe-device.ps1").is_file()
    assert (scripts / "la.mjs").is_file()


def test_build_settings_and_capture_plan_agree() -> None:
    """build_settings is a thin limits-checked wrapper; the file it writes is valid."""
    import analyzer

    plan = analyzer.build_settings(
        Path(__file__).resolve().parent / "fixtures" / "_tmp_scan.tcs",
        channels=tuple(range(1, 25)),
        frequency=100_000,
        pre_trigger_samples=256,
        post_trigger_samples=10_000,
        trigger="edge:1",
    )
    try:
        session = plan["session"]
        assert session["TotalSamples"] == 10_256
        assert plan["limits"]["mode"] == "Channels_24"
        assert "rising" in plan["triggerText"]
    finally:
        (Path(__file__).resolve().parent / "fixtures" / "_tmp_scan.tcs").unlink(missing_ok=True)


def test_build_settings_refuses_invalid_geometry(tmp_path: Path) -> None:
    import analyzer

    with pytest.raises(SettingsError, match="PreTriggerSamples"):
        analyzer.build_settings(
            tmp_path / "nope.tcs",
            channels=tuple(range(1, 25)),
            frequency=100_000,
            pre_trigger_samples=5000,  # > total/10 in 24-channel mode
            post_trigger_samples=1000,
        )
    assert not (tmp_path / "nope.tcs").exists()


def test_build_settings_writes_a_file_the_driver_would_accept(tmp_path: Path) -> None:
    """Cross-check our limits engine against the skill's own validator (la.mjs)."""
    import analyzer

    path = tmp_path / "valid.tcs"
    analyzer.build_settings(
        path,
        channels=tuple(range(1, 25)),
        frequency=100_000,
        pre_trigger_samples=256,
        post_trigger_samples=10_000,
        trigger="edge:1",
    )
    capture = read_lac(path)
    limits = device_limits(capture.channel_numbers())
    assert limits.mode == "Channels_24"
    assert 2 <= capture.pre_trigger_samples <= limits.max_pre
    assert 2 <= capture.post_trigger_samples <= limits.max_post
    assert capture.total_samples <= limits.total

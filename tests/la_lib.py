"""la_lib — pure-stdlib reader/writer for gusmanb LogicAnalyzer capture files.

Covers the three formats this bench needs, with **no third-party dependencies**:

* ``.lac``  — a capture. Plain JSON envelope ``{"Settings": <CaptureSession>}``.
* ``.tcs``  — capture settings. Plain JSON, a bare ``CaptureSession``.
* ``.vcd``  — Value Change Dump, written here so a capture can be opened in
  PulseView / GTKWave. (The GUI's own sigrok decoders are broken on this machine:
  its bundled pythonnet ``Python.Runtime`` tops out at ABI 3.13 and only Python
  3.14 is installed, so nothing we export can be *decoded* by ``LogicAnalyzer.exe``.
  Exporting VCD and decoding elsewhere is the supported route — see tests/README.md.)

Field names and encodings below were reverse-engineered from the ``logic-analyzer``
skill's ``reference.md`` and its ``la.mjs`` reference implementation, which itself
was read out of the gusmanb/logicanalyzer sources. They are not guesses; see
``docs/FORMAT-NOTES`` in this file's README section for the exact table.

File layout facts that matter (all verified against ``la.mjs``):

* Newtonsoft defaults: **compact single-line JSON**, no indentation, no BOM.
* ``CaptureSession`` serialises in declaration order: Frequency,
  PreTriggerSamples, PostTriggerSamples, TotalSamples, LoopCount, MeasureBursts,
  CaptureChannels, Bursts, TriggerType, TriggerChannel, TriggerInverted,
  TriggerBitCount, TriggerPattern.
* ``TotalSamples`` is a computed getter: ``post * (loop + 1) + pre``. The tool
  serialises it but ignores it on load, so we always recompute and warn on drift.
* ``AnalyzerChannel.Samples`` is base64 of **one byte per sample**, values 0/1.
* Channels are 0-based in the file and 1-based on every CLI surface.
"""

from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "TRIGGER_EDGE",
    "TRIGGER_COMPLEX",
    "TRIGGER_FAST",
    "TRIGGER_BLAST",
    "TRIGGER_NAMES",
    "FormatError",
    "SettingsError",
    "DeviceLimits",
    "Channel",
    "Capture",
    "read_lac",
    "read_settings",
    "write_tcs",
    "write_lac",
    "samples_to_edges",
    "write_vcd",
    "write_csv",
    "device_limits",
    "validate_settings",
]

# --------------------------------------------------------------------- constants

TRIGGER_EDGE = 0
TRIGGER_COMPLEX = 1
TRIGGER_FAST = 2
TRIGGER_BLAST = 3
TRIGGER_NAMES = {0: "Edge", 1: "Complex", 2: "Fast", 3: "Blast"}

# Verified against the live V6 board via probe-device.ps1 (skill reference.md):
# BufferSize 131072, MaxFrequency 100 MHz, BlastFrequency 200 MHz, 24 channels.
DEFAULT_MAX_FREQUENCY = 100_000_000
DEFAULT_BUFFER_SIZE = 131_072
DEFAULT_CHANNEL_COUNT = 24
DEFAULT_BLAST_FREQUENCY = 200_000_000

#: Declaration order the .NET serializer uses for CaptureSession.
_SESSION_KEY_ORDER = (
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
)

#: Declaration order for AnalyzerChannel.
_CHANNEL_KEY_ORDER = (
    "TextualChannelNumber",
    "ChannelNumber",
    "ChannelName",
    "ChannelColor",
    "Hidden",
    "Samples",
)

#: Printable ASCII VCD identifiers, `$` dropped ($ is legal per IEEE 1364 but some
#: readers treat it as a keyword introducer). 93 identifiers cover 24 channels.
_VCD_IDS = [chr(c) for c in range(33, 127) if chr(c) != "$"]


class FormatError(ValueError):
    """The file is not a LogicAnalyzer capture/settings file."""


class SettingsError(ValueError):
    """The settings would be rejected by the analyzer driver.

    ``errors`` carries every rule violation found, so a caller can print them all.
    """

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# ------------------------------------------------------------------- dataclasses


@dataclass
class DeviceLimits:
    """The capture limits the driver enforces, for one channel-count mode.

    ``mode``        8ch / 16ch / 24ch, chosen by the *highest channel used*.
    ``total``       ``floor(BufferSize / divisor)`` — the whole usable buffer.
    ``max_pre``     ``floor(total / 10)``.
    ``max_post``    ``total - 2``.
    ``min_freq``    ``floor(MaxFrequency * 2 / 65535)`` — 3051 Hz at 100 MHz.
    """

    mode: str
    divisor: int
    total: int
    max_pre: int
    max_post: int
    min_freq: int
    max_freq: int
    channel_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "maxTotalSamples": self.total,
            "maxPreSamples": self.max_pre,
            "maxPostSamples": self.max_post,
            "minFrequency": self.min_freq,
            "maxFrequency": self.max_freq,
            "channelCount": self.channel_count,
        }


@dataclass
class Channel:
    """One captured channel: a 0-based number plus its per-sample levels."""

    number: int
    name: str = ""
    samples: bytes | None = None
    color: int | None = None
    hidden: bool = False

    @property
    def label(self) -> str:
        """Human label: the name, else ``Channel <number+1>``."""
        return self.name.strip() or f"Channel {self.number + 1}"

    @property
    def position_label(self) -> str:
        """The CSV header form: the name, else the *array position* label."""
        return self.name.strip() or f"Channel {self.number + 1}"


@dataclass
class Capture:
    """A ``.lac`` capture (or a settings-only session, when ``has_data`` is false)."""

    frequency: int
    pre_trigger_samples: int
    post_trigger_samples: int
    channels: list[Channel] = field(default_factory=list)
    loop_count: int = 0
    measure_bursts: bool = False
    bursts: Any = None
    trigger_type: int = TRIGGER_EDGE
    trigger_channel: int = 0
    trigger_inverted: bool = False
    trigger_bit_count: int = 0
    trigger_pattern: int = 0
    total_samples_declared: int | None = None
    source: str | None = None
    warnings: list[str] = field(default_factory=list)

    # -- computed ---------------------------------------------------------

    @property
    def total_samples(self) -> int:
        """``post * (loop + 1) + pre`` — the computed ``TotalSamples`` getter."""
        return self.post_trigger_samples * (self.loop_count + 1) + self.pre_trigger_samples

    @property
    def sample_count(self) -> int:
        """Longest channel sample array — what ``la.mjs`` reports as ``samples``."""
        return max((len(c.samples) for c in self.channels if c.samples), default=0)

    @property
    def has_data(self) -> bool:
        return any(c.samples for c in self.channels)

    @property
    def sample_period_s(self) -> float:
        return 1.0 / self.frequency if self.frequency else 0.0

    @property
    def trigger_name(self) -> str:
        return TRIGGER_NAMES.get(self.trigger_type, f"Unknown({self.trigger_type})")

    @property
    def trigger_text(self) -> str:
        if self.trigger_type in (TRIGGER_EDGE, TRIGGER_BLAST):
            edge = "falling" if self.trigger_inverted else "rising"
            return f"{self.trigger_name} on Channel {self.trigger_channel + 1}, {edge}"
        pattern = "".join(
            "1" if (self.trigger_pattern >> b) & 1 else "0" for b in range(self.trigger_bit_count)
        )
        return (
            f"{self.trigger_name} on Channel {self.trigger_channel + 1}, "
            f"pattern {pattern} (leftmost digit = Channel {self.trigger_channel + 1})"
        )

    def data_channels(self) -> list[Channel]:
        """Channels that actually carry samples, in file order."""
        return [c for c in self.channels if c.samples]

    def channel_numbers(self) -> list[int]:
        return [c.number for c in self.channels]

    def levels_at(self, index: int) -> list[int]:
        """Level of every data channel at one sample index."""
        return [c.samples[index] for c in self.data_channels()]

    def state_at(self, index: int) -> int:
        """The data channels' levels as one integer (channel order, bit 0 = first).

        This turns the bench's parallel bus into a single observable "state".
        """
        value = 0
        for bit, ch in enumerate(self.data_channels()):
            if ch.samples[index]:
                value |= 1 << bit
        return value

    def edges(self, channel: int | None = None) -> dict[int, list[tuple[int, int]]]:
        """Per-channel ``(sample_index, new_level)`` lists.

        ``channel`` is a **0-based** channel number; ``None`` means every data
        channel. The index is that of the *first sample holding the new level*,
        matching ``la.mjs``'s ``transitionsOf`` (it reports ``@index from->to``).
        """
        out: dict[int, list[tuple[int, int]]] = {}
        for ch in self.data_channels():
            if channel is not None and ch.number != channel:
                continue
            out[ch.number] = samples_to_edges(ch.samples)
        return out


# ------------------------------------------------------------------- decoding


def _decode_samples(value: Any, *, context: str) -> bytes | None:
    """``Samples`` is base64 of one byte per sample; tolerate a byte array too."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return b""
        try:
            return base64.b64decode(text, validate=False)
        except Exception as exc:  # noqa: BLE001 - surfaced as FormatError
            raise FormatError(f"{context}: Samples is not valid base64 ({exc})") from exc
    if isinstance(value, (list, tuple)):
        # la.mjs accepts this shape; the .NET serializer never emits it.
        try:
            return bytes(int(v) & 0xFF for v in value)
        except (TypeError, ValueError) as exc:
            raise FormatError(f"{context}: Samples array is not byte-valued ({exc})") from exc
    raise FormatError(f"{context}: unsupported Samples encoding {type(value).__name__}")


def read_lac(path: str | Path, *, require_samples: bool = False) -> Capture:
    """Parse a ``.lac`` capture (or, tolerantly, a bare ``.tcs`` settings object).

    The ``.lac`` envelope is ``{"Settings": {...}}``; a ``.tcs`` is the inner
    object with ``Samples`` left null. Both are accepted, mirroring ``la.mjs``'s
    ``loadSession``.

    Raises :class:`FormatError` when the JSON is malformed or carries no
    ``CaptureChannels``. With ``require_samples=True`` a settings-only file is an
    error instead of a Capture with ``has_data == False``.
    """
    path = Path(path)
    raw = path.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":  # the .NET writer does not emit a BOM; be tolerant
        raw = raw[3:]
    try:
        document = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise FormatError(f"{path}: not UTF-8 text — not a LogicAnalyzer file ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise FormatError(f"{path}: not valid JSON ({exc})") from exc

    if isinstance(document, dict) and isinstance(document.get("Settings"), dict):
        session, warnings_extra = document["Settings"], []
    elif isinstance(document, dict):
        session, warnings_extra = document, ["no Settings envelope — read as a bare .tcs"]
    else:
        raise FormatError(f"{path}: top level is {type(document).__name__}, expected an object")

    channels_raw = session.get("CaptureChannels")
    if not isinstance(channels_raw, list):
        raise FormatError(f"{path}: no CaptureChannels — not a LogicAnalyzer capture or settings file")

    warnings = list(warnings_extra)
    channels: list[Channel] = []
    for index, entry in enumerate(channels_raw):
        if not isinstance(entry, dict):
            raise FormatError(f"{path}: CaptureChannels[{index}] is not an object")
        number = entry.get("ChannelNumber")
        if not isinstance(number, int) or isinstance(number, bool):
            warnings.append(f"CaptureChannels[{index}] has no integer ChannelNumber; using position {index}")
            number = index
        raw_name = entry.get("ChannelName")
        if raw_name is not None and not isinstance(raw_name, str):
            warnings.append(f"ChannelNumber {number}: non-string ChannelName ignored")
            raw_name = ""
        samples = _decode_samples(entry.get("Samples"), context=f"{path}: channel {number}")
        color = entry.get("ChannelColor")
        channels.append(
            Channel(
                number=number,
                name=raw_name or "",
                samples=samples,
                color=color if isinstance(color, int) else None,
                hidden=bool(entry.get("Hidden", False)),
            )
        )

    if require_samples and not any(c.samples for c in channels):
        raise FormatError(f"{path}: no sample data — this is a settings file, not a capture")

    try:
        frequency = int(session.get("Frequency") or 0)
    except (TypeError, ValueError):
        frequency = 0
        warnings.append("Frequency is not an integer; treated as 0")
    try:
        pre = int(session.get("PreTriggerSamples") or 0)
        post = int(session.get("PostTriggerSamples") or 0)
        loop = int(session.get("LoopCount") or 0)
    except (TypeError, ValueError) as exc:
        raise FormatError(f"{path}: pre/post/loop counts are not integers ({exc})") from exc

    declared = session.get("TotalSamples")
    try:
        declared_int = int(declared) if declared is not None else None
    except (TypeError, ValueError):
        declared_int = None

    capture = Capture(
        frequency=frequency,
        pre_trigger_samples=pre,
        post_trigger_samples=post,
        channels=channels,
        loop_count=loop,
        measure_bursts=bool(session.get("MeasureBursts", False)),
        bursts=session.get("Bursts"),
        trigger_type=int(session.get("TriggerType") or 0),
        trigger_channel=int(session.get("TriggerChannel") or 0),
        trigger_inverted=bool(session.get("TriggerInverted", False)),
        trigger_bit_count=int(session.get("TriggerBitCount") or 0),
        trigger_pattern=int(session.get("TriggerPattern") or 0),
        total_samples_declared=declared_int,
        source=str(path.resolve()),
        warnings=warnings,
    )

    if declared_int is not None and declared_int != capture.total_samples:
        capture.warnings.append(
            f"TotalSamples in the file ({declared_int}) does not match "
            f"post*(loop+1)+pre ({capture.total_samples}); the driver recomputes and ignores it"
        )

    lengths = {len(c.samples) for c in capture.data_channels()}
    if len(lengths) > 1:
        capture.warnings.append(
            f"channels disagree on sample count {sorted(lengths)}; using the longest"
        )
    if capture.has_data and capture.sample_count != capture.total_samples:
        capture.warnings.append(
            f"captured {capture.sample_count} samples but settings request "
            f"{capture.total_samples}; the capture may have been truncated"
        )
    return capture


# Backwards-friendly alias: the tolerance for bare .tcs lives in read_lac.
read_settings = read_lac


# ------------------------------------------------------------------- limits


def device_limits(
    used_channels: Iterable[int],
    *,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    max_frequency: int = DEFAULT_MAX_FREQUENCY,
    channel_count: int = DEFAULT_CHANNEL_COUNT,
) -> DeviceLimits:
    """The driver's limits for the mode implied by the highest channel used.

    This is the trap worth repeating: limits depend on the channels actually
    selected. Adding channel 17+ drops the buffer from 131072 to 32768 samples,
    so a comfortable 8-channel pre-trigger can exceed ``max_pre`` in 24-channel
    mode.
    """
    numbers = list(used_channels)
    highest = max(numbers) if numbers else 0
    if highest < 8:
        mode, divisor = "Channels_8", 1
    elif highest < 16:
        mode, divisor = "Channels_16", 2
    else:
        mode, divisor = "Channels_24", 4
    total = math.floor(buffer_size / divisor)
    return DeviceLimits(
        mode=mode,
        divisor=divisor,
        total=total,
        max_pre=math.floor(total / 10),
        max_post=total - 2,
        min_freq=math.floor(max_frequency * 2 / 65535),
        max_freq=max_frequency,
        channel_count=channel_count,
    )


def validate_settings(
    session: dict[str, Any],
    *,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    max_frequency: int = DEFAULT_MAX_FREQUENCY,
    channel_count: int = DEFAULT_CHANNEL_COUNT,
    blast_frequency: int | None = DEFAULT_BLAST_FREQUENCY,
) -> tuple[DeviceLimits, list[str], list[str]]:
    """Mirror ``LogicAnalyzerDriver.ValidateSettings`` + ``GetLimits``.

    Returns ``(limits, errors, warnings)``. This is the same rule set ``la.mjs
    validate`` implements, so the two tools agree on what a device would refuse.
    """
    errors: list[str] = []
    warnings: list[str] = []

    channels_raw = session.get("CaptureChannels") or []
    numbers = []
    for entry in channels_raw:
        if isinstance(entry, dict) and isinstance(entry.get("ChannelNumber"), int):
            numbers.append(entry["ChannelNumber"])
        else:
            numbers.append(len(numbers))

    limits = device_limits(
        numbers, buffer_size=buffer_size, max_frequency=max_frequency, channel_count=channel_count
    )

    pre = int(session.get("PreTriggerSamples") or 0)
    post = int(session.get("PostTriggerSamples") or 0)
    loop = int(session.get("LoopCount") or 0)
    freq = int(session.get("Frequency") or 0)
    ttype = int(session.get("TriggerType") or 0)

    requested = post * (loop + 1) + pre

    if not numbers:
        errors.append("no capture channels selected")
    if any(n < 0 or n > channel_count - 1 for n in numbers):
        errors.append(f"channel numbers must be 0..{channel_count - 1} (got {numbers})")

    if ttype in (TRIGGER_EDGE, TRIGGER_BLAST):
        tc = int(session.get("TriggerChannel") or 0)
        if not 0 <= tc <= channel_count:
            errors.append(
                f"TriggerChannel must be 0..{channel_count} ({channel_count} = external trigger), got {tc}"
            )
    else:
        bits = int(session.get("TriggerBitCount") or 0)
        tc = int(session.get("TriggerChannel") or 0)
        max_bits = 16 if ttype == TRIGGER_COMPLEX else 5
        span = 16 if ttype == TRIGGER_COMPLEX else 5
        if not 1 <= bits <= max_bits:
            errors.append(f"TriggerBitCount must be 1..{max_bits}, got {bits}")
        if not 0 <= tc <= 15:
            errors.append(f"pattern TriggerChannel must be 0..15, got {tc}")
        if tc + bits > span:
            errors.append(f"pattern of {bits} bit(s) starting at channel {tc} exceeds the first {span} channels")

    if ttype == TRIGGER_BLAST:
        if pre != 0:
            errors.append(f"blast mode requires PreTriggerSamples = 0 (got {pre})")
        if loop != 0:
            errors.append(f"blast mode requires LoopCount = 0 (got {loop})")
        if blast_frequency is not None and freq != blast_frequency:
            errors.append(f"blast mode requires Frequency = {blast_frequency} Hz (got {freq})")
    else:
        if not 2 <= pre <= limits.max_pre:
            errors.append(
                f"PreTriggerSamples must be 2..{limits.max_pre} in {limits.mode} mode (got {pre})"
            )
        if not 2 <= post <= limits.max_post:
            errors.append(
                f"PostTriggerSamples must be 2..{limits.max_post} in {limits.mode} mode (got {post})"
            )
        if requested > limits.total:
            errors.append(
                f"requested samples {requested} exceed the {limits.total} available in {limits.mode} mode"
            )
        if not 0 <= loop <= 254:
            errors.append(f"LoopCount must be 0..254, got {loop}")
        if not limits.min_freq <= freq <= limits.max_freq:
            errors.append(f"Frequency must be {limits.min_freq}..{limits.max_freq} Hz (got {freq})")

    declared = session.get("TotalSamples")
    if declared and int(declared) != requested:
        warnings.append(
            f"TotalSamples ({declared}) does not match post*(loop+1)+pre ({requested}); "
            "the field is recomputed on load and ignored"
        )
    return limits, errors, warnings


# ------------------------------------------------------------------- encoding


def _ordered_session(
    *,
    frequency: int,
    pre: int,
    post: int,
    loop: int,
    channels: list[dict[str, Any]],
    trigger_type: int,
    trigger_channel: int,
    trigger_inverted: bool,
    trigger_bit_count: int,
    trigger_pattern: int,
    measure_bursts: bool,
    bursts: Any = None,
) -> dict[str, Any]:
    """Build a CaptureSession dict in the .NET serializer's declaration order."""
    total = post * (loop + 1) + pre
    values = {
        "Frequency": frequency,
        "PreTriggerSamples": pre,
        "PostTriggerSamples": post,
        "TotalSamples": total,
        "LoopCount": loop,
        "MeasureBursts": measure_bursts,
        "CaptureChannels": channels,
        "Bursts": bursts,
        "TriggerType": trigger_type,
        "TriggerChannel": trigger_channel,
        "TriggerInverted": trigger_inverted,
        "TriggerBitCount": trigger_bit_count,
        "TriggerPattern": trigger_pattern,
    }
    return {key: values[key] for key in _SESSION_KEY_ORDER}


def _channel_dict(
    number: int, name: str = "", *, samples: bytes | None = None, hidden: bool = False
) -> dict[str, Any]:
    values = {
        "TextualChannelNumber": f"Channel {number + 1}",
        "ChannelNumber": number,
        "ChannelName": name,
        "ChannelColor": None,
        "Hidden": hidden,
        "Samples": None if samples is None else base64.b64encode(samples).decode("ascii"),
    }
    return {key: values[key] for key in _CHANNEL_KEY_ORDER}


def _write_json(path: str | Path, document: Any) -> Path:
    """Write compact single-line UTF-8 JSON, the way Newtonsoft does."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return path


# ------------------------------------------------------------------- .tcs writer


def write_tcs(
    path: str | Path,
    *,
    channels: Sequence[int] | int = tuple(range(1, 25)),
    frequency: int = 20_000_000,
    pre_trigger_samples: int = 512,
    post_trigger_samples: int = 4000,
    loop_count: int = 0,
    names: dict[int, str] | None = None,
    trigger: str = "edge:1",
    measure_bursts: bool = False,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    max_frequency: int = DEFAULT_MAX_FREQUENCY,
    channel_count: int = DEFAULT_CHANNEL_COUNT,
    blast_frequency: int = DEFAULT_BLAST_FREQUENCY,
    force: bool = False,
) -> dict[str, Any]:
    """Generate a ``.tcs`` settings file, enforcing the driver's own limits.

    ``channels`` are **1-based** (the CLI/GUI convention); they are stored 0-based.
    ``trigger`` uses the CLI specs ``edge:<ch>[:neg]``, ``blast:<ch>[:neg]``,
    ``fast:<ch>:<pattern>``, ``complex:<ch>:<pattern>`` — again 1-based.

    The driver's rules are checked **before anything is written**, so this cannot
    hand the device a file it would reject: mode by highest channel used
    (``max pre = total/10``, ``max post = total-2``), then pre/post/loop/frequency
    bounds and the pattern bit-count limits. Pass ``force=True`` to write a file
    that fails those checks anyway (for deliberately inspecting a bad file).

    Returns the session dict that was serialised.
    """
    channel_list = _parse_channels(channels, channel_count)
    trigger_spec = _parse_trigger(trigger)

    session = {
        "Frequency": int(frequency),
        "PreTriggerSamples": int(pre_trigger_samples),
        "PostTriggerSamples": int(post_trigger_samples),
        "TotalSamples": int(post_trigger_samples) * (int(loop_count) + 1) + int(pre_trigger_samples),
        "LoopCount": int(loop_count),
        "MeasureBursts": bool(measure_bursts),
        "CaptureChannels": [
            {
                "ChannelNumber": n - 1,
                "ChannelName": (names or {}).get(n, ""),
            }
            for n in channel_list
        ],
        "Bursts": None,
        "TriggerType": trigger_spec["type"],
        "TriggerChannel": trigger_spec["channel"],
        "TriggerInverted": trigger_spec["inverted"],
        "TriggerBitCount": trigger_spec["bitCount"],
        "TriggerPattern": trigger_spec["pattern"],
    }

    limits, errors, warnings = validate_settings(
        session,
        buffer_size=buffer_size,
        max_frequency=max_frequency,
        channel_count=channel_count,
        blast_frequency=blast_frequency,
    )
    del warnings  # regenerated below against the final ordered document

    ordered = _ordered_session(
        frequency=session["Frequency"],
        pre=session["PreTriggerSamples"],
        post=session["PostTriggerSamples"],
        loop=session["LoopCount"],
        channels=[
            _channel_dict(c["ChannelNumber"], c["ChannelName"]) for c in session["CaptureChannels"]
        ],
        trigger_type=session["TriggerType"],
        trigger_channel=session["TriggerChannel"],
        trigger_inverted=session["TriggerInverted"],
        trigger_bit_count=session["TriggerBitCount"],
        trigger_pattern=session["TriggerPattern"],
        measure_bursts=session["MeasureBursts"],
    )

    if errors and not force:
        raise SettingsError(errors)

    _write_json(path, ordered)
    return {
        "session": ordered,
        "limits": limits.as_dict(),
        "requestedSamples": ordered["TotalSamples"],
        "errors": errors,
        "triggerText": _trigger_text(trigger_spec),
    }


def write_lac(path: str | Path, capture: Capture) -> Path:
    """Serialise a :class:`Capture` back to a ``.lac`` envelope.

    Used to write the synthetic test fixture; real captures come from
    ``TerminalCapture.exe`` and are only ever read.
    """
    channels = [
        _channel_dict(ch.number, ch.name, samples=ch.samples, hidden=ch.hidden)
        for ch in capture.channels
    ]
    ordered = _ordered_session(
        frequency=capture.frequency,
        pre=capture.pre_trigger_samples,
        post=capture.post_trigger_samples,
        loop=capture.loop_count,
        channels=channels,
        trigger_type=capture.trigger_type,
        trigger_channel=capture.trigger_channel,
        trigger_inverted=capture.trigger_inverted,
        trigger_bit_count=capture.trigger_bit_count,
        trigger_pattern=capture.trigger_pattern,
        measure_bursts=capture.measure_bursts,
        bursts=capture.bursts,
    )
    return _write_json(path, {"Settings": ordered})


# ------------------------------------------------------------------- triggers


def _parse_channels(spec: Sequence[int] | int | str, channel_count: int) -> list[int]:
    """Accept ``"1-24"``, ``"1,2,3"``, ``[1, 2, 3]`` or a bare int — 1-based."""
    if isinstance(spec, int):
        numbers = [spec]
    elif isinstance(spec, str):
        numbers = []
        for token in spec.split(","):
            token = token.strip()
            if not token:
                continue
            match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
            if match:
                start, end = int(match.group(1)), int(match.group(2))
                if start > end:
                    raise SettingsError([f"bad channel range {token!r}"])
                numbers.extend(range(start, end + 1))
            elif token.isdigit():
                numbers.append(int(token))
            else:
                raise SettingsError([f"bad channel spec {token!r}"])
    else:
        numbers = [int(n) for n in spec]

    numbers = sorted(set(numbers))
    if not numbers:
        raise SettingsError(["no capture channels selected"])
    bad = [n for n in numbers if not 1 <= n <= channel_count]
    if bad:
        raise SettingsError([f"channel(s) {bad} outside 1..{channel_count}"])
    return numbers


def _parse_trigger(spec: str) -> dict[str, Any]:
    """``edge:4`` / ``blast:4:neg`` / ``fast:1:101`` / ``complex:3:101`` -> file fields."""
    parts = str(spec).split(":")
    kind = (parts[0] or "").lower()

    def channel(token: str | None) -> int:
        if token is None or not token.strip().lstrip("-").isdigit():
            raise SettingsError([f"trigger channel must be a number >= 1 in {spec!r}"])
        value = int(token)
        if value < 1:
            raise SettingsError([f"trigger channel must be >= 1 in {spec!r}"])
        return value - 1

    if kind == "edge":
        return {"type": TRIGGER_EDGE, "channel": channel(parts[1] if len(parts) > 1 else None),
                "inverted": len(parts) > 2 and parts[2].lower().startswith("neg"),
                "bitCount": 0, "pattern": 0, "text": None}
    if kind == "blast":
        return {"type": TRIGGER_BLAST, "channel": channel(parts[1] if len(parts) > 1 else None),
                "inverted": len(parts) > 2 and parts[2].lower().startswith("neg"),
                "bitCount": 0, "pattern": 0, "text": None}
    if kind in ("fast", "complex"):
        pattern = parts[2] if len(parts) > 2 else ""
        if not re.fullmatch(r"[01]{1,16}", pattern or ""):
            raise SettingsError([f"pattern must be 1-16 characters of 0/1 in {spec!r}"])
        # Index 0 is the LEFTMOST character and maps to the trigger channel itself;
        # reading it as a binary number is the classic off-by-reversal bug.
        value = 0
        for bit, digit in enumerate(pattern):
            if digit == "1":
                value |= 1 << bit
        return {
            "type": TRIGGER_FAST if kind == "fast" else TRIGGER_COMPLEX,
            "channel": channel(parts[1] if len(parts) > 1 else None),
            "inverted": False,
            "bitCount": len(pattern),
            "pattern": value,
            "text": pattern,
        }
    raise SettingsError([f"unknown trigger type {parts[0]!r} (use edge/blast/fast/complex)"])


def _trigger_text(spec: dict[str, Any]) -> str:
    name = TRIGGER_NAMES[spec["type"]]
    if spec["type"] in (TRIGGER_EDGE, TRIGGER_BLAST):
        edge = "falling" if spec["inverted"] else "rising"
        return f"{name} on Channel {spec['channel'] + 1}, {edge}"
    return (
        f"{name} on Channel {spec['channel'] + 1}, pattern {spec['text']} "
        f"(leftmost digit = Channel {spec['channel'] + 1})"
    )


# --------------------------------------------------------------- sample helpers


def samples_to_edges(samples: bytes | bytearray | Sequence[int] | None) -> list[tuple[int, int]]:
    """Per-channel ``(sample_index, new_level)`` list of level changes.

    The index is that of the first sample holding the new level (so a capture
    triggering on a rising edge at index ``PreTriggerSamples`` reports exactly
    that index, as verified on the live capture).
    """
    if not samples:
        return []
    edges: list[tuple[int, int]] = []
    previous = samples[0]
    for index in range(1, len(samples)):
        current = samples[index]
        if current != previous:
            edges.append((index, int(current)))
            previous = current
    return edges


def runs_of(samples: bytes | bytearray | Sequence[int] | None) -> list[tuple[int, int, int]]:
    """Runs of equal levels as ``(level, from_index, to_index)``, inclusive."""
    if not samples:
        return []
    runs: list[tuple[int, int, int]] = []
    start = 0
    for index in range(1, len(samples) + 1):
        if index == len(samples) or samples[index] != samples[start]:
            runs.append((int(samples[start]), start, index - 1))
            start = index
    return runs


# --------------------------------------------------------------------- exports


def write_vcd(path: str | Path, capture: Capture, *, channels: Sequence[int] | None = None) -> Path:
    """Export a capture as VCD for PulseView / GTKWave.

    **Timescale.** The header declares ``$timescale <period_ps> ps $end`` where
    ``period_ps = round(1e12 / Frequency)``, and a change at sample *i* is stamped
    ``#i``. Stamping ``#i`` is an exact multiple of the timescale, so readers
    recover sample times directly; emitting ``#i * period_ps`` under a nominal
    ``1ps`` timescale would be the same numbers but would make a reader that
    trusts the timescale (as :func:`measure.read_vcd` does) infer a sample rate
    that is off by the period. The two must agree — that is the invariant.

    Identifier codes are printable ASCII from ``!`` upward with ``$`` skipped,
    matching the reference implementation.

    ``channels`` selects **0-based** channel numbers; ``None`` exports all
    channels that carry data.

    Note this is an export only — this machine's ``LogicAnalyzer.exe`` cannot
    decode anything, because its pythonnet runtime stops at Python ABI 3.13 and
    only 3.14 is installed.
    """
    path = Path(path)
    selected = capture.data_channels()
    if channels is not None:
        wanted = set(channels)
        selected = [c for c in selected if c.number in wanted]
    if not selected:
        raise FormatError("write_vcd: no channels with sample data")

    total = max(len(c.samples) for c in selected)
    period_ps = round(1e12 / capture.frequency) if capture.frequency else 1000

    def safe(text: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", text)
        return cleaned or "ch"

    ids = [_VCD_IDS[i % len(_VCD_IDS)] for i in range(len(selected))]

    lines = [
        f"$date {__import__('datetime').datetime.now().isoformat()} $end",
        "$version la_lib.py (RP2040 harness capture) $end",
        f"$comment source {Path(capture.source).name if capture.source else 'capture'} $end",
        f"$comment frequency {capture.frequency} Hz, {period_ps} ps per sample $end",
        # VCD emits a timestamp only when a value changes, so the run of samples
        # after the final change is invisible to a reader. Recording the length
        # explicitly makes the export lossless; this is our own comment, and a
        # third-party reader simply ignores it.
        f"$comment la_total_samples {total} $end",
        f"$timescale {period_ps} ps $end",
        "$scope module logic $end",
    ]
    for channel, ident in zip(selected, ids):
        lines.append(f"$var wire 1 {ident} {safe(channel.label)} $end")
    lines += ["$upscope $end", "$enddefinitions $end", "#0", "$dumpvars"]
    for channel, ident in zip(selected, ids):
        lines.append(f"{1 if channel.samples[0] else 0}{ident}")
    lines.append("$end")

    for index in range(1, total):
        header_written = False
        for channel, ident in zip(selected, ids):
            value = channel.samples[index] if index < len(channel.samples) else channel.samples[-1]
            previous = channel.samples[index - 1]
            if value != previous:
                if not header_written:
                    lines.append(f"#{index}")
                    header_written = True
                lines.append(f"{1 if value else 0}{ident}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_csv(path: str | Path, capture: Capture, *, channels: Sequence[int] | None = None) -> Path:
    """Export samples as CSV in TerminalCapture's own layout.

    Header row: each channel's ``ChannelName``, else ``Channel <position+1>``
    (**array position**, not ``ChannelNumber``). One row per sample index.
    Uses CRLF, matching the Windows tool.
    """
    path = Path(path)
    selected = capture.data_channels()
    if channels is not None:
        wanted = set(channels)
        selected = [c for c in selected if c.number in wanted]
    if not selected:
        raise FormatError("write_csv: no channels with sample data")

    total = max(len(c.samples) for c in selected)
    lines = [",".join(c.position_label for c in selected)]
    for index in range(total):
        lines.append(
            ",".join(str(c.samples[index] if index < len(c.samples) else 0) for c in selected)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    return path

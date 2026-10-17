"""Offline tests for :mod:`fx2_harness.loader` (Intel HEX + download sequence)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fx2_harness import loader
from fx2_harness.loader import (
    CPUCS,
    HALT,
    REQ_FX2,
    RUN,
    LoadResult,
    default_image_path,
    load_firmware,
    parse_ihx,
)


# --------------------------------------------------------------------------
# Intel HEX helpers
# --------------------------------------------------------------------------


def ihx_record(address: int, data: bytes, rtype: int = 0) -> str:
    body = bytes([len(data), (address >> 8) & 0xFF, address & 0xFF, rtype]) + data
    checksum = (-sum(body)) & 0xFF
    return ":" + (body + bytes([checksum])).hex().upper()


def ihx(address: int, data: bytes) -> str:
    """One data record plus an EOF record."""
    return ihx_record(address, data) + "\n:00000001FF\n"


# --------------------------------------------------------------------------
# parse_ihx
# --------------------------------------------------------------------------


def test_parse_ihx_merges_contiguous_records():
    text = ihx_record(0x0000, b"\x01\x02") + "\n" + ihx_record(0x0002, b"\x03\x04") + "\n:00000001FF\n"
    assert parse_ihx(text) == [(0x0000, b"\x01\x02\x03\x04")]


def test_parse_ihx_keeps_gaps_apart():
    text = (
        ihx_record(0x0000, b"\xAA\xBB")
        + "\n"
        + ihx_record(0x0010, b"\xCC")
        + "\n:00000001FF\n"
    )
    assert parse_ihx(text) == [(0x0000, b"\xAA\xBB"), (0x0010, b"\xCC")]


def test_parse_ihx_ignores_blank_lines_and_stops_at_eof():
    text = "\n\n" + ihx_record(0x0100, b"\x11") + "\n\n:00000001FF\n" + ":00000001FF\n"
    assert parse_ihx(text) == [(0x0100, b"\x11")]


def test_parse_ihx_rejects_a_bad_checksum():
    good = ihx_record(0x0000, b"\x01\x02")
    bad = good[:-2] + ("00" if good[-2:] != "00" else "01")
    with pytest.raises(ValueError, match="checksum"):
        parse_ihx(bad + "\n")


def test_parse_ihx_rejects_a_count_mismatch():
    with pytest.raises(ValueError, match="declares"):
        parse_ihx(":03000000010203\n")


def test_parse_ihx_rejects_a_non_hex_line():
    with pytest.raises(ValueError, match="not an Intel HEX record"):
        parse_ihx("hello\n")


def test_parse_ihx_rejects_extended_addressing():
    with pytest.raises(ValueError, match="extended addressing"):
        parse_ihx(ihx_record(0x0000, b"\x01\x02", rtype=4) + "\n")


def test_parse_ihx_rejects_an_empty_image():
    with pytest.raises(ValueError, match="no data records"):
        parse_ihx(":00000001FF\n")


def test_parse_ihx_reads_a_file(tmp_path: Path):
    path = tmp_path / "image.ihx"
    path.write_text(ihx(0x0000, b"\xDE\xAD\xBE\xEF"), encoding="ascii")
    assert parse_ihx(path) == [(0x0000, b"\xDE\xAD\xBE\xEF")]
    # A str path works too.
    assert parse_ihx(str(path)) == [(0x0000, b"\xDE\xAD\xBE\xEF")]


def test_parse_ihx_reports_a_missing_file(tmp_path: Path):
    with pytest.raises(ValueError, match="cannot read"):
        parse_ihx(tmp_path / "nope.ihx")


def test_parse_ihx_accepts_the_real_build_artifact():
    """The build output (if present) must parse and cover the descriptor page."""
    image = default_image_path()
    if not image.exists():
        pytest.skip(f"{image} has not been built yet")
    runs = parse_ihx(image)
    assert runs
    covered = {address for address, data in runs for address in range(address, address + len(data))}
    assert 0x3E00 in covered, "USB descriptors must land at DSCR_AREA 0x3E00"


# --------------------------------------------------------------------------
# default image path
# --------------------------------------------------------------------------


def test_default_image_path_points_into_the_cypress_tree(monkeypatch):
    monkeypatch.delenv("FX2_HARNESS_IMAGE", raising=False)
    path = default_image_path()
    assert path.name == "harness_fx2.ihx"
    assert path.parent.name == "build"
    assert path.parent.parent.name == "firmware"
    assert path.parent.parent.parent.name == "cypress"


def test_default_image_path_honours_the_environment(monkeypatch, tmp_path):
    override = tmp_path / "custom.ihx"
    monkeypatch.setenv("FX2_HARNESS_IMAGE", str(override))
    assert default_image_path() == override


# --------------------------------------------------------------------------
# download sequence
# --------------------------------------------------------------------------


class FakeBootDevice:
    """Records the Cypress 0xA0 download requests."""

    def __init__(self, *, refuse_download: bool = False):
        self.requests: list[tuple[int, int, bytes | None]] = []
        self.configurations = 0
        self.refuse_download = refuse_download

    def set_configuration(self):
        self.configurations += 1

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data, timeout):
        assert bmRequestType == 0x40
        assert bRequest == REQ_FX2
        assert wIndex == 0
        if self.refuse_download and wValue == CPUCS:
            raise OSError("control transfer stalled")
        self.requests.append((bRequest, wValue, data))
        return len(data) if data is not None else 0

    # -- assertion helpers --------------------------------------------------
    def halt_request(self):
        assert self.requests[0] == (REQ_FX2, CPUCS, HALT)
        return self.requests[0]

    def run_request(self):
        assert self.requests[-1] == (REQ_FX2, CPUCS, RUN)
        return self.requests[-1]

    def image_writes(self):
        return [r for r in self.requests if r[1] != CPUCS]


def test_load_firmware_halts_writes_chunks_then_runs(tmp_path, monkeypatch):
    image = tmp_path / "image.ihx"
    payload = bytes(range(0x80))  # two 64-byte download chunks
    image.write_text(
        ihx_record(0x0000, payload[:0x40])
        + "\n"
        + ihx_record(0x0040, payload[0x40:])
        + "\n:00000001FF\n",
        encoding="ascii",
    )

    device = FakeBootDevice()
    monkeypatch.setattr(loader, "find_boot_device", lambda **kw: device)
    monkeypatch.setattr(loader, "find_app_device", lambda **kw: None)
    monkeypatch.setattr(
        loader, "wait_for", lambda vid, pid, timeout=0, **kw: object()
    )

    quiet_lines: list[str] = []
    result = load_firmware(image, out=quiet_lines.append)

    assert isinstance(result, LoadResult)
    assert result.reenumerated is True
    assert result.written == len(payload)
    assert result.total_bytes == len(payload)
    assert result.address_range == (0x0000, 0x007F)
    assert result.reset_from_app is False
    assert result.direct_download is False

    device.halt_request()
    device.run_request()
    writes = device.image_writes()
    assert [w[1] for w in writes] == [0x0000, 0x0040]
    assert all(len(w[2]) <= 64 for w in writes)
    assert b"".join(w[2] for w in writes) == payload


def test_load_firmware_uses_the_core_channel_when_the_app_runs(
    tmp_path, monkeypatch
):
    """A running application device is reprogrammed directly over 0xA0 -- the
    EZ-USB core services those register writes itself."""
    image = tmp_path / "image.ihx"
    image.write_text(ihx(0x0000, b"\x01\x02\x03\x04"), encoding="ascii")

    app = FakeBootDevice()
    monkeypatch.setattr(loader, "find_boot_device", lambda **kw: None)
    monkeypatch.setattr(loader, "find_app_device", lambda **kw: app)
    monkeypatch.setattr(
        loader, "wait_for", lambda vid, pid, timeout=0, **kw: object()
    )
    monkeypatch.setattr(
        loader,
        "reset_to_boot",
        lambda *a, **k: pytest.fail("reset_to_boot must not be needed here"),
    )

    result = load_firmware(image, out=lambda _line: None)

    assert result.direct_download is True
    assert result.reset_from_app is False
    assert result.written == 4
    app.halt_request()
    app.run_request()


def test_load_firmware_falls_back_to_the_boot_device(tmp_path, monkeypatch):
    """If the core download channel is refused, hand USB back to the core."""
    image = tmp_path / "image.ihx"
    image.write_text(ihx(0x0000, b"\x01\x02\x03\x04"), encoding="ascii")

    app = FakeBootDevice(refuse_download=True)
    boot = FakeBootDevice()
    order: list[str] = []

    def find_boot(**kw):
        return boot if order else None

    def wait_for(vid, pid, timeout=0, **kw):
        order.append("wait")
        return boot

    def reset_to_boot(device, **kw):
        order.append("reset")
        assert device is app

    monkeypatch.setattr(loader, "find_boot_device", find_boot)
    monkeypatch.setattr(loader, "find_app_device", lambda **kw: app)
    monkeypatch.setattr(loader, "wait_for", wait_for)
    monkeypatch.setattr(loader, "reset_to_boot", reset_to_boot)

    result = load_firmware(image, out=lambda _line: None)

    assert order == ["reset", "wait", "wait"]
    assert result.reset_from_app is True
    assert result.direct_download is False
    boot.halt_request()
    boot.run_request()


def test_load_firmware_reports_a_missing_board(tmp_path, monkeypatch):
    image = tmp_path / "image.ihx"
    image.write_text(ihx(0x0000, b"\x01"), encoding="ascii")
    monkeypatch.setattr(loader, "find_boot_device", lambda **kw: None)
    monkeypatch.setattr(loader, "find_app_device", lambda **kw: None)

    from rp2040_harness.errors import DeviceNotFoundError

    with pytest.raises(DeviceNotFoundError, match="no FX2LP found"):
        load_firmware(image)


def test_load_firmware_fails_when_the_board_does_not_reenumerate(tmp_path, monkeypatch):
    image = tmp_path / "image.ihx"
    image.write_text(ihx(0x0000, b"\x01"), encoding="ascii")
    device = FakeBootDevice()
    monkeypatch.setattr(loader, "find_boot_device", lambda **kw: device)
    monkeypatch.setattr(loader, "find_app_device", lambda **kw: None)
    monkeypatch.setattr(loader, "wait_for", lambda vid, pid, timeout=0, **kw: None)

    from rp2040_harness.errors import TransportError

    with pytest.raises(TransportError, match="did not re-enumerate"):
        load_firmware(image, out=lambda _line: None)

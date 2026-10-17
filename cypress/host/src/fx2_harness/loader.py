"""Firmware loader for the FX2LP harness.

The FX2LP comes out of reset as the EZ-USB *boot* device ``04B4:8613``: the
8051 is halted and the USB core itself answers the Cypress RAM download
request.  After the image has been written and the CPU released, the firmware
re-enumerates with its own descriptors as ``04B4:8614``, where the
pre-installed ``cyfx2lp.inf`` binds WinUSB.

Download protocol (the same one every FX2LP loader uses, and the one the
reference ``tools/fx28255.py`` implements):

* vendor request ``0xA0`` to register ``CPUCS`` (``0xE600``) with a one-byte
  payload ``0x01`` halts the 8051,
* ``0xA0`` with ``wValue = address`` and up to 64 data bytes writes the image
  into RAM,
* ``0xA0`` to ``CPUCS`` with ``0x00`` releases the 8051, which runs the image
  from address 0 and re-enumerates.

A device that is already running the application firmware is sent the
``REQ_RESET_TO_BOOT`` extension request first (the CLI command ``boot`` does
the same), so the board never has to be physically replugged between builds.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Iterable

from rp2040_harness.errors import DeviceNotFoundError, TransportError

from .transport import (
    APP_PID,
    APP_VID,
    BOOT_PID,
    BOOT_VID,
    REQ_RESET_TO_BOOT,
    _default_backend,
    _usb_core,
)

__all__ = [
    "REQ_FX2",
    "CPUCS",
    "HALT",
    "RUN",
    "DEFAULT_TIMEOUT_S",
    "LoadResult",
    "parse_ihx",
    "default_image_path",
    "find_boot_device",
    "find_app_device",
    "wait_for",
    "reset_to_boot",
    "load_firmware",
]

#: Cypress' RAM download / "firmware load" vendor request.
REQ_FX2: Final = 0xA0
#: CPU control and status register: bit 0 halts the 8051.
CPUCS: Final = 0xE600
HALT: Final = b"\x01"
RUN: Final = b"\x00"

DEFAULT_TIMEOUT_S: Final = 8.0
#: A RAM download request carries at most 64 bytes.
DOWNLOAD_CHUNK: Final = 64


def default_image_path() -> Path:
    """``cypress/firmware/build/harness_fx2.ihx`` (or ``$FX2_HARNESS_IMAGE``)."""
    override = os.environ.get("FX2_HARNESS_IMAGE")
    if override:
        return Path(override)
    # .../cypress/host/src/fx2_harness/loader.py -> .../cypress
    return (
        Path(__file__).resolve().parents[3]
        / "firmware"
        / "build"
        / "harness_fx2.ihx"
    )


# --------------------------------------------------------------------------
# Intel HEX
# --------------------------------------------------------------------------


def _read_ihx(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii")
    except OSError as exc:
        raise ValueError(f"cannot read the Intel HEX image {path}: {exc}") from exc


def parse_ihx(source) -> list[tuple[int, bytes]]:
    """Parse an Intel HEX file into ``[(address, data), ...]`` runs.

    ``source`` is a path (``str``/``Path``) or the file's text.  Consecutive
    records are merged into one run so the download needs as few control
    transfers as possible.  Every record's checksum is verified; a malformed
    file raises :class:`ValueError`.
    """
    if isinstance(source, Path):
        text = _read_ihx(source)
    elif isinstance(source, str):
        # A HEX record always starts with ':' and a file's text contains a
        # newline, so plain hex text is never mistaken for a path.
        if source.lstrip().startswith(":") or "\n" in source:
            text = source
        else:
            text = _read_ihx(Path(source))
    else:
        text = source

    runs: list[tuple[int, bytes]] = []
    current: tuple[int, bytes] | None = None
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"line {lineno}: not an Intel HEX record: {line!r}")
        try:
            body = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise ValueError(f"line {lineno}: bad hex digits in {line!r}") from exc
        if len(body) < 5:
            raise ValueError(f"line {lineno}: record too short ({len(body)} bytes)")
        count, address, rtype = body[0], (body[1] << 8) | body[2], body[3]
        if len(body) != count + 5:
            raise ValueError(
                f"line {lineno}: record declares {count} data bytes but carries "
                f"{len(body) - 5}"
            )
        if sum(body) & 0xFF:
            raise ValueError(f"line {lineno}: Intel HEX checksum mismatch")
        data = body[4 : 4 + count]
        if rtype == 0x00:  # data
            if current is not None and current[0] + len(current[1]) == address:
                current = (current[0], current[1] + data)
                runs[-1] = current
            else:
                current = (address, data)
                runs.append(current)
        elif rtype == 0x01:  # end of file
            break
        elif rtype in (0x02, 0x04):
            raise ValueError(
                f"line {lineno}: extended addressing (record type {rtype:02X}) "
                "is not supported by the FX2LP RAM download"
            )
        # 0x03 / 0x05 (start address records) are legal and irrelevant here.
    if not runs:
        raise ValueError("the Intel HEX image contains no data records")
    return runs


# --------------------------------------------------------------------------
# Device discovery
# --------------------------------------------------------------------------


def _find(vid: int, pid: int, backend=None):
    usb_core = _usb_core()
    if backend is None:
        backend = _default_backend()
    kwargs: dict = {"idVendor": vid, "idProduct": pid}
    if backend is not None:
        kwargs["backend"] = backend
    try:
        return usb_core.find(**kwargs)
    except Exception as exc:  # pragma: no cover - environment dependent
        raise TransportError(
            f"USB enumeration failed ({exc.__class__.__name__}: {exc})."
        ) from exc


def find_boot_device(*, backend=None):
    """The unconfigured FX2LP (``04B4:8613``) or ``None``."""
    return _find(BOOT_VID, BOOT_PID, backend)


def find_app_device(*, backend=None):
    """The FX2LP running our firmware (``04B4:8614``) or ``None``."""
    return _find(APP_VID, APP_PID, backend)


def wait_for(vid: int, pid: int, timeout: float = DEFAULT_TIMEOUT_S, *, backend=None):
    """Poll for a device until ``timeout`` seconds have elapsed.

    Enumeration errors are treated as "not there yet": while a device
    renumbers, libusb can raise instead of returning ``None``, and that must
    not abort a load.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        try:
            device = _find(vid, pid, backend)
        except TransportError:
            device = None
        if device is not None:
            return device
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def _dispose(device) -> None:
    try:  # pragma: no cover - environment dependent
        import usb.util  # type: ignore

        usb.util.dispose_resources(device)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


@dataclass
class LoadResult:
    """What :func:`load_firmware` did."""

    image: Path
    runs: int
    total_bytes: int
    written: int
    address_range: tuple[int, int]
    reset_from_app: bool = False
    direct_download: bool = False
    reenumerated: bool = False
    device: object = field(default=None, repr=False)

    def lines(self) -> Iterable[str]:
        yield f"image     : {self.image}"
        yield (
            f"runs      : {self.runs} "
            f"(0x{self.address_range[0]:04X}..0x{self.address_range[1]:04X})"
        )
        yield f"bytes     : {self.written} written of {self.total_bytes}"


def reset_to_boot(device=None, *, backend=None, timeout_ms: int = 2000) -> None:
    """Ask the application firmware to hand USB back to the EZ-USB core."""
    device = device or find_app_device(backend=backend)
    if device is None:
        raise DeviceNotFoundError(
            f"no {APP_VID:04X}:{APP_PID:04X} device to reset to boot"
        )
    try:
        device.ctrl_transfer(0x40, REQ_RESET_TO_BOOT, 0, 0, None, timeout_ms)
    except Exception as exc:
        raise TransportError(
            "the running firmware did not accept the reset-to-boot request "
            f"({exc.__class__.__name__}: {exc}). If an unrelated firmware "
            "owns 04B4:8614, unplug and replug the board."
        ) from exc


def _download(device, runs: list[tuple[int, bytes]], total: int) -> int:
    """Halt the 8051, push the image through ``0xA0``, release the 8051."""
    try:
        device.set_configuration()
    except Exception:  # noqa: BLE001 - redundant on an enumerated device
        pass

    def control_out(value, data=None):
        return device.ctrl_transfer(0x40, REQ_FX2, value & 0xFFFF, 0, data, 2000)

    written = 0
    try:
        control_out(CPUCS, HALT)
        for address, data in runs:
            for offset in range(0, len(data), DOWNLOAD_CHUNK):
                piece = data[offset : offset + DOWNLOAD_CHUNK]
                control_out(address + offset, piece)
                written += len(piece)
    except Exception as exc:
        raise TransportError(
            f"firmware download failed after {written} of {total} bytes: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    finally:
        try:
            control_out(CPUCS, RUN)
        except Exception:
            pass
    return written


def load_firmware(
    image=None,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    quiet: bool = False,
    wait_reenumerate: bool = True,
    backend=None,
    out=print,
) -> LoadResult:
    """Download ``image`` into the FX2LP's RAM and release the CPU.

    Three routes, in order:

    1. The board is the unconfigured boot device ``04B4:8613`` -- download.
    2. The board runs our firmware -- the EZ-USB *core* still services the
       ``0xA0`` register writes, so the new image is pushed through the core
       while the application device is enumerated (no replug, no round trip
       through ``04B4:8613``).
    3. That channel is refused (a foreign firmware owns the PID) -- ask the
       firmware to hand USB back to the core (``REQ_RESET_TO_BOOT``), wait for
       ``04B4:8613`` and download there.

    Raises :class:`~rp2040_harness.errors.DeviceNotFoundError` when no FX2LP
    is visible and :class:`~rp2040_harness.errors.TransportError` when the
    download itself fails.
    """
    path = Path(image) if image is not None else default_image_path()
    runs = parse_ihx(path)
    total = sum(len(data) for _, data in runs)
    low = min(addr for addr, _ in runs)
    high = max(addr + len(data) for addr, data in runs) - 1

    reset_from_app = False
    direct_download = False

    boot = find_boot_device(backend=backend)
    if boot is not None:
        written = _download(boot, runs, total)
        _dispose(boot)
    else:
        app = find_app_device(backend=backend)
        if app is None:
            raise DeviceNotFoundError(
                f"no FX2LP found (neither the boot device {BOOT_VID:04X}:"
                f"{BOOT_PID:04X} nor the application device {APP_VID:04X}:"
                f"{APP_PID:04X}). Check the cable and that cyfx2lp.inf is bound."
            )
        if not quiet:
            out("device    : application firmware is running; downloading "
                "through the EZ-USB core")
        try:
            written = _download(app, runs, total)
            direct_download = True
            _dispose(app)
        except TransportError as exc:
            if not quiet:
                out(f"device    : core download refused ({exc}); handing USB "
                    "back to the EZ-USB core")
            _dispose(app)
            app = find_app_device(backend=backend)
            if app is None:
                raise TransportError(
                    "the board renumbered while the download was being retried; "
                    "run `load` again"
                ) from exc
            try:
                reset_to_boot(app, backend=backend)
            finally:
                _dispose(app)
            boot = wait_for(BOOT_VID, BOOT_PID, 10.0, backend=backend)
            if boot is None:
                raise TransportError(
                    "the board did not re-enumerate as the boot device "
                    f"{BOOT_VID:04X}:{BOOT_PID:04X}; unplug and replug the "
                    "board, then load again"
                ) from exc
            written = _download(boot, runs, total)
            _dispose(boot)
            reset_from_app = True

    result = LoadResult(
        image=path,
        runs=len(runs),
        total_bytes=total,
        written=written,
        address_range=(low, high),
        reset_from_app=reset_from_app,
        direct_download=direct_download,
    )
    if written != total:
        raise TransportError(
            f"firmware download wrote {written} of {total} bytes"
        )
    if wait_reenumerate:
        # The released 8051 renumbers itself straight away.
        time.sleep(0.3)
        app = wait_for(APP_VID, APP_PID, timeout, backend=backend)
        if app is None:
            raise TransportError(
                "the firmware was downloaded but the board did not re-enumerate "
                f"as {APP_VID:04X}:{APP_PID:04X} within {timeout:.1f} s. The "
                "8051 may have crashed: unplug and replug the board, then load "
                "again (a blank board still answers as the boot device)."
            )
        result.reenumerated = True
        result.device = app
    return result

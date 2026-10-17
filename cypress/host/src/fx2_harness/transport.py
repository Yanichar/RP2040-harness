"""The chunked-EP0 transport: a :class:`rp2040_harness.transport.Transport`.

The Cypress board has no bulk endpoints at all (its interface descriptor has
``bNumEndpoints = 0``), so a protocol frame -- up to 1032 bytes, see
``docs/protocol.md`` §2 -- has to travel through endpoint zero.  EP0 moves at
most 64 bytes per transfer, so the firmware and this module implement a small
chunked transport around the frozen frame format:

======================  ======  ==============  ============  ==================
request                 bmReq   wValue          wIndex        data stage
======================  ======  ==============  ============  ==================
``REQ_OUT_CHUNK``       0x40    byte offset     byte count    OUT: frame bytes
``REQ_EXEC``            0x40    total length    0             none
``REQ_REPLY_LEN``       0xC0    0               0             IN: 2 (u16 length)
``REQ_IN_CHUNK``        0xC0    byte offset     byte count    IN: reply bytes
======================  ======  ==============  ============  ==================

``write(frame)`` sends the frame in ``REQ_OUT_CHUNK`` transfers of at most 64
bytes, issues ``REQ_EXEC`` and then -- important -- pulls the whole reply
before returning.  The device keeps its reply in RAM until the host collects
it, so a second ``REQ_EXEC`` would overwrite it; collecting eagerly makes the
transport safe against a host that writes the next request before reading the
previous reply (``Harness.stream()`` pipelines exactly like that).

Replies are queued and handed out in request order, so the transport behaves
like an ordinary ordered byte pipe: ``read()`` never returns bytes of two
different frames in one call.
"""

from __future__ import annotations

import collections
import struct
from typing import Final

from rp2040_harness.errors import TransportError
from rp2040_harness.transport import Transport

__all__ = [
    "APP_VID",
    "APP_PID",
    "BOOT_VID",
    "BOOT_PID",
    "MAX_FRAME",
    "CHUNK_SIZE",
    "REQ_OUT_CHUNK",
    "REQ_EXEC",
    "REQ_REPLY_LEN",
    "REQ_IN_CHUNK",
    "REQ_RESET_TO_BOOT",
    "REQ_DEBUG",
    "BM_OUT",
    "BM_IN",
    "ControlTransport",
    "list_devices",
    "find_device",
]

#: Unconfigured FX2LP (EZ-USB boot device, WinUSB bound): firmware download target.
BOOT_VID: Final = 0x04B4
BOOT_PID: Final = 0x8613
#: Application firmware (this project).  ``cyfx2lp.inf`` binds WinUSB to it.
APP_VID: Final = 0x04B4
APP_PID: Final = 0x8614

#: protocol.md §2.5: frame <= 8 + 1024 bytes.
MAX_FRAME: Final = 1032
#: EP0 max packet size (full and high speed).
CHUNK_SIZE: Final = 64

#: Vendor request codes, bmRequestType 0x40 (host -> device) / 0xC0 (in).
REQ_OUT_CHUNK: Final = 0x20
REQ_EXEC: Final = 0x21
REQ_REPLY_LEN: Final = 0x22
REQ_IN_CHUNK: Final = 0x23
#: Extension (not in protocol.md): hand USB back to the EZ-USB core.
REQ_RESET_TO_BOOT: Final = 0x28
#: Extension (not in protocol.md): 16 bytes of firmware diagnostics.
REQ_DEBUG: Final = 0x29

BM_OUT: Final = 0x40
BM_IN: Final = 0xC0


def _usb_core():
    """Import ``usb.core`` lazily so the tests never need libusb."""
    try:
        import usb.core  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise TransportError(
            "pyusb is not usable in this environment "
            f"({exc.__class__.__name__}: {exc}). Install the dependencies with "
            "pip install pyusb libusb-package"
        ) from exc
    return usb.core


def _default_backend():
    """The libusb-1.0 backend shipped by the ``libusb-package`` wheel."""
    try:
        import libusb_package  # type: ignore
    except Exception:  # pragma: no cover - optional
        return None
    try:
        return libusb_package.get_libusb1_backend()
    except Exception:  # pragma: no cover - environment dependent
        return None


def list_devices(vid: int = APP_VID, pid: int = APP_PID, backend=None) -> list:
    """Return every USB device matching ``vid:pid``."""
    usb_core = _usb_core()
    if backend is None:
        backend = _default_backend()
    kwargs: dict = {"find_all": True, "idVendor": vid, "idProduct": pid}
    if backend is not None:
        kwargs["backend"] = backend
    try:
        return list(usb_core.find(**kwargs))
    except Exception as exc:  # pragma: no cover - environment dependent
        raise TransportError(
            f"USB enumeration failed ({exc.__class__.__name__}: {exc})."
        ) from exc


def find_device(
    vid: int = APP_VID,
    pid: int = APP_PID,
    *,
    backend=None,
    device=None,
):
    """Find (or pass through) one ``usb.core.Device``."""
    if device is not None:
        return device
    usb_core = _usb_core()
    if backend is None:
        backend = _default_backend()
    kwargs: dict = {"idVendor": vid, "idProduct": pid}
    if backend is not None:
        kwargs["backend"] = backend
    try:
        found = usb_core.find(**kwargs)
    except Exception as exc:
        raise TransportError(
            f"USB enumeration failed ({exc.__class__.__name__}: {exc})."
        ) from exc
    if found is None:
        from rp2040_harness.errors import DeviceNotFoundError

        raise DeviceNotFoundError(
            f"no USB device {vid:04X}:{pid:04X}. The Cypress FX2LP application "
            f"firmware runs as {APP_VID:04X}:{APP_PID:04X}; load it first with "
            "`python -m fx2_harness load`."
        )
    return found


class ControlTransport(Transport):
    """Transport over the Cypress board's chunked EP0 protocol.

    Parameters
    ----------
    device:
        An open ``usb.core.Device`` (see :func:`find_device`).
    timeout_ms:
        Per-control-transfer timeout, default 1000 ms.
    chunk_size:
        EP0 payload per chunk; the protocol fixes this at 64.
    """

    def __init__(
        self,
        device,
        *,
        timeout_ms: int = 1000,
        chunk_size: int = CHUNK_SIZE,
    ):
        self.device = device
        self.timeout_ms = int(timeout_ms)
        self.chunk_size = int(chunk_size)
        if not 1 <= self.chunk_size <= 64:
            raise ValueError(f"chunk_size must be 1..64, got {chunk_size}")
        self._replies: collections.deque[bytearray] = collections.deque()
        self._current = bytearray()
        self._closed = False

    # -- low level ----------------------------------------------------------

    def _out(self, request: int, value: int, index: int, data=None) -> None:
        try:
            self.device.ctrl_transfer(
                BM_OUT, request, value & 0xFFFF, index & 0xFFFF, data,
                self.timeout_ms,
            )
        except Exception as exc:
            raise TransportError(
                f"control OUT bRequest=0x{request:02X} wValue=0x{value:04X} "
                f"wIndex=0x{index:04X} on {self.description} failed: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc

    def _in(self, request: int, value: int, index: int, length: int) -> bytes:
        try:
            raw = self.device.ctrl_transfer(
                BM_IN, request, value & 0xFFFF, index & 0xFFFF, length,
                self.timeout_ms,
            )
        except Exception as exc:
            raise TransportError(
                f"control IN bRequest=0x{request:02X} wValue=0x{value:04X} "
                f"wIndex=0x{index:04X} on {self.description} failed: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        if raw is None:
            return b""
        return bytes(raw)

    # -- Transport API ------------------------------------------------------

    @property
    def description(self) -> str:
        try:
            return (
                f"FX2LP {APP_VID:04X}:{APP_PID:04X} "
                f"bus={self.device.bus} addr={self.device.address}"
            )
        except Exception:  # pragma: no cover - cosmetic
            return f"FX2LP {APP_VID:04X}:{APP_PID:04X}"

    def write(self, data: bytes) -> int:
        """Send one protocol frame and buffer its reply."""
        if self._closed:
            raise TransportError("transport is closed")
        data = bytes(data)
        if not data:
            raise TransportError("refusing to send an empty frame")
        if len(data) > MAX_FRAME:
            raise TransportError(
                f"frame of {len(data)} bytes exceeds the protocol maximum of "
                f"{MAX_FRAME} bytes"
            )
        offset = 0
        while offset < len(data):
            piece = data[offset : offset + self.chunk_size]
            self._out(REQ_OUT_CHUNK, offset, len(piece), piece)
            offset += len(piece)
        self._out(REQ_EXEC, len(data), 0, None)
        self._replies.append(self._pull_reply())
        return len(data)

    def read(self, size: int, timeout_ms: int = 1000) -> bytes:
        """Return up to ``size`` bytes of the next buffered reply frame."""
        if self._closed:
            raise TransportError("transport is closed")
        if size <= 0:
            return b""
        while not self._current:
            if not self._replies:
                return b""
            self._current = self._replies.popleft()
        n = min(size, len(self._current))
        chunk = bytes(self._current[:n])
        del self._current[:n]
        return chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._replies.clear()
        self._current = bytearray()
        try:  # pragma: no cover - environment dependent
            import usb.util  # type: ignore

            try:
                usb.util.release_interface(self.device, 0)
            except Exception:
                pass
            try:
                usb.util.dispose_resources(self.device)
            except Exception:
                pass
        except Exception:
            pass

    # -- internals ----------------------------------------------------------

    def _pull_reply(self) -> bytearray:
        raw = self._in(REQ_REPLY_LEN, 0, 0, 2)
        if len(raw) != 2:
            raise TransportError(
                f"REQ_REPLY_LEN returned {len(raw)} bytes, expected 2, from "
                f"{self.description}"
            )
        length = struct.unpack("<H", raw)[0]
        if length == 0:
            raise TransportError(
                f"{self.description} reports an empty reply frame; the device "
                "did not execute the request"
            )
        if length > MAX_FRAME:
            raise TransportError(
                f"reply frame length {length} exceeds the protocol maximum of "
                f"{MAX_FRAME} bytes"
            )
        buf = bytearray()
        offset = 0
        while offset < length:
            n = min(self.chunk_size, length - offset)
            chunk = self._in(REQ_IN_CHUNK, offset, n, n)
            if len(chunk) != n:
                raise TransportError(
                    f"short REQ_IN_CHUNK: asked for {n} bytes at offset "
                    f"{offset}, got {len(chunk)} from {self.description}"
                )
            buf += chunk
            offset += n
        return buf

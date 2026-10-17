"""USB transport layer.

* :class:`Transport` — the abstract byte pipe the :class:`~rp2040_harness.device.Harness`
  talks to: ``write(data)``, ``read(size, timeout_ms)``, ``close()``.
* :class:`PyUsbTransport` — the real thing, over ``pyusb``/``libusb``.  On
  Windows it loads the libusb-1.0 DLL shipped by the ``libusb-package`` wheel,
  so no manual libusb installation is required.
* :class:`FakeTransport` — an in-memory RP2040 Harness that speaks the real
  wire protocol (frame parser, CRC checks, status codes, per-pin state).  It
  lets the whole host stack be tested with no hardware attached.

Protocol reference: ``docs/protocol.md`` v1.0 (§1 transport, §2 framing,
§4 opcodes).
"""

from __future__ import annotations

import abc
import struct
import time
from typing import Final

from . import protocol as P
from .errors import DeviceNotFoundError, TransportError

__all__ = [
    "DEFAULT_VID",
    "DEFAULT_PID",
    "DATA_INTERFACE",
    "EP_OUT",
    "EP_IN",
    "Transport",
    "PyUsbTransport",
    "FakeTransport",
    "list_devices",
]

DEFAULT_VID: Final = 0x2E8A
DEFAULT_PID: Final = 0x10C0

# protocol.md §1: interface 0 is data, bulk OUT 0x01 / bulk IN 0x81.
DATA_INTERFACE: Final = 0
EP_OUT: Final = 0x01
EP_IN: Final = 0x81
# Full-speed bulk max packet size (protocol.md §1).
BULK_PACKET_SIZE: Final = 64


class Transport(abc.ABC):
    """Abstract byte transport to the harness device."""

    @abc.abstractmethod
    def write(self, data: bytes) -> int:
        """Send ``data`` to the device.  Returns the number of bytes written."""

    @abc.abstractmethod
    def read(self, size: int, timeout_ms: int = 1000) -> bytes:
        """Read up to ``size`` bytes, waiting at most ``timeout_ms``.

        Returns ``b""`` on timeout (the caller decides whether that is an
        error).  May return fewer bytes than requested.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Release the device.  Idempotent."""

    # Generic helpers -------------------------------------------------------

    @property
    def description(self) -> str:
        """One-line human description, used in error messages."""
        return type(self).__name__

    def read_exact(self, size: int, timeout_ms: int = 1000) -> bytes:
        """Read exactly ``size`` bytes or raise :class:`~rp2040_harness.errors.HarnessTimeout`.

        Uses the same deadline budgeting as :meth:`Harness._read_frame`, but is
        also useful on its own.
        """
        from .errors import HarnessTimeout

        deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
        buf = bytearray()
        while len(buf) < size:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            chunk = self.read(size - len(buf), min(remaining_ms, timeout_ms))
            if not chunk:
                break
            buf += chunk
        if len(buf) < size:
            raise HarnessTimeout(
                f"timed out after {timeout_ms} ms reading {size} bytes from "
                f"{self.description}: got {len(buf)}"
            )
        return bytes(buf)

    def __enter__(self) -> "Transport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# --------------------------------------------------------------------------
# Real transport
# --------------------------------------------------------------------------


def _usb_util():
    """Import ``usb.util`` lazily; it owns claim_interface/release_interface."""
    try:
        import usb.util  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise TransportError(
            "pyusb is not usable in this environment "
            f"({exc.__class__.__name__}: {exc}). "
            "Install the dependencies with: pip install pyusb libusb-package"
        ) from exc
    return usb.util


def _usb_core():
    """Import ``usb.core`` lazily so tests never need libusb."""
    try:
        import usb.core  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise TransportError(
            "pyusb is not usable in this environment "
            f"({exc.__class__.__name__}: {exc}). "
            "Install the dependencies with: pip install pyusb libusb-package"
        ) from exc
    if getattr(usb.core, "USBError", None) is None:  # pragma: no cover
        raise TransportError(
            "pyusb is installed but its backend is unavailable; install "
            "libusb-package with: pip install libusb-package"
        )
    return usb.core


def _default_backend():
    """Best backend for this platform.

    On Windows the ``libusb-package`` wheel carries a libusb-1.0 DLL; using it
    means no separate libusb install and no PATH juggling.
    """
    try:
        import libusb_package  # type: ignore
    except Exception:
        return None
    try:
        return libusb_package.get_libusb1_backend()
    except Exception:  # pragma: no cover - environment dependent
        return None


def list_devices(
    vid: int = DEFAULT_VID,
    pid: int = DEFAULT_PID,
    serial: str | None = None,
    backend=None,
) -> list:
    """Return every matching USB device (``usb.core.Device`` objects)."""
    usb_core = _usb_core()
    if backend is None:
        backend = _default_backend()
    kwargs: dict = {"find_all": True, "idVendor": vid, "idProduct": pid}
    if backend is not None:
        kwargs["backend"] = backend
    try:
        devices = list(usb_core.find(**kwargs))
    except Exception as exc:
        raise TransportError(
            f"USB enumeration failed ({exc.__class__.__name__}: {exc}). "
            "Check that the device is plugged in and that a WinUSB driver is bound."
        ) from exc
    if serial is None:
        return devices
    wanted = str(serial)
    return [d for d in devices if _safe_serial(d) == wanted]


def _safe_serial(device) -> str | None:
    try:
        return device.serial_number
    except Exception:
        return None


class PyUsbTransport(Transport):
    """Real transport over pyusb/libusb.

    Parameters
    ----------
    device:
        An already-open ``usb.core.Device``.  When omitted the constructor
        discovers one by ``vid``/``pid``/``serial``.
    vid, pid:
        Defaults per protocol.md §6: ``0x2E8A`` / ``0x10C0``.
    serial:
        Optional serial number (16 hex characters) to disambiguate.
    timeout_ms:
        Default read timeout.
    """

    def __init__(
        self,
        device=None,
        *,
        vid: int = DEFAULT_VID,
        pid: int = DEFAULT_PID,
        serial: str | None = None,
        timeout_ms: int = 1000,
        backend=None,
    ):
        self.vid = vid
        self.pid = pid
        self.serial = serial
        self.timeout_ms = timeout_ms
        self._closed = False
        self._detached_kernel_driver = False

        if device is None:
            device = self._find_device(vid, pid, serial, backend)
        self.device = device
        self._configure()

    # -- discovery / setup --------------------------------------------------

    @staticmethod
    def _find_device(vid, pid, serial, backend):
        usb_core = _usb_core()
        if backend is None:
            backend = _default_backend()
        kwargs: dict = {"idVendor": vid, "idProduct": pid}
        if backend is not None:
            kwargs["backend"] = backend
        try:
            device = usb_core.find(**kwargs)
        except Exception as exc:
            raise TransportError(
                f"USB enumeration failed ({exc.__class__.__name__}: {exc})."
            ) from exc
        if device is None:
            raise DeviceNotFoundError(
                f"no USB device with VID:PID {vid:04X}:{pid:04X}"
                + (f" and serial {serial!r}" if serial else "")
                + " was found. Check the cable, that the firmware is running, and "
                "that WinUSB is bound (WCID descriptors in the firmware)."
            )
        if serial is not None and _safe_serial(device) != str(serial):
            raise DeviceNotFoundError(
                f"device {vid:04X}:{pid:04X} found but its serial "
                f"{_safe_serial(device)!r} does not match the requested {serial!r}"
            )
        return device

    def _configure(self) -> None:
        device = self.device

        # 1. Selecting a configuration is BEST EFFORT, and normally a no-op.
        #
        # A device that enumerated already has a configuration, so this call is
        # redundant — and on WinUSB it is actively harmful: the libusb backend
        # answers a redundant SET_CONFIGURATION with "Access denied" (some
        # builds with "Operation not supported"). Requiring it to succeed made
        # every session after the first fail to open with Access denied.
        try:
            device.set_configuration()
        except Exception:  # noqa: BLE001 - pyusb raises usb.core.USBError
            pass

        # Detach a kernel driver if one grabbed the interface (Linux).
        try:  # pragma: no cover - platform specific
            if device.is_kernel_driver_active(DATA_INTERFACE):
                device.detach_kernel_driver(DATA_INTERFACE)
                self._detached_kernel_driver = True
        except Exception:
            pass

        # 2. Claiming the data interface is what actually grants exclusive access
        #    to the bulk pipes. This one IS reported: a failure means another
        #    process holds the device, or WinUSB is not bound.
        try:
            _usb_util().claim_interface(device, DATA_INTERFACE)
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if "already" not in text and "busy" not in text:
                raise TransportError(
                    f"could not claim interface {DATA_INTERFACE} on "
                    f"{self.description}: {exc.__class__.__name__}: {exc}. "
                    "Another program may be using the device, or the WinUSB "
                    "driver is not bound."
                ) from exc

        # 3. Make sure the alternate setting with our endpoints is selected.
        #    Tolerated: backends reject this when nothing needs changing.
        try:
            device.set_interface_altsetting(
                interface=DATA_INTERFACE, alternate_setting=0
            )
        except Exception:  # noqa: BLE001
            pass

    # -- Transport API ------------------------------------------------------

    @property
    def description(self) -> str:
        serial = None
        try:
            serial = self.device.serial_number
        except Exception:
            serial = self.serial
        extra = f" serial={serial}" if serial else ""
        return f"USB {self.vid:04X}:{self.pid:04X}{extra}"

    def write(self, data: bytes) -> int:
        if self._closed:
            raise TransportError("transport is closed")
        data = bytes(data)
        try:
            written = self.device.write(EP_OUT, data, timeout=self.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(
                f"USB bulk write of {len(data)} bytes to {self.description} failed: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        written = int(written) if written is not None else len(data)
        if written != len(data):
            raise TransportError(
                f"short USB bulk write: {written} of {len(data)} bytes"
            )
        # The device arms its OUT endpoint with room for a whole frame, so a
        # request normally arrives as ONE USB transfer that ends on a short
        # packet. When the frame length is an exact multiple of the endpoint
        # packet size there is no short packet, so append a zero-length packet
        # to terminate the transfer (docs/protocol.md §2.5).
        if len(data) and (len(data) % BULK_PACKET_SIZE) == 0:
            try:
                self.device.write(EP_OUT, b"", timeout=self.timeout_ms)
            except Exception as exc:  # noqa: BLE001
                raise TransportError(
                    f"USB zero-length write to {self.description} failed: "
                    f"{exc.__class__.__name__}: {exc}"
                ) from exc
        return written

    def read(self, size: int, timeout_ms: int = 1000) -> bytes:
        if self._closed:
            raise TransportError("transport is closed")
        try:
            data = self.device.read(EP_IN, size, timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            if _is_timeout_error(exc):
                return b""
            raise TransportError(
                f"USB bulk read from {self.description} failed: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        if data is None:
            return b""
        return bytes(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        device = self.device
        if device is None:
            return
        # NOTE: deliberately NO clear_halt() on the data endpoints here.
        #
        # CLEAR_FEATURE(ENDPOINT_HALT) is a *device* control request: TinyUSB
        # services it with usbd_edpt_clear_stall(), which clears the endpoint's
        # busy flag and DISCARDS the transfer the firmware had armed, without
        # ever invoking the class driver's xfer callback. Issuing it on every
        # close therefore wipes the device's receive state on each teardown, and
        # if anything goes wrong with re-arming, the device stops answering for
        # good — it is still enumerated, but every bulk packet is NAKed.
        #
        # The harness firmware never stalls EP1, so there is nothing to clear.
        # A genuinely stuck pipe is recovered by the firmware's own re-arm
        # retry (see firmware/src/usb_vendor.c) or by re-plugging.
        try:  # pragma: no cover - platform specific
            if self._detached_kernel_driver:
                device.attach_kernel_driver(DATA_INTERFACE)
        except Exception:
            pass

        # Release the claim, then hand the handle back.
        #
        # `dispose_resources` is a MODULE-level function in pyusb
        # (usb.util.dispose_resources); it is NOT a Device method. Calling the
        # method form raised AttributeError, which a bare `except: pass` hid —
        # so the interface was never released and the handle never closed, and
        # enough repeated open/close cycles made the device undclaimable with
        # "Access denied". Do both steps explicitly and by the correct API.
        usb_util = _usb_util()
        try:
            usb_util.release_interface(device, DATA_INTERFACE)
        except Exception:  # noqa: BLE001 - already released / never claimed
            pass
        try:
            usb_util.dispose_resources(device)
        except Exception:  # noqa: BLE001
            pass

    def reset(self) -> None:
        """Issue a USB port reset (best effort)."""
        try:
            self.device.reset()
        except Exception as exc:  # noqa: BLE001
            raise TransportError(
                f"USB reset of {self.description} failed: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc


def _is_timeout_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if "timeout" in text or "timed out" in text:
        return True
    return getattr(exc, "errno", None) in (-7, 110)


# --------------------------------------------------------------------------
# Fake transport (tests, no hardware)
# --------------------------------------------------------------------------


class FakeTransport(Transport):
    """An in-memory device that speaks the real protocol.

    It parses request frames exactly like the firmware is specified to do
    (magic, length limit, CRC, status codes, per-opcode payload validation) and
    answers with real reply frames.  Tests can therefore drive the real
    :class:`~rp2040_harness.device.Harness` end to end without USB hardware.

    Behavioural knobs
    -----------------
    vid, pid, serial:
        Identify the fake device (used by discovery tests).
    stream_rate_mhz:
        Controls the reported STREAM duration so ``updates_per_second`` is
        deterministic in tests.
    max_read_chunk:
        Largest single ``read()`` result, mimicking a 64-byte bulk endpoint.
    strict_length:
        When True, a payload whose length does not match the opcode yields
        ERR_BAD_LENGTH, exactly like the firmware.
    fail_next_write, fail_next_read, drop_replies, force_status, wrong_seq,
    corrupt_crc:
        Fault-injection switches for error-path tests.
    """

    def __init__(
        self,
        *,
        vid: int = DEFAULT_VID,
        pid: int = DEFAULT_PID,
        serial: str = "0123456789ABCDEF",
        pin_count: int = P.PIN_COUNT,
        fw_version: tuple[int, int, int] = (1, 0, 0),
        board_id: int = 0xE6605A1B2C3D4E5F,
        caps: int = P.CAPS_V1,
        stream_rate_mhz: float = 1.0,
        max_read_chunk: int = 64,
        strict_length: bool = True,
    ):
        self.vid = vid
        self.pid = pid
        self.serial = serial
        self.pin_count = pin_count
        self.fw_version = fw_version
        self.board_id = board_id
        self.caps = caps
        self.stream_rate_mhz = stream_rate_mhz
        self.max_read_chunk = max_read_chunk
        self.strict_length = strict_length

        # Device state, initialised exactly as protocol.md §5.1 requires:
        # every line INPUT, no pull, Schmitt on, 4 mA, slew slow.
        self.directions = [P.DIR_INPUT] * pin_count
        self.flags = [P.FLAG_SCHMITT] * pin_count
        self.drives = [1] * pin_count  # code 1 = 4 mA hardware default
        self.slews = [0] * pin_count
        # ``out_latch`` is what OUTPUT pins drive (and the SIO latch value used
        # by SET_MASK).  ``external`` is the level an INPUT pin sees, i.e. what
        # an attached circuit would drive -- READ_ALL reports the pad, so an
        # OUTPUT pin reports its latch and an INPUT pin reports ``external``.
        self.out_latch = [0] * pin_count
        self.external = [0] * pin_count
        self.streaming = False

        # Counters (§4.11)
        self.frames_ok = 0
        self.frames_err = 0
        self.crc_errors = 0
        self.last_status = P.ST_OK
        self.uptime_ms = 0

        # I/O plumbing
        self._rx = bytearray()  # bytes written by the host, not yet framed
        self._tx = bytearray()  # reply bytes waiting to be read
        self._closed = False
        self.last_request: bytes | None = None
        self.requests: list[tuple[int, int, bytes]] = []
        self.writes: list[bytes] = []

        # Fault injection
        self.fail_next_write: Exception | None = None
        self.fail_next_read: Exception | None = None
        self.drop_replies = 0
        self.force_status: int | None = None
        self.wrong_seq = False
        self.corrupt_crc = False

    # -- test helpers -------------------------------------------------------

    def set_input_level(self, index: int, level: int) -> None:
        """Simulate an external circuit driving pin ``index`` (see READ_ALL)."""
        self.external[index] = 1 if level else 0

    def input_level(self, index: int) -> int:
        """What the device reports for pin ``index`` on READ_ALL (§4.9)."""
        if self.directions[index] == P.DIR_OUTPUT:
            return self.out_latch[index] & 1
        return self.external[index] & 1

    def output_levels(self) -> int:
        """24-bit value of every output latch."""
        value = 0
        for i in range(self.pin_count):
            value |= (self.out_latch[i] & 1) << i
        return value

    @property
    def description(self) -> str:
        return f"FakeTransport({self.vid:04X}:{self.pid:04X})"

    # -- Transport API ------------------------------------------------------

    def write(self, data: bytes) -> int:
        if self._closed:
            raise TransportError("fake transport is closed")
        if self.fail_next_write is not None:
            exc, self.fail_next_write = self.fail_next_write, None
            raise exc
        data = bytes(data)
        self.writes.append(data)
        self._rx += data
        self._pump()
        return len(data)

    def read(self, size: int, timeout_ms: int = 1000) -> bytes:
        if self._closed:
            raise TransportError("fake transport is closed")
        if self.fail_next_read is not None:
            exc, self.fail_next_read = self.fail_next_read, None
            raise exc
        if not self._tx:
            return b""  # timeout, exactly like a real bulk read
        n = min(size, len(self._tx), self.max_read_chunk)
        chunk = bytes(self._tx[:n])
        del self._tx[:n]
        return chunk

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    # -- device implementation ---------------------------------------------

    def _pump(self) -> None:
        """Frame everything currently in ``_rx`` and answer it."""
        while True:
            if len(self._rx) < P.HEADER_SIZE:
                return
            magic, opcode, seq, length, crc = struct.unpack_from("<BBHHH", self._rx, 0)
            if magic != P.REQ_MAGIC:
                self._emit_status(P.ST_ERR_MAGIC, 0)
                self._rx.clear()
                return
            if length > P.MAX_PAYLOAD:
                self._emit_status(P.ST_ERR_BAD_LENGTH, 0)
                self._rx.clear()
                return
            total = P.HEADER_SIZE + length
            if len(self._rx) < total:
                return
            frame = bytes(self._rx[:total])
            del self._rx[:total]
            payload = frame[P.HEADER_SIZE:]
            self.last_request = frame
            self.requests.append((opcode, seq, payload))
            self._handle(opcode, seq, payload, crc)

    def _handle(self, opcode: int, seq: int, payload: bytes, frame_crc: int) -> None:
        expected = P.crc16_ccitt_false(
            struct.pack("<BHH", opcode, seq, len(payload)) + payload
        )
        if expected != frame_crc and not self.corrupt_crc:
            self.crc_errors += 1
            self._emit_status(P.ST_ERR_CRC, seq)
            return
        if self.force_status is not None:
            status, self.force_status = self.force_status, None
            self._emit_status(status, seq)
            return
        try:
            self._dispatch(opcode, seq, payload)
        except _BadLength:
            self.frames_err += 1
            self._emit_status(P.ST_ERR_BAD_LENGTH, seq)

    def _dispatch(self, opcode: int, seq: int, payload: bytes) -> None:
        handler = self._HANDLERS.get(opcode)
        if handler is None:
            self.frames_err += 1
            self._emit_status(P.ST_ERR_UNKNOWN_OPCODE, seq)
            return
        handler(self, seq, payload)

    # -- opcode handlers ----------------------------------------------------

    def _valid_index(self, index: int) -> bool:
        return 0 <= index < self.pin_count

    def _op_get_info(self, seq: int, payload: bytes) -> None:
        self._require_len(payload, 0)
        info = P.Info(
            proto_major=1,
            proto_minor=0,
            fw_major=self.fw_version[0],
            fw_minor=self.fw_version[1],
            fw_patch=self.fw_version[2],
            pin_count=self.pin_count,
            caps=self.caps,
            pin_mask=(1 << self.pin_count) - 1,
            board_id=self.board_id,
            reset_iface=1,
        )
        self._emit(P.ST_OK, seq, info.pack())

    def _op_get_pin(self, seq: int, payload: bytes) -> None:
        self._require_len(payload, 1)
        index = payload[0]
        if not self._valid_index(index):
            return self._bad_param(seq)
        state = P.PinState(
            index=index,
            dir=self.directions[index],
            level=self.input_level(index),
            flags=self.flags[index],
            drive=self.drives[index],
            slew=self.slews[index],
        )
        self._emit(P.ST_OK, seq, state.pack())

    def _op_configure_pin(self, seq: int, payload: bytes) -> None:
        self._require_len(payload, P.CFG_PIN_STRUCT.size)
        cfg = P.CfgPin.unpack(payload)
        if not self._apply_cfg(cfg):
            return self._bad_param(seq)
        self._emit(P.ST_OK, seq, b"")

    def _op_set_pin(self, seq: int, payload: bytes) -> None:
        self._require_len(payload, P.SET_PIN_STRUCT.size)
        item = P.SetPin.unpack(payload)
        if not self._apply_set(item):
            return self._bad_param(seq)
        self._emit(P.ST_OK, seq, b"")

    def _op_set_mask(self, seq: int, payload: bytes) -> None:
        self._require_len(payload, P.SET_MASK_STRUCT.size)
        req = P.SetMask.unpack(payload)
        # firmware harness_gpio_set_mask(): out-of-range bits are masked off,
        # not rejected; one store to the SIO latch = atomic.
        mask = req.mask & P.ALL_PINS_MASK
        new_value = (self.output_levels() & ~mask) | (req.value & mask)
        for i in range(self.pin_count):
            self.out_latch[i] = (new_value >> i) & 1
        self._emit(P.ST_OK, seq, struct.pack("<I", self.output_levels()))

    def _op_read_all(self, seq: int, payload: bytes) -> None:
        self._require_len(payload, 0)
        value = 0
        for i in range(self.pin_count):
            value |= (self.input_level(i) & 1) << i
        self._emit(P.ST_OK, seq, struct.pack("<I", value))

    def _op_set_many(self, seq: int, payload: bytes) -> None:
        if len(payload) == 0 or len(payload) % P.SET_PIN_STRUCT.size:
            raise _BadLength
        count = len(payload) // P.SET_PIN_STRUCT.size
        if not 1 <= count <= P.SET_MANY_MAX:
            raise _BadLength
        items = [
            P.SetPin.unpack(payload[i * 4 : i * 4 + 4]) for i in range(count)
        ]
        for item in items:
            if not self._valid_index(item.index) or item.level not in (0, 1):
                return self._bad_param(seq)
            if self.directions[item.index] != P.DIR_OUTPUT:
                return self._bad_param(seq)
        for item in items:
            self.out_latch[item.index] = item.level
        self._emit(P.ST_OK, seq, struct.pack("<B", count))

    def _op_configure_many(self, seq: int, payload: bytes) -> None:
        if len(payload) == 0 or len(payload) % P.CFG_PIN_STRUCT.size:
            raise _BadLength
        count = len(payload) // P.CFG_PIN_STRUCT.size
        if not 1 <= count <= P.CONFIGURE_MANY_MAX:
            raise _BadLength
        cfgs = [
            P.CfgPin.unpack(payload[i * 8 : i * 8 + 8]) for i in range(count)
        ]
        for cfg in cfgs:
            if not self._apply_cfg(cfg):
                return self._bad_param(seq)
        self._emit(P.ST_OK, seq, struct.pack("<B", count))

    def _op_stop(self, seq: int, payload: bytes) -> None:
        self._require_len(payload, 0)
        self.streaming = False
        # firmware harness_gpio_safe_state(): INPUT, no pull, Schmitt on,
        # 4 mA, slew slow, and every own latch cleared (protocol.md §3.1/§5).
        for i in range(self.pin_count):
            self.directions[i] = P.DIR_INPUT
            self.flags[i] = P.FLAG_SCHMITT
            self.drives[i] = 1  # 4 mA
            self.slews[i] = 0
            self.out_latch[i] = 0
        self._emit(P.ST_OK, seq, b"")

    def _op_get_status(self, seq: int, payload: bytes) -> None:
        self._require_len(payload, 0)
        status = P.DeviceStatus(
            state=1 if self.streaming else 0,
            last_status=self.last_status,
            frames_ok=self.frames_ok,
            frames_err=self.frames_err,
            crc_errors=self.crc_errors,
            uptime_ms=self.uptime_ms,
        )
        self._emit(P.ST_OK, seq, status.pack())

    def _op_stream(self, seq: int, payload: bytes) -> None:
        # Mirrors the order of checks in firmware harness_protocol_handle().
        if len(payload) < P.STREAM_HDR_STRUCT.size:
            raise _BadLength
        mask, count, mode, _reserved = P.STREAM_HDR_STRUCT.unpack_from(payload, 0)
        if mode != 0 or count == 0 or count > P.STREAM_COUNT_MAX:
            return self._bad_param(seq)
        if len(payload) != P.STREAM_HDR_STRUCT.size + 4 * count:
            raise _BadLength
        masked = mask & P.ALL_PINS_MASK
        if masked == 0:
            return self._bad_param(seq)
        values = list(
            struct.unpack_from(f"<{count}I", payload, P.STREAM_HDR_STRUCT.size)
        )
        self.streaming = True
        previous = self.output_levels()
        toggles = 0
        applied = 0
        for value in values:
            # firmware harness_gpio_stream(): one atomic SIO store per value.
            new_value = (previous & ~masked) | (value & masked)
            if new_value != previous:
                toggles += bin(new_value ^ previous).count("1")
                previous = new_value
            applied += 1
        for i in range(self.pin_count):
            self.out_latch[i] = (previous >> i) & 1
        self.streaming = False
        # Report a duration consistent with ``stream_rate_mhz`` so that
        # ``updates_per_second`` is meaningful in tests without hardware.
        if self.stream_rate_mhz > 0:
            duration_us = max(int(round(applied / self.stream_rate_mhz)), 1)
        else:
            duration_us = 1
        reply = P.StreamReply(
            applied=applied, duration_us=duration_us, toggles=toggles
        )
        self._emit(P.ST_OK, seq, reply.pack())

    def _op_ping(self, seq: int, payload: bytes) -> None:
        self._require_len(payload, 4)
        self._emit(P.ST_OK, seq, payload)

    _HANDLERS: Final = {
        P.OP_GET_INFO: _op_get_info,
        P.OP_GET_PIN: _op_get_pin,
        P.OP_CONFIGURE_PIN: _op_configure_pin,
        P.OP_SET_PIN: _op_set_pin,
        P.OP_SET_MASK: _op_set_mask,
        P.OP_READ_ALL: _op_read_all,
        P.OP_SET_MANY: _op_set_many,
        P.OP_CONFIGURE_MANY: _op_configure_many,
        P.OP_STOP: _op_stop,
        P.OP_GET_STATUS: _op_get_status,
        P.OP_STREAM: _op_stream,
        P.OP_PING: _op_ping,
    }

    # -- helpers ------------------------------------------------------------

    def _require_len(self, payload: bytes, expected: int) -> None:
        if self.strict_length and len(payload) != expected:
            raise _BadLength

    def _bad_param(self, seq: int) -> None:
        self.frames_err += 1
        self._emit_status(P.ST_ERR_BAD_PARAM, seq)

    def _apply_cfg(self, cfg: P.CfgPin) -> bool:
        if not self._valid_index(cfg.index):
            return False
        if cfg.dir not in (P.DIR_INPUT, P.DIR_OUTPUT):
            return False
        if cfg.drive not in P.CODE_TO_DRIVE:
            return False
        if cfg.slew not in P.CODE_TO_SLEW:
            return False
        if cfg.level not in (0, 1):
            return False
        if cfg.flags & ~P.FLAG_MASK:
            return False
        if (cfg.flags & P.FLAG_PULL_UP) and (cfg.flags & P.FLAG_PULL_DOWN):
            return False
        if cfg.flags & P.FLAG_OPEN_DRAIN and cfg.dir != P.DIR_OUTPUT:
            return False
        index = cfg.index
        self.directions[index] = cfg.dir
        self.flags[index] = cfg.flags
        # protocol.md §4.4: for an INPUT line drive and slew are validated but
        # IGNORED (the pad keeps what it had), and the latch is forced low.
        # firmware/src/gpio_ctrl.c:harness_gpio_configure() does the same.
        if cfg.dir == P.DIR_OUTPUT:
            self.drives[index] = cfg.drive
            self.slews[index] = cfg.slew
            self.out_latch[index] = cfg.level
        else:
            self.out_latch[index] = 0
        return True

    def _apply_set(self, item: P.SetPin) -> bool:
        if not self._valid_index(item.index) or item.level not in (0, 1):
            return False
        if self.directions[item.index] != P.DIR_OUTPUT:
            return False  # protocol.md §4.7: implicit direction change forbidden
        self.out_latch[item.index] = item.level
        return True

    def _emit_status(self, status: int, seq: int) -> None:
        self.last_status = status
        self._emit(status, seq, b"")

    def _emit(self, status: int, seq: int, payload: bytes) -> None:
        self.last_status = status
        if status == P.ST_OK:
            self.frames_ok += 1
        if self.drop_replies > 0:
            self.drop_replies -= 1
            return
        out_seq = (seq + 1) & 0xFFFF if self.wrong_seq else seq
        crc = P.crc16_ccitt_false(
            struct.pack("<BHH", status, out_seq, len(payload)) + payload
        )
        if self.corrupt_crc:
            crc ^= 0xFFFF
        self._tx += struct.pack(
            "<BBHHH", P.REP_MAGIC, status, out_seq, len(payload), crc
        ) + payload


class _BadLength(Exception):
    """Internal: this opcode's payload length is wrong (-> ERR_BAD_LENGTH)."""

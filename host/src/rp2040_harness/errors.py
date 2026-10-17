"""Exception hierarchy for the RP2040 Harness host package.

Everything raised by this package derives from :class:`HarnessError`, so a
caller can write ``except HarnessError`` and be done.

The device status codes of ``docs/protocol.md`` §2.4 each get their own class
via :data:`STATUS_EXCEPTIONS` / :func:`exception_for_status`.
"""

from __future__ import annotations

from typing import Final

from . import protocol as _p

__all__ = [
    "HarnessError",
    "DeviceNotFoundError",
    "DeviceBusyError",
    "TransportError",
    "ProtocolError",
    "HarnessTimeout",
    "TimeoutError_",
    "ParameterError",
    "DeviceStatusError",
    "UnknownOpcodeError",
    "BadLengthError",
    "BadParamError",
    "DeviceBusyStatusError",
    "CrcError",
    "MagicError",
    "UnknownStatusError",
    "STATUS_EXCEPTIONS",
    "exception_for_status",
]


class HarnessError(Exception):
    """Base class for every error raised by :mod:`rp2040_harness`."""


# --------------------------------------------------------------------------
# Host-side errors
# --------------------------------------------------------------------------


class DeviceNotFoundError(HarnessError):
    """No RP2040 Harness device matched the requested VID/PID/serial."""


class DeviceBusyError(HarnessError):
    """Another process (or this one) already owns the device lock."""

    def __init__(self, message: str, *, pid: int | None = None, path: str | None = None):
        super().__init__(message)
        self.pid = pid
        self.path = path


class TransportError(HarnessError):
    """The USB layer failed (open/write/read/claim/config)."""


class HarnessTimeout(HarnessError):
    """No reply arrived within the configured timeout."""


# Verbose alias, requested by the task statement; kept for readability.
TimeoutError_ = HarnessTimeout


class ParameterError(HarnessError, ValueError):
    """A client-side argument failed validation before touching USB.

    Inherits from :class:`ValueError` as well so that callers who treat bad
    arguments as plain ``ValueError`` keep working.
    """


class ProtocolError(HarnessError):
    """A frame was malformed: bad magic, bad length, bad CRC, wrong seq."""


# --------------------------------------------------------------------------
# Device-reported status errors (protocol.md §2.4)
# --------------------------------------------------------------------------


class DeviceStatusError(HarnessError):
    """The device answered with a non-zero status code.

    Attributes
    ----------
    status:
        Numeric status code from the reply frame.
    status_name:
        Protocol name of the status code, e.g. ``"ERR_BAD_PARAM"``.
    opcode / opcode_name:
        The request opcode that produced this status (when known).
    seq:
        Sequence number of the offending request (when known).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int,
        opcode: int | None = None,
        seq: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.status_name = _p.status_name(status)
        self.opcode = opcode
        self.opcode_name = _p.opcode_name(opcode) if opcode is not None else None
        self.seq = seq


class UnknownOpcodeError(DeviceStatusError):
    """``ERR_UNKNOWN_OPCODE`` — opcode not supported by this firmware."""


class BadLengthError(DeviceStatusError):
    """``ERR_BAD_LENGTH`` — payload length does not match the opcode."""


class BadParamError(DeviceStatusError):
    """``ERR_BAD_PARAM`` — pin out of range or a field value is invalid."""


class DeviceBusyStatusError(DeviceStatusError):
    """``ERR_BUSY`` — the device is busy (for example, a STREAM is running)."""


class CrcError(DeviceStatusError):
    """``ERR_CRC`` — the device rejected the frame CRC."""


class MagicError(DeviceStatusError):
    """``ERR_MAGIC`` — the device did not see the request magic byte."""


class UnknownStatusError(DeviceStatusError):
    """A status code this host build does not know about."""


STATUS_EXCEPTIONS: Final[dict[int, type[DeviceStatusError]]] = {
    _p.ST_ERR_UNKNOWN_OPCODE: UnknownOpcodeError,
    _p.ST_ERR_BAD_LENGTH: BadLengthError,
    _p.ST_ERR_BAD_PARAM: BadParamError,
    _p.ST_ERR_BUSY: DeviceBusyStatusError,
    _p.ST_ERR_CRC: CrcError,
    _p.ST_ERR_MAGIC: MagicError,
}


def exception_for_status(
    status: int,
    *,
    opcode: int | None = None,
    seq: int | None = None,
    detail: str | None = None,
) -> DeviceStatusError:
    """Build (do not raise) the exception matching a device status code."""
    cls = STATUS_EXCEPTIONS.get(status, UnknownStatusError)
    name = _p.status_name(status)
    parts = [f"device returned {name} (0x{status:02X})"]
    if opcode is not None:
        parts.append(f"for {_p.opcode_name(opcode)}")
    if seq is not None:
        parts.append(f"seq={seq}")
    message = " ".join(parts)
    if detail:
        message = f"{message}: {detail}"
    message += _HINTS.get(status, "")
    return cls(message, status=status, opcode=opcode, seq=seq)


_HINTS: Final[dict[int, str]] = {
    _p.ST_ERR_UNKNOWN_OPCODE: (
        " — the firmware is older than the host package; update the firmware."
    ),
    _p.ST_ERR_BAD_LENGTH: (
        " — host and firmware disagree about this frame's payload; check versions."
    ),
    _p.ST_ERR_BAD_PARAM: (
        " — check pin index (0..23), level (0/1), pull/drive/slew values, and that "
        "the pin is configured as OUTPUT before set_pin()."
    ),
    _p.ST_ERR_BUSY: " — wait for the current operation (e.g. STREAM) to finish.",
    _p.ST_ERR_CRC: " — USB corruption; retry, and check cable/EMI if it persists.",
    _p.ST_ERR_MAGIC: " — the device lost frame sync; reopen the device.",
}

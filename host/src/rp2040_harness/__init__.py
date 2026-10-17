"""RP2040 Harness host package.

PC-side control of a Raspberry Pi Pico W driving 24 digital lines over USB
raw-vendor (TinyUSB) + pyusb/libusb.

Quick start::

    from rp2040_harness import Harness

    with Harness.open() as h:
        h.configure(3, direction="out", drive="8mA", slew="fast",
                    schmitt=True, pull=None, open_drain=False)
        h.write_mask(mask=0x0000FF, value=0x0000A5)
        print(h.read_all())
        h.batch([("set", 3, 1), ("set", 4, 0)])

The wire protocol is ``docs/protocol.md`` v1.0; :mod:`rp2040_harness.protocol`
implements it byte for byte.

Testing without hardware::

    from rp2040_harness import FakeTransport, Harness

    with Harness.open(transport=FakeTransport(), lock=False) as h:
        h.configure(3, direction="out")
        h.set_pin(3, 1)
"""

from __future__ import annotations

from .device import (
    BATCH_OPCODES,
    DRIVE_VALUES,
    PULL_VALUES,
    SLEW_VALUES,
    Harness,
    StreamResult,
)
from .errors import (
    BadLengthError,
    BadParamError,
    CrcError,
    DeviceBusyError,
    DeviceBusyStatusError,
    DeviceNotFoundError,
    DeviceStatusError,
    HarnessError,
    HarnessTimeout,
    MagicError,
    ParameterError,
    ProtocolError,
    TimeoutError_,
    TransportError,
    UnknownOpcodeError,
    UnknownStatusError,
    exception_for_status,
)
from .locking import DeviceLock, default_lock_path, read_lock_owner
from . import protocol
from .protocol import (
    ALL_PINS_MASK,
    CONFIGURE_MANY_MAX,
    PIN_COUNT,
    SET_MANY_MAX,
    STREAM_COUNT_MAX,
    Info,
    PinState,
    DeviceStatus,
    CfgPin,
    SetPin,
    SetMask,
    StreamHeader,
    StreamReply,
    Reply,
    crc16_ccitt_false,
    decode_reply,
    encode_request,
)
from .transport import (
    DEFAULT_PID,
    DEFAULT_VID,
    FakeTransport,
    PyUsbTransport,
    Transport,
    list_devices,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # core
    "Harness",
    "StreamResult",
    # protocol records / helpers
    "protocol",
    "encode_request",
    "decode_reply",
    "crc16_ccitt_false",
    "Reply",
    "Info",
    "PinState",
    "DeviceStatus",
    "CfgPin",
    "SetPin",
    "SetMask",
    "StreamHeader",
    "StreamReply",
    "PIN_COUNT",
    "ALL_PINS_MASK",
    "SET_MANY_MAX",
    "CONFIGURE_MANY_MAX",
    "STREAM_COUNT_MAX",
    # transport
    "Transport",
    "PyUsbTransport",
    "FakeTransport",
    "list_devices",
    "DEFAULT_VID",
    "DEFAULT_PID",
    # locking
    "DeviceLock",
    "default_lock_path",
    "read_lock_owner",
    # validation vocabularies
    "PULL_VALUES",
    "DRIVE_VALUES",
    "SLEW_VALUES",
    "BATCH_OPCODES",
    # errors
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
    "exception_for_status",
]

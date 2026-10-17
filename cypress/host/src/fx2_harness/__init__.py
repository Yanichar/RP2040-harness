"""Cypress FX2LP (CY7C68013A) harness host package.

A second, controllable 24-line harness next to the RP2040 one.  It speaks the
*frozen* wire protocol v1.0 (``docs/protocol.md``) byte for byte -- the codec,
the ``Transport`` ABC, the ``Harness`` API and the error classes are all reused
from :mod:`rp2040_harness`; only the USB plumbing is new:

* :mod:`fx2_harness.transport` -- the chunked-EP0 :class:`Transport`
* :mod:`fx2_harness.loader`    -- find ``04B4:8613``, download the ``.ihx``,
  wait for ``04B4:8614``
* :mod:`fx2_harness.harness`   -- ``Harness.open()`` -> the shared API

Quick start::

    from fx2_harness import Harness

    with Harness.open() as h:
        print(h.get_info().protocol_version)      # "1.0"
        h.configure(3, direction="out")
        h.set_pin(3, 1)
        print(hex(h.read_all()))                  # 0x8

Load the firmware first (``python -m fx2_harness load``); the board starts out
as the unconfigured EZ-USB boot device ``04B4:8613``.
"""

from __future__ import annotations

from rp2040_harness import protocol
from rp2040_harness.errors import (
    BadLengthError,
    BadParamError,
    DeviceBusyError,
    DeviceNotFoundError,
    DeviceStatusError,
    HarnessError,
    HarnessTimeout,
    ParameterError,
    ProtocolError,
    TransportError,
)
from rp2040_harness.protocol import ALL_PINS_MASK, PIN_COUNT

from .harness import Harness, open_harness
from .loader import (
    LoadResult,
    default_image_path,
    find_app_device,
    find_boot_device,
    load_firmware,
    parse_ihx,
    reset_to_boot,
    wait_for,
)
from .transport import (
    APP_PID,
    APP_VID,
    BOOT_PID,
    BOOT_VID,
    CHUNK_SIZE,
    MAX_FRAME,
    ControlTransport,
    find_device,
    list_devices,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # core
    "Harness",
    "open_harness",
    "ControlTransport",
    # discovery / loading
    "APP_VID",
    "APP_PID",
    "BOOT_VID",
    "BOOT_PID",
    "CHUNK_SIZE",
    "MAX_FRAME",
    "list_devices",
    "find_device",
    "find_app_device",
    "find_boot_device",
    "load_firmware",
    "reset_to_boot",
    "wait_for",
    "parse_ihx",
    "default_image_path",
    "LoadResult",
    # protocol re-exports
    "protocol",
    "PIN_COUNT",
    "ALL_PINS_MASK",
    # errors
    "HarnessError",
    "DeviceNotFoundError",
    "DeviceBusyError",
    "TransportError",
    "ProtocolError",
    "HarnessTimeout",
    "ParameterError",
    "DeviceStatusError",
    "BadLengthError",
    "BadParamError",
]

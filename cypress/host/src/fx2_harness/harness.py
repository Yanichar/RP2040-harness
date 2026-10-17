"""The programmer-facing entry point: :func:`Harness.open`.

``fx2_harness.Harness.open()`` finds the Cypress board, wraps it in a
:class:`~fx2_harness.transport.ControlTransport` and returns the *unchanged*
:class:`rp2040_harness.device.Harness` bound to that transport::

    from fx2_harness import Harness

    with Harness.open() as h:
        h.configure(3, direction="out")
        h.set_pin(3, 1)
        print(h.read_all())

Every method of the RP2040 API therefore works as-is (``configure``,
``set_pin``, ``write_mask``, ``read_all``, ``batch``, ``stream``, ``stop``,
``status``, ``get_info``, ...), and the wire protocol on the cable is the
frozen v1.0 frame format from ``docs/protocol.md``.
"""

from __future__ import annotations

from rp2040_harness import Harness as _BaseHarness

from .transport import (
    APP_PID,
    APP_VID,
    ControlTransport,
    find_device,
    list_devices,
)

__all__ = ["Harness", "open_harness", "list_devices"]


class Harness:
    """Factory for an :class:`rp2040_harness.Harness` on the Cypress board."""

    @staticmethod
    def open(
        *,
        vid: int = APP_VID,
        pid: int = APP_PID,
        timeout_ms: int = 1000,
        device=None,
        backend=None,
    ) -> _BaseHarness:
        """Open the Cypress board and return the shared Harness API.

        Parameters
        ----------
        vid, pid:
            USB ids; the defaults are the application firmware's ``04B4:8614``.
        timeout_ms:
            Reply timeout, default 1000 ms (protocol.md §2.5).
        device:
            An already-open ``usb.core.Device`` (tests, or a device picked
            from :func:`list_devices`).
        backend:
            libusb backend override.

        The returned object is ``rp2040_harness.Harness.with_transport(...,
        lock=False)``: the file lock of the RP2040 tools is intentionally not
        taken, because that lock file belongs to the RP2040 device.
        """
        usb_device = find_device(vid, pid, device=device, backend=backend)
        try:
            usb_device.set_configuration()
        except Exception:  # noqa: BLE001 - redundant on an enumerated device
            pass
        transport = ControlTransport(usb_device, timeout_ms=timeout_ms)
        return _BaseHarness.with_transport(transport, timeout_ms=timeout_ms, lock=False)


def open_harness(**kwargs) -> _BaseHarness:
    """Shortcut for :meth:`Harness.open`."""
    return Harness.open(**kwargs)

"""A fake FX2LP that speaks the chunked-EP0 transport, in memory.

It is the firmware's mirror image: it accepts ``REQ_OUT_CHUNK`` /
``REQ_EXEC`` / ``REQ_REPLY_LEN`` / ``REQ_IN_CHUNK`` control transfers and
delegates the assembled frame to :class:`rp2040_harness.transport.FakeTransport`,
which is the *reference* implementation of the protocol.  The tests therefore
check the Cypress transport against the RP2040 device behaviour, not against a
second hand-written mock.
"""

from __future__ import annotations

from fx2_harness.transport import (
    BM_IN,
    BM_OUT,
    MAX_FRAME,
    REQ_EXEC,
    REQ_IN_CHUNK,
    REQ_OUT_CHUNK,
    REQ_REPLY_LEN,
)
from rp2040_harness.transport import FakeTransport


class FakeUsbError(Exception):
    """Stand-in for ``usb.core.USBError``."""


class FakeFx2Device:
    """``usb.core.Device``-shaped object with ``ctrl_transfer``."""

    def __init__(self, *, reply_len_override=None, **fake_kwargs):
        self.inner = FakeTransport(**fake_kwargs)
        self.rx = bytearray()
        self.tx = bytearray()
        self.calls: list[tuple[int, int, int, int, object]] = []
        self.fail_next = None
        self.reply_len_override = reply_len_override
        self.bus = 2
        self.address = 42
        self.configurations = 0

    # -- usb.core.Device surface -------------------------------------------

    def set_configuration(self, *_a, **_k):  # pragma: no cover - trivial
        self.configurations += 1

    def ctrl_transfer(
        self, bmRequestType, bRequest, wValue=0, wIndex=0, data_or_wLength=None,
        timeout=None,
    ):
        self.calls.append(
            (bmRequestType, bRequest, wValue, wIndex, data_or_wLength)
        )
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

        if bmRequestType == BM_OUT and bRequest == REQ_OUT_CHUNK:
            data = bytes(data_or_wLength or b"")
            if len(data) != wIndex:
                raise FakeUsbError(f"wIndex {wIndex} != {len(data)} data bytes")
            if len(data) > 64:
                raise FakeUsbError("chunk larger than one EP0 packet")
            if wValue + len(data) > MAX_FRAME:
                raise FakeUsbError("chunk outside the frame buffer")
            need = wValue + len(data)
            if len(self.rx) < need:
                self.rx.extend(b"\x00" * (need - len(self.rx)))
            self.rx[wValue:need] = data
            return len(data)

        if bmRequestType == BM_OUT and bRequest == REQ_EXEC:
            frame = bytes(self.rx[:wValue])
            if len(frame) != wValue:
                raise FakeUsbError("REQ_EXEC for bytes that were never sent")
            self.tx = bytearray()
            self.inner.write(frame)             # FakeTransport parses + replies
            while True:
                piece = self.inner.read(MAX_FRAME)
                if not piece:
                    break
                self.tx += piece
            self.rx = bytearray()
            return wValue

        if bmRequestType == BM_IN and bRequest == REQ_REPLY_LEN:
            length = (
                self.reply_len_override
                if self.reply_len_override is not None
                else len(self.tx)
            )
            raw = bytes([length & 0xFF, (length >> 8) & 0xFF])
            return raw[:data_or_wLength] if data_or_wLength else raw

        if bmRequestType == BM_IN and bRequest == REQ_IN_CHUNK:
            want = int(data_or_wLength)
            piece = bytes(self.tx[wValue : wValue + want])
            if len(piece) != want:
                raise FakeUsbError(
                    f"host asked for {want} bytes at {wValue}, only "
                    f"{len(self.tx)} are buffered"
                )
            return piece

        raise FakeUsbError(
            f"unexpected control transfer bmRequestType=0x{bmRequestType:02X} "
            f"bRequest=0x{bRequest:02X}"
        )

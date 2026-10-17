"""Offline tests for the chunked-EP0 :class:`ControlTransport`.

The transport is exercised against :class:`fake_device.FakeFx2Device`, which
implements the firmware side of the chunked transport and delegates the
protocol itself to ``rp2040_harness.transport.FakeTransport`` (the reference
implementation).  Nothing here needs USB hardware.
"""

from __future__ import annotations

import struct

import pytest

from fx2_harness.transport import (
    CHUNK_SIZE,
    MAX_FRAME,
    REQ_EXEC,
    REQ_IN_CHUNK,
    REQ_OUT_CHUNK,
    REQ_REPLY_LEN,
    ControlTransport,
)
from fake_device import FakeFx2Device, FakeUsbError
from rp2040_harness import protocol as P
from rp2040_harness.device import Harness
from rp2040_harness.errors import BadParamError, TransportError


def make_pair(**kwargs):
    device = FakeFx2Device(**kwargs)
    return device, ControlTransport(device, timeout_ms=500)


def make_harness(**kwargs):
    device, transport = make_pair(**kwargs)
    return device, Harness.with_transport(transport, timeout_ms=500)


# --------------------------------------------------------------------------
# framing / chunking
# --------------------------------------------------------------------------


def test_write_splits_into_ep0_chunks_then_exec_():
    device, transport = make_pair()
    frame = P.encode_request(P.OP_PING, 7, struct.pack("<I", 0xDEADBEEF))

    written = transport.write(frame)

    assert written == len(frame)
    out_chunks = [c for c in device.calls if c[1] == REQ_OUT_CHUNK]
    assert len(out_chunks) == 1, "a 12-byte frame fits in one EP0 packet"
    assert [c[2] for c in out_chunks] == [0]
    assert all(len(c[4]) <= CHUNK_SIZE for c in out_chunks)
    # ... then REQ_EXEC, then the reply is pulled immediately.
    kinds = [c[1] for c in device.calls]
    assert kinds[0] == REQ_OUT_CHUNK
    assert kinds[1] == REQ_EXEC
    assert kinds[2] == REQ_REPLY_LEN
    assert device.calls[1][2] == len(frame)
    assert device.calls[1][4] is None
    # Only frame bytes travel in the data stages.
    assert b"".join(bytes(c[4]) for c in out_chunks) == frame


def test_write_of_a_full_size_frame_is_split_every_64_bytes():
    device, transport = make_pair()
    payload = bytes(range(256)) * 4  # 1024 bytes -> 1032-byte frame
    frame = P.encode_request(P.OP_STREAM, 1, payload)

    transport.write(frame)

    chunks = [c for c in device.calls if c[1] == REQ_OUT_CHUNK]
    assert len(chunks) == (MAX_FRAME + CHUNK_SIZE - 1) // CHUNK_SIZE == 17
    assert [len(c[4]) for c in chunks] == [CHUNK_SIZE] * 16 + [MAX_FRAME - 16 * CHUNK_SIZE]
    assert bytes(b"".join(bytes(c[4]) for c in chunks)) == frame
    assert [c[1] for c in device.calls][17] == REQ_EXEC


def test_write_rejects_an_oversized_frame():
    device, transport = make_pair()
    with pytest.raises(TransportError, match="exceeds the protocol maximum"):
        transport.write(b"\xA5" * (MAX_FRAME + 1))


def test_write_rejects_an_empty_frame():
    _, transport = make_pair()
    with pytest.raises(TransportError, match="empty frame"):
        transport.write(b"")


# --------------------------------------------------------------------------
# replies
# --------------------------------------------------------------------------


def test_read_returns_the_decoded_reply_frame():
    device, transport = make_pair()
    transport.write(P.encode_request(P.OP_PING, 9, b"\x01\x02\x03\x04"))

    assert any(c[1] == REQ_REPLY_LEN for c in device.calls)
    assert any(c[1] == REQ_IN_CHUNK for c in device.calls)

    reply = P.decode_reply(transport.read(MAX_FRAME))
    assert reply.status == P.ST_OK
    assert reply.seq == 9
    assert reply.payload == b"\x01\x02\x03\x04"


def test_read_of_a_larger_reply_needs_several_in_chunks():
    # GET_INFO carries a 32-byte payload = 40-byte frame, so two IN chunks.
    device, transport = make_pair()
    transport.write(P.encode_request(P.OP_GET_INFO, 3))

    in_chunks = [c for c in device.calls if c[1] == REQ_IN_CHUNK]
    assert len(in_chunks) == 1  # 40 bytes fit in one 64-byte EP0 packet
    reply = P.decode_reply(transport.read(MAX_FRAME))
    assert reply.seq == 3
    info = P.Info.unpack(reply.payload)
    assert info.pin_count == P.PIN_COUNT


def test_read_without_a_pending_reply_returns_nothing():
    _, transport = make_pair()
    assert transport.read(8) == b""


def test_replies_are_handed_out_in_request_order():
    """``Harness.stream()`` writes the next request before reading the last
    reply; the transport must not mix the two."""
    device, transport = make_pair()
    first = P.encode_request(P.OP_PING, 1, b"\x01\x00\x00\x00")
    second = P.encode_request(P.OP_PING, 2, b"\x02\x00\x00\x00")

    transport.write(first)
    transport.write(second)

    reply1 = P.decode_reply(transport.read(MAX_FRAME))
    reply2 = P.decode_reply(transport.read(MAX_FRAME))
    assert (reply1.seq, reply2.seq) == (1, 2)
    assert reply1.payload == b"\x01\x00\x00\x00"
    assert reply2.payload == b"\x02\x00\x00\x00"


def test_zero_length_reply_is_a_transport_error():
    device, transport = make_pair()
    device.reply_len_override = 0
    with pytest.raises(TransportError, match="empty reply frame"):
        transport.write(P.encode_request(P.OP_PING, 1, b"\x00\x00\x00\x00"))


def test_reply_length_that_does_not_match_the_buffer_is_a_transport_error():
    device, transport = make_pair()
    device.reply_len_override = 64  # claims more than the device buffered
    with pytest.raises(TransportError, match="bRequest=0x23"):
        transport.write(P.encode_request(P.OP_PING, 1, b"\x00\x00\x00\x00"))


def test_usb_errors_become_transport_errors():
    device, transport = make_pair()
    device.fail_next = FakeUsbError("pipe stalled")
    with pytest.raises(TransportError, match="pipe stalled"):
        transport.write(P.encode_request(P.OP_PING, 1, b"\x00\x00\x00\x00"))


def test_closed_transport_refuses_traffic():
    _, transport = make_pair()
    transport.close()
    transport.close()  # idempotent
    with pytest.raises(TransportError, match="closed"):
        transport.write(P.encode_request(P.OP_PING, 1, b"\x00\x00\x00\x00"))
    with pytest.raises(TransportError, match="closed"):
        transport.read(8)


# --------------------------------------------------------------------------
# the whole RP2040 API on top of the Cypress transport
# --------------------------------------------------------------------------


def test_harness_api_works_unchanged():
    _, h = make_harness()
    info = h.get_info(cached=False)
    assert info.protocol_version == "1.0"
    assert info.pin_count == 24
    assert info.pin_mask == P.ALL_PINS_MASK
    assert h.ping(0x12345678) == 0x12345678

    h.configure(3, direction="out", drive="8mA", slew="fast")
    h.set_pin(3, 1)
    assert h.read_all() == 1 << 3
    assert h.get_pin(3).direction == "out"

    assert h.write_mask(0x0000FF, 0x0000A5) == 0x000000A5
    h.configure(4, direction="out")
    h.configure(5, direction="out")
    h.batch([("set", 4, 1), ("set", 5, 0)])

    results = list(h.stream([0xFFFFFF, 0x000000], 0x00FFFFFF, chunk=2))
    assert [r.applied for r in results] == [2]
    assert results[0].toggles > 0
    assert results[0].duration_us > 0

    status = h.status()
    assert status.frames_ok > 0
    h.stop()
    assert all(pin.dir == P.DIR_INPUT for pin in h.list_pins())


def test_device_status_codes_raise_the_shared_exceptions():
    _, h = make_harness()
    h.stop()  # every line INPUT
    with pytest.raises(BadParamError):
        h.set_pin(0, 1)


def test_transport_reports_the_frame_length_it_wrote():
    device, transport = make_pair()
    frame = P.encode_request(P.OP_READ_ALL, 5)
    assert transport.write(frame) == len(frame)

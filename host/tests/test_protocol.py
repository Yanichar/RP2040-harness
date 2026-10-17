"""Framing, CRC and struct pack/unpack against docs/protocol.md v1.0."""

from __future__ import annotations

import struct

import pytest

from rp2040_harness import protocol as P


# --------------------------------------------------------------------------
# §2.3 CRC
# --------------------------------------------------------------------------


def test_crc16_ccitt_false_check_vector():
    """The frozen vector from protocol.md §2.3."""
    assert P.crc16_ccitt_false(b"123456789") == 0x29B1


def test_crc_variant_is_ccitt_false_not_xmodem_or_true():
    """Guard against accidentally implementing the reflected variant.

    CRC-16/XMODEM (refin/refout = false but poly/init identical) gives 0x31C3
    for the standard string, so a wrong variant would be caught here.
    """
    assert P.crc16_ccitt_false(b"123456789") != 0x31C3  # XMODEM
    assert P.crc16_ccitt_false(b"123456789") != 0x2189  # CCITT-TRUE / KERMIT
    assert P.crc16_ccitt_false(b"123456789") == 0x29B1  # CCITT-FALSE


def test_crc_matches_an_independent_table_driven_implementation():
    """Cross-check the bitwise CRC against a table-driven reference."""
    import random

    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)

    def reference(data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc = ((crc << 8) & 0xFFFF) ^ table[((crc >> 8) ^ byte) & 0xFF]
        return crc

    rng = random.Random(20240521)
    samples = [b"", b"\x00", b"\xff", bytes(range(256))]
    samples += [bytes(rng.randrange(256) for _ in range(rng.randrange(0, 64)))
                for _ in range(200)]
    for data in samples:
        assert P.crc16_ccitt_false(data) == reference(data)


def test_crc_fast_path_matches_the_bitwise_reference():
    """The shipped CRC (binascii.crc_hqx) must equal the bitwise reference.

    crc16_ccitt_false_bitwise() is the specification in executable form; the
    fast path replaced it for speed (~490x on a 1 KB frame) and must stay
    byte-identical to it.
    """
    import random

    rng = random.Random(1234)
    samples = [b"", b"\x00", b"123456789", bytes(range(256)), b"\xff" * 1024]
    samples += [bytes(rng.randrange(256) for _ in range(rng.randrange(0, 1100)))
                for _ in range(100)]
    for data in samples:
        assert P.crc16_ccitt_false(data) == P.crc16_ccitt_false_bitwise(data)

    # And it is materially faster, which is the whole reason it exists.
    import time

    payload = bytes(1016)
    start = time.perf_counter()
    for _ in range(50):
        P.crc16_ccitt_false_bitwise(payload)
    slow = time.perf_counter() - start
    start = time.perf_counter()
    for _ in range(50):
        P.crc16_ccitt_false(payload)
    fast = time.perf_counter() - start
    assert fast * 20 < slow, f"fast CRC is not faster: {fast:.4f}s vs {slow:.4f}s"



def test_crc16_ccitt_false_empty_and_known_patterns():
    assert P.crc16_ccitt_false(b"") == 0xFFFF
    assert P.crc16_ccitt_false(b"\x00") == 0xE1F0
    assert P.crc16_ccitt_false(b"A") == 0xB915


# --------------------------------------------------------------------------
# §2.1/§2.2 framing
# --------------------------------------------------------------------------


def test_encode_request_layout_and_crc():
    frame = P.encode_request(P.OP_SET_PIN, 0x1234, P.SetPin(5, 1).pack())

    assert len(frame) == 8 + 4
    assert frame[0] == 0xA5
    assert frame[1] == P.OP_SET_PIN
    assert struct.unpack_from("<H", frame, 2)[0] == 0x1234
    assert struct.unpack_from("<H", frame, 4)[0] == 4

    crc = struct.unpack_from("<H", frame, 6)[0]
    assert crc == P.crc16_ccitt_false(frame[1:6] + frame[8:])


def test_encode_request_empty_payload():
    frame = P.encode_request(P.OP_GET_INFO, 1)
    assert len(frame) == 8
    assert frame[8:] == b""
    assert struct.unpack_from("<H", frame, 4)[0] == 0


def test_encode_request_crc_covers_opcode_seq_len_and_payload():
    """Changing opcode, seq, len or payload must change the CRC."""
    base = P.encode_request(P.OP_PING, 7, struct.pack("<I", 0xDEADBEEF))
    other_op = P.encode_request(P.OP_GET_INFO, 7, struct.pack("<I", 0xDEADBEEF))
    other_seq = P.encode_request(P.OP_PING, 8, struct.pack("<I", 0xDEADBEEF))
    other_payload = P.encode_request(P.OP_PING, 7, struct.pack("<I", 0xDEADBEEE))

    crcs = {
        struct.unpack_from("<H", f, 6)[0]
        for f in (base, other_op, other_seq, other_payload)
    }
    assert len(crcs) == 4


def test_encode_request_rejects_bad_opcode_seq_and_oversize():
    with pytest.raises(ValueError):
        P.encode_request(0x100, 1)
    with pytest.raises(ValueError):
        P.encode_request(P.OP_PING, 0x10000)
    with pytest.raises(ValueError):
        P.encode_request(P.OP_STREAM, 1, bytes(P.MAX_PAYLOAD + 1))
    # Exactly at the maximum is fine (frame is 8 + 1024 bytes).
    assert len(P.encode_request(P.OP_STREAM, 1, bytes(P.MAX_PAYLOAD))) == P.MAX_FRAME


def test_decode_reply_round_trip():
    payload = struct.pack("<I", 0x0000A5A5)
    crc = P.crc16_ccitt_false(struct.pack("<BHH", P.ST_OK, 0x0042, 4) + payload)
    frame = struct.pack("<BBHHH", 0x5A, P.ST_OK, 0x0042, 4, crc) + payload

    reply = P.decode_reply(frame)
    assert reply.status == P.ST_OK
    assert reply.seq == 0x0042
    assert reply.payload == payload
    assert reply.ok is True
    assert len(reply) == 4


def test_decode_reply_rejects_truncated_bad_magic_bad_length_and_bad_crc():
    good = bytearray(P.encode_request(P.OP_PING, 3, b"\x01\x00\x00\x00"))
    good[0] = 0x5A  # make it look like a reply frame
    good[1] = P.ST_OK
    good[6:8] = struct.pack(
        "<H", P.crc16_ccitt_false(bytes(good[1:6]) + bytes(good[8:]))
    )
    frame = bytes(good)

    with pytest.raises(ValueError, match="truncated"):
        P.decode_reply(frame[:4])

    bad_magic = bytearray(frame)
    bad_magic[0] = 0xA5
    with pytest.raises(ValueError, match="magic"):
        P.decode_reply(bytes(bad_magic))

    bad_len = bytearray(frame)
    struct.pack_into("<H", bad_len, 4, 0xFFFF)
    with pytest.raises(ValueError, match="maximum"):
        P.decode_reply(bytes(bad_len))

    bad_crc = bytearray(frame)
    bad_crc[6] ^= 0xFF
    with pytest.raises(ValueError, match="CRC"):
        P.decode_reply(bytes(bad_crc))

    short_payload = bytearray(frame)
    struct.pack_into("<H", short_payload, 4, 40)
    with pytest.raises(ValueError, match="truncated"):
        P.decode_reply(bytes(short_payload))


def test_protocol_md_example_frame_set_mask():
    """protocol.md §7: SET_MASK(mask=0x0000FF, value=0x0000A5), seq=0x1234.

    The document shows the frame bytes without the CRC values; we check every
    byte it does show, and that the reply payload decodes to the applied value.
    """
    frame = P.encode_request(
        P.OP_SET_MASK, 0x1234, P.SetMask(mask=0x0000FF, value=0x0000A5).pack()
    )
    assert len(frame) == 16
    assert frame[:2] == bytes([0xA5, 0x05])
    assert frame[2:6] == bytes([0x34, 0x12, 0x08, 0x00])
    assert frame[8:] == bytes([0xFF, 0x00, 0x00, 0x00, 0xA5, 0x00, 0x00, 0x00])

    # Reply: 5A 00 34 12 04 00 <crc> A5 00 00 00
    payload = struct.pack("<I", 0x0000A5)
    crc = P.crc16_ccitt_false(struct.pack("<BHH", 0x00, 0x1234, 4) + payload)
    reply = P.decode_reply(
        struct.pack("<BBHHH", 0x5A, 0x00, 0x1234, 0x0004, crc) + payload
    )
    assert reply.seq == 0x1234
    assert struct.unpack("<I", reply.payload)[0] == 0x0000A5


# --------------------------------------------------------------------------
# Opcodes and status codes
# --------------------------------------------------------------------------


def test_opcode_constants_match_protocol_table():
    assert P.OP_GET_INFO == 0x01
    assert P.OP_GET_PIN == 0x02
    assert P.OP_CONFIGURE_PIN == 0x03
    assert P.OP_SET_PIN == 0x04
    assert P.OP_SET_MASK == 0x05
    assert P.OP_READ_ALL == 0x06
    assert P.OP_SET_MANY == 0x07
    assert P.OP_CONFIGURE_MANY == 0x08
    assert P.OP_STOP == 0x09
    assert P.OP_GET_STATUS == 0x0A
    assert P.OP_STREAM == 0x10
    assert P.OP_PING == 0x7F
    assert len(set(P.OPCODE_NAMES.values())) == len(P.OPCODE_NAMES) == 12


def test_status_constants_match_protocol_table():
    assert P.ST_OK == 0x00
    assert P.ST_ERR_UNKNOWN_OPCODE == 0x01
    assert P.ST_ERR_BAD_LENGTH == 0x02
    assert P.ST_ERR_BAD_PARAM == 0x03
    assert P.ST_ERR_BUSY == 0x04
    assert P.ST_ERR_CRC == 0x05
    assert P.ST_ERR_MAGIC == 0x06
    assert P.status_name(0x03) == "ERR_BAD_PARAM"
    assert P.status_name(0x99) == "UNKNOWN_STATUS_0x99"
    assert P.opcode_name(0x7F) == "PING"
    assert P.opcode_name(0x33) == "0x33"


def test_caps_names():
    assert P.CAPS_V1 == 0x000003FF
    names = P.caps_to_names(P.CAPS_V1)
    assert names[0] == "PIN_DIR"
    assert "RESET_IFACE" in names
    assert len(names) == 10


# --------------------------------------------------------------------------
# §4.1 info_t
# --------------------------------------------------------------------------


def test_info_struct_size_and_offsets():
    assert P.INFO_STRUCT.size == 32
    info = P.Info(
        proto_major=1,
        proto_minor=0,
        fw_major=2,
        fw_minor=3,
        fw_patch=4,
        pin_count=24,
        caps=P.CAPS_V1,
        pin_mask=P.ALL_PINS_MASK,
        board_id=0x1122334455667788,
        reset_iface=1,
    )
    raw = info.pack()
    assert len(raw) == 32
    assert raw[0] == 1 and raw[1] == 0
    assert raw[2:5] == bytes([2, 3, 4])
    assert raw[5] == 24
    assert raw[6:8] == b"\x00\x00"
    assert struct.unpack_from("<I", raw, 8)[0] == P.CAPS_V1
    assert struct.unpack_from("<I", raw, 12)[0] == 0x00FFFFFF
    assert struct.unpack_from("<Q", raw, 16)[0] == 0x1122334455667788
    assert struct.unpack_from("<I", raw, 24)[0] == 1
    assert raw[28:32] == b"\x00\x00\x00\x00"

    decoded = P.Info.unpack(raw)
    assert decoded == info
    assert decoded.firmware_version == "2.3.4"
    assert decoded.protocol_version == "1.0"
    assert decoded.serial == "1122334455667788"


def test_info_unpack_rejects_wrong_length():
    with pytest.raises(ValueError, match="info_t"):
        P.Info.unpack(b"\x00" * 31)


# --------------------------------------------------------------------------
# §4.3 pin_state_t
# --------------------------------------------------------------------------


def test_pin_state_struct_size_pack_unpack():
    assert P.PIN_STATE_STRUCT.size == 8
    state = P.PinState(
        index=5,
        dir=P.DIR_OUTPUT,
        level=1,
        flags=P.FLAG_PULL_UP | P.FLAG_SCHMITT | P.FLAG_OPEN_DRAIN,
        drive=2,
        slew=1,
    )
    raw = state.pack()
    assert len(raw) == 8
    assert raw == bytes([5, 1, 1, 0b1101, 2, 1, 0, 0])
    assert P.PinState.unpack(raw) == state

    decoded = P.PinState.unpack(raw)
    assert decoded.direction == "out"
    assert decoded.pull == "up"
    assert decoded.schmitt is True
    assert decoded.open_drain is True
    assert decoded.drive_strength == "8mA"
    assert decoded.slew_rate == "fast"


def test_pin_state_pull_variants():
    assert P.PinState(0, 0, 0, 0, 0, 0).pull is None
    assert P.PinState(0, 0, 0, P.FLAG_PULL_DOWN, 0, 0).pull == "down"
    assert P.PinState(0, 0, 0, P.FLAG_PULL_UP, 0, 0).pull == "up"


def test_pin_state_unpack_rejects_wrong_length():
    with pytest.raises(ValueError, match="pin_state_t"):
        P.PinState.unpack(b"\x00" * 7)


# --------------------------------------------------------------------------
# §4.4 cfg_pin_t
# --------------------------------------------------------------------------


def test_cfg_pin_struct_size_pack_unpack():
    assert P.CFG_PIN_STRUCT.size == 8
    cfg = P.CfgPin(
        index=23,
        dir=P.DIR_OUTPUT,
        flags=P.FLAG_PULL_DOWN | P.FLAG_SCHMITT,
        drive=3,
        slew=1,
        level=1,
    )
    raw = cfg.pack()
    assert len(raw) == 8
    assert raw == bytes([23, 1, 0b1010, 3, 1, 1, 0, 0])
    assert P.CfgPin.unpack(raw) == cfg


def test_cfg_pin_unpack_rejects_wrong_length():
    with pytest.raises(ValueError, match="cfg_pin_t"):
        P.CfgPin.unpack(b"\x00" * 6)


# --------------------------------------------------------------------------
# §4.7 set_pin_t
# --------------------------------------------------------------------------


def test_set_pin_struct_size_pack_unpack():
    assert P.SET_PIN_STRUCT.size == 4
    item = P.SetPin(index=7, level=1)
    raw = item.pack()
    assert len(raw) == 4
    assert raw == bytes([7, 1, 0, 0])
    assert P.SetPin.unpack(raw) == item
    with pytest.raises(ValueError, match="set_pin_t"):
        P.SetPin.unpack(b"\x07\x01\x00")


# --------------------------------------------------------------------------
# §4.8 set_mask_t
# --------------------------------------------------------------------------


def test_set_mask_struct_size_pack_unpack():
    assert P.SET_MASK_STRUCT.size == 8
    req = P.SetMask(mask=0x0000FF, value=0x0000A5)
    raw = req.pack()
    assert len(raw) == 8
    assert raw == bytes([0xFF, 0, 0, 0, 0xA5, 0, 0, 0])
    assert P.SetMask.unpack(raw) == req
    with pytest.raises(ValueError, match="set_mask_t"):
        P.SetMask.unpack(b"\x00" * 9)


# --------------------------------------------------------------------------
# §4.10 stream_hdr_t / stream_rep_t
# --------------------------------------------------------------------------


def test_stream_header_struct_size_pack_unpack():
    assert P.STREAM_HDR_STRUCT.size == 16
    header = P.StreamHeader(mask=0x0000FF, count=250)
    raw = header.pack()
    assert len(raw) == 16
    assert struct.unpack("<IIII", raw) == (0x0000FF, 250, 0, 0)
    assert P.StreamHeader.unpack(raw) == header
    # 16 + 4*250 = 1016 bytes, exactly the documented protocol maximum.
    assert P.STREAM_VALUES_OFFSET + 4 * P.STREAM_COUNT_MAX == 1016
    with pytest.raises(ValueError, match="stream_hdr_t"):
        P.StreamHeader.unpack(b"\x00" * 15)


def test_stream_reply_struct_size_pack_unpack_and_rate():
    assert P.STREAM_REP_STRUCT.size == 16
    reply = P.StreamReply(applied=250, duration_us=500, toggles=17)
    raw = reply.pack()
    assert len(raw) == 16
    assert struct.unpack("<IIII", raw) == (250, 500, 17, 0)
    decoded = P.StreamReply.unpack(raw)
    assert decoded == reply
    assert decoded.updates_per_second == pytest.approx(500_000.0)
    assert P.StreamReply(0, 0, 0).updates_per_second == 0.0
    with pytest.raises(ValueError, match="stream_rep_t"):
        P.StreamReply.unpack(b"\x00" * 12)


# --------------------------------------------------------------------------
# §4.11 status_t
# --------------------------------------------------------------------------


def test_status_struct_size_pack_unpack():
    assert P.STATUS_STRUCT.size == 24
    status = P.DeviceStatus(
        state=1,
        last_status=P.ST_ERR_BAD_PARAM,
        frames_ok=1000,
        frames_err=3,
        crc_errors=1,
        uptime_ms=123456,
    )
    raw = status.pack()
    assert len(raw) == 24
    assert raw[0] == 1
    assert raw[1] == P.ST_ERR_BAD_PARAM
    assert struct.unpack_from("<I", raw, 4)[0] == 1000
    assert struct.unpack_from("<I", raw, 8)[0] == 3
    assert struct.unpack_from("<I", raw, 12)[0] == 1
    assert struct.unpack_from("<I", raw, 16)[0] == 123456
    assert raw[20:24] == b"\x00\x00\x00\x00"

    decoded = P.DeviceStatus.unpack(raw)
    assert decoded == status
    assert decoded.streaming is True
    assert decoded.state_name == "streaming"
    assert decoded.last_status_name == "ERR_BAD_PARAM"
    assert P.DeviceStatus(0, 0, 0, 0, 0, 0).state_name == "idle"
    with pytest.raises(ValueError, match="status_t"):
        P.DeviceStatus.unpack(b"\x00" * 23)


# --------------------------------------------------------------------------
# Limits and value tables
# --------------------------------------------------------------------------


def test_limits_and_tables_match_protocol():
    assert P.PIN_COUNT == 24
    assert P.ALL_PINS_MASK == 0x00FFFFFF
    assert P.SET_MANY_MAX == 64
    assert P.CONFIGURE_MANY_MAX == 32
    assert P.STREAM_COUNT_MAX == 250
    assert P.MAX_PAYLOAD == 1024
    assert P.HEADER_SIZE == 8

    assert P.DRIVE_TO_CODE == {"2mA": 0, "4mA": 1, "8mA": 2, "12mA": 3}
    assert P.CODE_TO_DRIVE == {0: "2mA", 1: "4mA", 2: "8mA", 3: "12mA"}
    assert P.SLEW_TO_CODE == {"slow": 0, "fast": 1}
    assert P.CODE_TO_SLEW == {0: "slow", 1: "fast"}

    assert P.FLAG_PULL_UP == 0x01
    assert P.FLAG_PULL_DOWN == 0x02
    assert P.FLAG_OPEN_DRAIN == 0x04
    assert P.FLAG_SCHMITT == 0x08
    assert P.FLAG_MASK == 0x0F


def test_pin_index_to_gpio_table_is_not_reinvented_here():
    """§3: index i maps to a GPIO, but the table lives in docs, not code.

    The host only ever sends logical indices, so this test simply pins down
    that 24 indices exist and are contiguous.
    """
    assert list(range(P.PIN_COUNT)) == list(range(24))

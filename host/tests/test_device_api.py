"""The real Harness class driven against FakeTransport (no hardware)."""

from __future__ import annotations

import struct

import pytest

from rp2040_harness import protocol as P
from rp2040_harness.device import Harness, StreamResult
from rp2040_harness.errors import (
    BadLengthError,
    BadParamError,
    CrcError,
    DeviceBusyStatusError,
    HarnessError,
    HarnessTimeout,
    MagicError,
    ParameterError,
    ProtocolError,
    TransportError,
    UnknownOpcodeError,
)
from rp2040_harness.transport import FakeTransport


@pytest.fixture
def fake():
    return FakeTransport()


@pytest.fixture
def h(fake):
    harness = Harness.open(transport=fake, lock=False)
    yield harness
    harness.close()


# --------------------------------------------------------------------------
# Wiring: the fake must actually speak the protocol
# --------------------------------------------------------------------------


def test_roundtrip_uses_real_frames(fake, h):
    h.ping(0xCAFEF00D)
    opcode, seq, payload = fake.requests[-1]
    assert opcode == P.OP_PING
    assert seq == h._seq
    assert struct.unpack("<I", payload)[0] == 0xCAFEF00D
    # The fake received exactly one well-formed request frame.
    assert fake.last_request[0] == P.REQ_MAGIC
    assert len(fake.last_request) == 8 + 4
    assert fake.crc_errors == 0


def test_sequence_numbers_increment_and_wrap(fake, h):
    h.ping(1)
    first = h._seq
    h.ping(2)
    assert h._seq == first + 1
    h._seq = 0xFFFF
    h.ping(3)
    assert h._seq == 0


def test_ping_echo_and_random_nonce(h):
    assert h.ping(0x11223344) == 0x11223344
    nonce = h.ping()
    assert 0 <= nonce <= 0xFFFFFFFF


def test_ping_rejects_non_int_nonce(h):
    with pytest.raises(ParameterError):
        h.ping("nope")
    with pytest.raises(ParameterError):
        h.ping(-1)


def test_reply_assembled_from_partial_reads():
    """A 64-byte bulk endpoint may split a reply across several reads."""
    fake = FakeTransport(max_read_chunk=3)
    with Harness.open(transport=fake, lock=False) as h:
        info = h.get_info()
        assert info.pin_count == 24  # 32-byte payload arrived in 3-byte pieces
        assert h.ping(0xABCDEF01) == 0xABCDEF01


def test_reply_assembled_when_header_is_split():
    fake = FakeTransport(max_read_chunk=1)
    with Harness.open(transport=fake, lock=False) as h:
        assert h.ping(0x0BADF00D) == 0x0BADF00D
        assert h.read_all() == 0


def test_seq_mismatch_is_a_protocol_error(fake, h):
    fake.wrong_seq = True
    with pytest.raises(ProtocolError, match="sequence mismatch"):
        h.ping(1)


def test_reply_crc_error_is_a_protocol_error(fake, h):
    fake.corrupt_crc = True
    with pytest.raises(ProtocolError, match="CRC"):
        h.ping(1)


def test_timeout_when_reply_never_arrives(fake):
    fake.drop_replies = 1
    h = Harness.open(transport=fake, lock=False, timeout_ms=50)
    try:
        with pytest.raises(HarnessTimeout, match="no reply"):
            h.ping(1)
    finally:
        h.close()


def test_timeout_ms_must_be_positive(fake):
    h = Harness.open(transport=fake, lock=False)
    try:
        with pytest.raises(ParameterError):
            h.timeout_ms = 0
        h.timeout_ms = 250
        assert h.timeout_ms == 250
    finally:
        h.close()


def test_write_failure_propagates_as_transport_error(fake):
    h = Harness.open(transport=fake, lock=False)
    fake.fail_next_write = TransportError("cable yanked")
    try:
        with pytest.raises(TransportError, match="cable yanked"):
            h.ping(1)
    finally:
        h.close()


def test_requests_after_close_raise(fake):
    h = Harness.open(transport=fake, lock=False)
    h.close()
    assert h.closed
    with pytest.raises(HarnessError, match="closed"):
        h.ping(1)
    h.close()  # idempotent


def test_context_manager_closes(fake):
    with Harness.open(transport=fake, lock=False) as h:
        assert not h.closed
    assert h.closed


# --------------------------------------------------------------------------
# get_info / status
# --------------------------------------------------------------------------


def test_get_info(h):
    info = h.get_info()
    assert info.proto_major == 1 and info.proto_minor == 0
    assert info.pin_count == 24
    assert info.pin_mask == 0x00FFFFFF
    assert info.caps == P.CAPS_V1
    assert info.reset_iface == 1
    assert len(info.serial) == 16
    assert info.serial == "E6605A1B2C3D4E5F"
    assert "PIN_DIR" in info.capabilities


def test_get_info_caching(fake, h):
    h.get_info()
    h.get_info()
    assert len(fake.requests) == 1
    h.get_info(cached=False)
    assert len(fake.requests) == 2


def test_status(h):
    h.ping(1)
    status = h.status()
    assert status.state == 0
    assert status.state_name == "idle"
    assert status.last_status == P.ST_OK
    assert status.frames_ok > 0
    assert status.frames_err == 0
    assert status.crc_errors == 0


def test_stop_returns_all_lines_to_input(fake, h):
    h.configure(3, direction="out")
    h.set_pin(3, 1)
    assert h.read_pin(3) == 1
    h.stop()
    assert all(d == P.DIR_INPUT for d in fake.directions)
    # The SIO latch keeps its value, but nothing drives the pad any more.
    assert h.read_all() == 0


# --------------------------------------------------------------------------
# configure / get_pin
# --------------------------------------------------------------------------


def test_configure_writes_the_documented_cfg_pin_record(fake, h):
    h.configure(
        3,
        direction="out",
        drive="8mA",
        slew="fast",
        schmitt=True,
        pull=None,
        open_drain=False,
        level=1,
    )
    opcode, _seq, payload = fake.requests[-1]
    assert opcode == P.OP_CONFIGURE_PIN
    assert len(payload) == 8
    cfg = P.CfgPin.unpack(payload)
    assert cfg.index == 3
    assert cfg.dir == P.DIR_OUTPUT
    assert cfg.drive == P.DRIVE_TO_CODE["8mA"]
    assert cfg.slew == P.SLEW_TO_CODE["fast"]
    assert cfg.level == 1
    assert cfg.flags == P.FLAG_SCHMITT  # no pull, no open-drain
    assert fake.directions[3] == P.DIR_OUTPUT
    assert fake.out_latch[3] == 1


def test_configure_input_state(fake, h):
    h.configure(21, direction="in", pull="up", schmitt=False)
    cfg = P.CfgPin.unpack(fake.requests[-1][2])
    assert cfg.index == 21
    assert cfg.dir == P.DIR_INPUT
    assert cfg.flags == P.FLAG_PULL_UP
    assert cfg.level == 0


def test_configure_pull_down_and_none(h):
    h.configure(0, direction="in", pull="down")
    assert h.get_pin(0).pull == "down"
    h.configure(0, direction="in", pull=None)
    assert h.get_pin(0).pull is None
    h.configure(0, direction="in", pull="none")
    assert h.get_pin(0).pull is None


def test_configure_open_drain_and_schmitt(h):
    h.configure(1, direction="out", open_drain=True, schmitt=True)
    state = h.get_pin(1)
    assert state.open_drain is True
    assert state.schmitt is True


def test_configure_missing_pull_keyword_default_is_no_pull(h):
    h.configure(1, direction="in")
    assert h.get_pin(1).pull is None
    assert h.get_pin(1).schmitt is True  # protocol default, §5.1


def test_get_pin_round_trip_all_attributes(h):
    h.configure(
        23,
        direction="out",
        drive="12mA",
        slew="fast",
        schmitt=False,
        pull="down",
        open_drain=True,
        level=1,
    )
    state = h.get_pin(23)
    assert state.index == 23
    assert state.direction == "out"
    assert state.level == 1
    assert state.pull == "down"
    assert state.drive_strength == "12mA"
    assert state.slew_rate == "fast"
    assert state.schmitt is False
    assert state.open_drain is True


def test_list_pins_returns_24_states(fake, h):
    pins = h.list_pins()
    assert len(pins) == 24
    assert [p.index for p in pins] == list(range(24))
    assert all(p.direction == "in" for p in pins)
    assert len(fake.requests) == 24


# --------------------------------------------------------------------------
# configure_many
# --------------------------------------------------------------------------


def test_configure_many_single_transaction(fake, h):
    applied = h.configure_many(
        [
            {"index": 0, "direction": "out", "level": 1},
            (1, {"direction": "out", "drive": "2mA"}),
            {"index": 2, "direction": "in", "pull": "up"},
        ]
    )
    assert applied == 3
    assert len(fake.requests) == 1
    opcode, _seq, payload = fake.requests[0]
    assert opcode == P.OP_CONFIGURE_MANY
    assert len(payload) == 3 * 8
    cfgs = [P.CfgPin.unpack(payload[i * 8 : i * 8 + 8]) for i in range(3)]
    assert [c.index for c in cfgs] == [0, 1, 2]
    assert cfgs[0].dir == P.DIR_OUTPUT and cfgs[0].level == 1
    assert cfgs[1].drive == P.DRIVE_TO_CODE["2mA"]
    assert cfgs[2].flags == P.FLAG_PULL_UP | P.FLAG_SCHMITT


def test_configure_many_limits(fake, h):
    with pytest.raises(ParameterError, match="at least one"):
        h.configure_many([])
    with pytest.raises(ParameterError, match="maximum is 32"):
        h.configure_many([{"index": 0}] * (P.CONFIGURE_MANY_MAX + 1))
    assert h.configure_many([{"index": 0}] * P.CONFIGURE_MANY_MAX) == 32


def test_configure_many_rejects_bad_items(h):
    with pytest.raises(ParameterError, match="index"):
        h.configure_many([{"direction": "out"}])
    with pytest.raises(ParameterError):
        h.configure_many(["index=0"])
    with pytest.raises(ParameterError):
        h.configure_many([(0, {"direction": "out"}, "extra")])


def test_configure_many_device_rejects_pull_up_and_down(fake, h):
    """flags with both pull bits set is ERR_BAD_PARAM per §4.5."""
    payload = P.CfgPin(0, P.DIR_INPUT, P.FLAG_PULL_UP | P.FLAG_PULL_DOWN, 1, 0, 0).pack()
    with pytest.raises(BadParamError):
        h.request(P.OP_CONFIGURE_MANY, payload)


# --------------------------------------------------------------------------
# set_pin / set_pins / write_mask / read_all
# --------------------------------------------------------------------------


def test_set_pin_and_read_all(fake, h):
    for i in range(8):
        h.configure(i, direction="out")
    h.set_pin(3, 1)
    h.set_pin(4, 1)
    assert h.read_all() == 0b00011000
    assert h.read_pin(3) == 1
    assert h.read_pin(4) == 1
    assert h.read_pin(5) == 0


def test_set_pin_on_input_pin_is_bad_param(fake, h):
    """§4.7: an INPUT pin rejects SET_PIN; no implicit direction change."""
    with pytest.raises(BadParamError, match="ERR_BAD_PARAM"):
        h.set_pin(3, 1)
    assert fake.directions[3] == P.DIR_INPUT


def test_set_pins_batch(fake, h):
    for i in range(4):
        h.configure(i, direction="out")
    before = len(fake.requests)
    applied = h.set_pins({0: 1, 1: 0, 2: 1, 3: 1})
    assert applied == 4
    assert len(fake.requests) == before + 1
    opcode, _seq, payload = fake.requests[-1]
    assert opcode == P.OP_SET_MANY
    assert len(payload) == 16
    assert h.read_all() == 0b1101


def test_set_pins_limits(fake, h):
    with pytest.raises(ParameterError, match="at least one"):
        h.set_pins({})
    with pytest.raises(ParameterError, match="at least one"):
        h.set_pins([])
    # Duplicated records are legal in SET_MANY, so >24 records is possible.
    with pytest.raises(ParameterError, match="maximum is 64"):
        h.set_pins([(0, 1)] * 65)
    for i in range(24):
        h.configure(i, direction="out")
    assert h.set_pins([(0, 1)] * P.SET_MANY_MAX) == P.SET_MANY_MAX


def test_write_mask_is_atomic_and_reports_applied(fake, h):
    for i in range(8):
        h.configure(i, direction="out")
    h.write_mask(mask=0x0000FF, value=0x000000FF)
    applied = h.write_mask(mask=0x0000FF, value=0x000000A5)
    assert applied == 0x000000A5
    # Bits outside the mask keep their previous value.
    h.write_mask(mask=0x0000F0, value=0x00000000)
    assert fake.output_levels() == 0x00000005


def test_write_mask_rejects_bits_outside_24_lines(h):
    with pytest.raises(ParameterError, match="outside the 24"):
        h.write_mask(mask=0x01000000, value=0)
    with pytest.raises(ParameterError, match="outside the 24"):
        h.write_mask(mask=0x00FFFFFF, value=1 << 24)
    with pytest.raises(ParameterError, match="outside the 24"):
        h.write_mask(mask=0xFFFFFFFF, value=0)
    with pytest.raises(ParameterError, match="negative"):
        h.write_mask(mask=-1, value=0)


def test_write_mask_keyword_form_matches_documented_example(fake, h):
    h.configure(3, direction="out")
    h.write_mask(mask=0x0000FF, value=0x0000A5)
    opcode, _seq, payload = fake.requests[-1]
    assert opcode == P.OP_SET_MASK
    assert P.SetMask.unpack(payload) == P.SetMask(0x0000FF, 0x0000A5)


def test_read_all_reports_external_input_levels(fake, h):
    fake.set_input_level(0, 1)
    fake.set_input_level(23, 1)
    assert h.read_all() == (1 << 0) | (1 << 23)
    assert h.read_pins() == (1 << 0) | (1 << 23)


def test_write_mask_on_input_pins_does_not_drive_pads(fake, h):
    """§4.8: masked non-OUTPUT pins latch but do not appear on the pad."""
    applied = h.write_mask(mask=0x0000000F, value=0x0000000F)
    assert applied == 0x0000000F  # the latch value
    assert h.read_all() == 0  # nothing reaches the pads (all INPUT)


def test_write_mask_matches_firmware_latch_semantics(fake, h):
    """firmware gpio_ctrl.c: mask bits outside 24 are masked off, not refused."""
    h.configure(0, direction="out")
    applied = h.request(
        P.OP_SET_MASK, P.SetMask(mask=0x01000005, value=0x01000001).pack()
    )
    assert struct.unpack("<I", applied)[0] == 0x00000001


def test_configure_to_input_clears_the_latch(fake, h):
    """firmware harness_gpio_configure() calls gpio_put(0) on an undriven line."""
    h.configure(3, direction="out")
    h.set_pin(3, 1)
    assert h.read_pin(3) == 1
    h.configure(3, direction="in")
    assert fake.out_latch[3] == 0
    assert h.get_pin(3).level == 0


def test_stop_clears_latches_and_restores_defaults(fake, h):
    """firmware harness_gpio_safe_state()."""
    h.configure(3, direction="out", drive="12mA", slew="fast", pull="up")
    h.set_pin(3, 1)
    h.stop()
    state = h.get_pin(3)
    assert state.direction == "in"
    assert state.pull is None
    assert state.schmitt is True
    assert state.drive_strength == "4mA"
    assert state.slew_rate == "slow"
    assert fake.output_levels() == 0


# --------------------------------------------------------------------------
# batch
# --------------------------------------------------------------------------


def test_batch_homogeneous_set_is_one_transaction(fake, h):
    for i in range(6):
        h.configure(i, direction="out")
    before = len(fake.requests)
    results = h.batch([("set", 3, 1), ("set", 4, 0), ("set", 5, 1)])
    assert len(fake.requests) == before + 1
    assert fake.requests[-1][0] == P.OP_SET_MANY
    assert results == [3]
    assert h.read_all() == (1 << 3) | (1 << 5)


def test_batch_mixed_is_split_into_minimum_transactions(fake, h):
    before = len(fake.requests)
    results = h.batch(
        [
            ("configure", 0, {"direction": "out"}),
            ("configure", 1, {"direction": "out"}),
            ("set", 0, 1),
            ("set", 1, 1),
            ("configure", 2, {"direction": "in"}),
        ]
    )
    # three homogeneous runs -> three transactions
    assert len(fake.requests) == before + 3
    assert [r[0] for r in fake.requests[-3:]] == [
        P.OP_CONFIGURE_MANY,
        P.OP_SET_MANY,
        P.OP_CONFIGURE_MANY,
    ]
    assert results == [2, 2, 1]


def test_batch_respects_per_frame_record_limits(fake, h):
    ops = []
    for i in range(70):
        ops.append(("configure", i % 24, {"direction": "in"}))
    h.batch(ops)
    # 70 configure records -> ceil(70/32) = 3 frames
    frames = [r for r in fake.requests if r[0] == P.OP_CONFIGURE_MANY]
    assert len(frames) == 3
    assert len(frames[0][2]) == 32 * 8
    assert len(frames[2][2]) == 6 * 8


def test_batch_configure_dict_form_with_index(fake, h):
    results = h.batch(
        [("configure", {"index": 7, "direction": "out", "drive": "4mA"})]
    )
    assert results == [1]
    cfg = P.CfgPin.unpack(fake.requests[-1][2])
    assert cfg.index == 7 and cfg.dir == P.DIR_OUTPUT


def test_batch_empty_returns_empty(h):
    assert h.batch([]) == []


def test_batch_rejects_bad_ops(h):
    with pytest.raises(ParameterError, match="one of"):
        h.batch([("frobnicate", 1, 1)])
    with pytest.raises(ParameterError, match="takes 3 items"):
        h.batch([("set", 1)])
    with pytest.raises(ParameterError, match="mapping"):
        h.batch([("configure", 1, "out")])
    with pytest.raises(ParameterError):
        h.batch(["set 1 1"])
    with pytest.raises(ParameterError):
        h.batch([()])
    with pytest.raises(ParameterError):
        h.batch([("configure", 1, {"direction": "in"}, "junk")])


def test_batch_validates_before_sending(fake, h):
    with pytest.raises(ParameterError):
        h.batch([("set", 0, 1), ("set", 99, 0)])
    assert fake.requests == []


# --------------------------------------------------------------------------
# stream
# --------------------------------------------------------------------------


def test_stream_yields_per_chunk_results(fake, h):
    for i in range(4):
        h.configure(i, direction="out")
    results = list(h.stream(range(10), mask=0x0000000F, chunk=4))
    assert len(results) == 3  # 4 + 4 + 2
    assert [r.applied for r in results] == [4, 4, 2]
    assert [r.chunk_index for r in results] == [0, 1, 2]
    assert [r.total_applied for r in results] == [4, 8, 10]
    assert all(isinstance(r, StreamResult) for r in results)
    assert all(r.updates_per_second > 0 for r in results)
    assert all(r.duration_us >= 1 for r in results)


def test_stream_sends_documented_frames(fake, h):
    for i in range(4):
        h.configure(i, direction="out")
    before = len(fake.requests)
    list(h.stream([0x1, 0x2, 0x3], mask=0x0000000F, chunk=250))
    opcode, _seq, payload = fake.requests[before]
    assert opcode == P.OP_STREAM
    assert len(payload) == 16 + 3 * 4
    header = P.StreamHeader.unpack(payload[:16])
    assert header.mask == 0x0000000F
    assert header.count == 3
    assert header.mode == 0
    assert struct.unpack("<3I", payload[16:]) == (1, 2, 3)


def test_stream_mask_limits_which_lines_move(fake, h):
    for i in range(8):
        h.configure(i, direction="out")
    list(h.stream([0xFF, 0xFF], mask=0x0000000F, chunk=250))
    # only the low nibble is driven
    assert fake.output_levels() == 0x0000000F


def test_stream_updates_per_second_matches_protocol_formula(fake, h):
    fake.stream_rate_mhz = 2.0  # 2 updates per microsecond
    result = next(iter(h.stream(range(100), mask=0x00FFFFFF, chunk=100)))
    assert result.duration_us == 50
    assert result.updates_per_second == pytest.approx(100 / 50e-6)
    assert result.updates_per_second == pytest.approx(2_000_000.0)


def test_stream_updates_helper(fake, h):
    applied, rate = h.stream_updates(range(500), mask=0x00FFFFFF, chunk=250)
    assert applied == 500
    assert rate == pytest.approx(1_000_000.0)  # default fake rate 1 upd/us


def test_stream_toggles_counter(fake, h):
    for i in range(4):
        h.configure(i, direction="out")
    result = next(iter(h.stream([0x0, 0xF, 0x0], mask=0x0000000F, chunk=250)))
    assert result.toggles == 8  # 4 bits toggled twice


def test_stream_argument_validation(h):
    with pytest.raises(ParameterError, match="non-zero mask"):
        list(h.stream([1, 2], mask=0))
    with pytest.raises(ParameterError, match="outside the 24"):
        list(h.stream([1, 2], mask=1 << 24))
    with pytest.raises(ParameterError, match="chunk must be 1..250"):
        list(h.stream([1, 2], mask=1, chunk=0))
    with pytest.raises(ParameterError, match="chunk must be 1..250"):
        list(h.stream([1, 2], mask=1, chunk=251))
    with pytest.raises(ParameterError, match="chunk must be an int"):
        list(h.stream([1, 2], mask=1, chunk="250"))


def test_stream_empty_values_yields_nothing(fake, h):
    assert list(h.stream([], mask=0x00FF)) == []
    assert fake.requests == []


def test_stream_values_are_masked_to_24_bits(fake, h):
    next(iter(h.stream([0xFFFFFFFFFF], mask=0x00FFFFFF, chunk=1)))
    _op, _seq, payload = fake.requests[-1]
    assert struct.unpack("<I", payload[16:])[0] == 0x00FFFFFF


def test_stream_device_rejects_zero_mask_via_protocol(fake, h):
    """Bypass the client guard to prove the fake enforces §4.10."""
    with pytest.raises(BadParamError):
        h.request(
            P.OP_STREAM,
            P.StreamHeader(mask=0, count=1).pack() + struct.pack("<I", 0),
        )


# --------------------------------------------------------------------------
# Argument validation: everything must fail before any USB traffic
# --------------------------------------------------------------------------


@pytest.mark.parametrize("index", [-1, 24, 100, "3", 3.0, True])
def test_bad_pin_index(fake, h, index):
    with pytest.raises(ParameterError):
        h.configure(index, direction="in")
    with pytest.raises(ParameterError):
        h.get_pin(index)
    with pytest.raises(ParameterError):
        h.set_pin(index, 0)
    with pytest.raises(ParameterError):
        h.read_pin(index)
    assert fake.requests == []


@pytest.mark.parametrize("level", [-1, 2, 100, "1", None, 1.5])
def test_bad_level(fake, h, level):
    with pytest.raises(ParameterError):
        h.set_pin(0, level)
    assert fake.requests == []


@pytest.mark.parametrize("pull", ["UP", "pullup", "downish", 1, True])
def test_bad_pull(fake, h, pull):
    if pull in ("UP", "pullup"):
        # case/alias tolerant forms are accepted, not errors
        h.configure(0, direction="in", pull=pull)
        return
    with pytest.raises(ParameterError, match="pull"):
        h.configure(0, direction="in", pull=pull)
    assert fake.requests == []


@pytest.mark.parametrize("drive", ["1mA", "16mA", "8ma", "8", 8, None])
def test_bad_drive(fake, h, drive):
    if drive == "8ma":
        assert h.configure(0, direction="out", drive=drive) is None
        return
    with pytest.raises(ParameterError, match="drive"):
        h.configure(0, direction="out", drive=drive)
    assert fake.requests == []


@pytest.mark.parametrize("slew", ["medium", "SLOW", "0", 0, None])
def test_bad_slew(fake, h, slew):
    if slew == "SLOW":
        h.configure(0, direction="out", slew=slew)
        return
    with pytest.raises(ParameterError, match="slew"):
        h.configure(0, direction="out", slew=slew)
    assert fake.requests == []


@pytest.mark.parametrize("direction", ["both", "outputt", 1, None])
def test_bad_direction(fake, h, direction):
    with pytest.raises(ParameterError, match="direction"):
        h.configure(0, direction=direction)
    assert fake.requests == []


def test_bad_schmitt_and_open_drain(h):
    with pytest.raises(ParameterError, match="schmitt"):
        h.configure(0, direction="in", schmitt="yes")
    with pytest.raises(ParameterError, match="open_drain"):
        h.configure(0, direction="in", open_drain="yes")
    with pytest.raises(ParameterError, match="open_drain"):
        h.configure(0, direction="in", open_drain=True)


def test_all_valid_values_are_accepted(fake, h):
    for index in range(P.PIN_COUNT):
        for direction in ("in", "out"):
            for pull in (None, "up", "down", "none"):
                for drive in ("2mA", "4mA", "8mA", "12mA"):
                    for slew in ("slow", "fast"):
                        h.configure(
                            index,
                            direction=direction,
                            pull=pull,
                            drive=drive,
                            slew=slew,
                            schmitt=True,
                            open_drain=(direction == "out"),
                        )
    assert fake.frames_err == 0
    assert fake.last_status == P.ST_OK


# --------------------------------------------------------------------------
# Device-reported error mapping
# --------------------------------------------------------------------------


def test_unknown_opcode_maps(fake, h):
    with pytest.raises(UnknownOpcodeError) as excinfo:
        h.request(0x42, b"")
    assert excinfo.value.status == P.ST_ERR_UNKNOWN_OPCODE
    assert excinfo.value.status_name == "ERR_UNKNOWN_OPCODE"
    assert "0x42" in str(excinfo.value)


def test_bad_length_maps(fake, h):
    with pytest.raises(BadLengthError):
        h.request(P.OP_GET_INFO, b"\x00\x00")  # GET_INFO takes no payload


def test_forced_status_codes_map(fake, h):
    expectations = {
        P.ST_ERR_CRC: CrcError,
        P.ST_ERR_MAGIC: MagicError,
        P.ST_ERR_BUSY: DeviceBusyStatusError,
        P.ST_ERR_BAD_PARAM: BadParamError,
    }
    for status, cls in expectations.items():
        fake.force_status = status
        with pytest.raises(cls) as excinfo:
            h.ping(1)
        assert excinfo.value.status == status
    assert all(issubclass(c, HarnessError) for c in expectations.values())


def test_garbage_reply_magic_raises_protocol_error(fake, h):
    fake._tx += b"\x11" * 8
    with pytest.raises(ProtocolError, match="magic"):
        h.ping(1)


def test_malformed_status_error_is_actionable(h):
    from rp2040_harness.errors import exception_for_status

    exc = exception_for_status(P.ST_ERR_BAD_PARAM, opcode=P.OP_SET_PIN, seq=9)
    text = str(exc)
    assert "ERR_BAD_PARAM" in text
    assert "SET_PIN" in text
    assert "seq=9" in text
    assert "pin index" in text


# --------------------------------------------------------------------------
# Documented example from TASK.md §4.1 must work verbatim
# --------------------------------------------------------------------------


def test_task_md_example_works(fake):
    """The documented call shape from TASK.md §4.1, verbatim.

    Pin 4 is also configured as OUTPUT here because protocol.md §4.7 forbids
    SET_PIN on an INPUT pin -- the TASK.md snippet shows call *shape*, not a
    complete program.
    """
    with Harness.open(transport=fake, lock=False) as h:
        h.configure(
            3,
            direction="out",
            drive="8mA",
            slew="fast",
            schmitt=True,
            pull=None,
            open_drain=False,
        )
        h.write_mask(mask=0x0000FF, value=0x0000A5)
        assert fake.output_levels() == 0x0000A5
        assert h.read_pin(3) == 0  # bit 3 of 0xA5
        print(h.read_all())
        h.configure(4, direction="out")
        h.batch([("set", 3, 1), ("set", 4, 0)])
        assert h.read_pin(3) == 1
        assert h.read_pin(4) == 0


def test_high_level_helpers(fake, h):
    applied = h.all_outputs(0x0000FF)
    assert applied == 0x000000FF
    assert all(h.get_pin(i).direction == "out" for i in range(8))
    h.all_inputs()
    assert all(h.get_pin(i).direction == "in" for i in range(8))


# --------------------------------------------------------------------------
# CLI (python -m rp2040_harness ...)
# --------------------------------------------------------------------------


def _use_fake_device(monkeypatch, fake_ref: list):
    """Make ``cli.Harness.open()`` return a harness on a FakeTransport.

    ``cli.Harness`` is the same class object as ``device.Harness``, so the
    replacement must build the harness via ``with_transport`` (which is not
    patched) to avoid recursing into itself.
    """
    from rp2040_harness import cli

    def fake_open(cls, **kwargs):
        assert "vid" in kwargs and "pid" in kwargs  # CLI passes them through
        fake = FakeTransport()
        fake_ref.append(fake)
        return Harness.with_transport(fake)

    monkeypatch.setattr(cli.Harness, "open", classmethod(fake_open))


def test_cli_info(monkeypatch, capsys):
    from rp2040_harness import cli

    ref: list = []
    _use_fake_device(monkeypatch, ref)
    assert cli.main(["info"]) == cli.EXIT_CODES["ok"]
    out = capsys.readouterr().out
    assert "protocol        : 1.0" in out
    assert "pins            : 24" in out
    assert "PIN_DIR" in out


def test_cli_read_get_status_stop(monkeypatch, capsys):
    from rp2040_harness import cli

    ref: list = []
    _use_fake_device(monkeypatch, ref)

    assert cli.main(["read"]) == 0
    assert "0x000000" in capsys.readouterr().out

    # Configure pin 3 as an output through the CLI.
    assert cli.main(["configure", "3", "--dir", "out", "--drive", "8mA",
                     "--slew", "fast", "--schmitt", "--level", "1"]) == 0
    out = capsys.readouterr().out
    assert "direction   : out" in out
    assert "drive       : 8mA" in out
    assert "slew        : fast" in out
    assert "schmitt     : True" in out
    assert "level       : 1" in out

    # Each CLI run opens a fresh session, so the device is back at boot state.
    assert cli.main(["get", "3"]) == 0
    assert "direction   : in" in capsys.readouterr().out

    # SET_PIN on an INPUT pin is ERR_BAD_PARAM -> exit code 7.
    assert cli.main(["set", "3", "0"]) == cli.EXIT_CODES["device-status"] == 7
    assert "ERR_BAD_PARAM" in capsys.readouterr().err

    assert cli.main(["mask", "0x0000FF", "0x0000A5"]) == 0
    assert "applied 0x0000A5" in capsys.readouterr().out

    assert cli.main(["pins"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 25  # header + 24

    assert cli.main(["status"]) == 0
    assert "state        : idle" in capsys.readouterr().out

    assert cli.main(["stop"]) == 0
    assert "stopped" in capsys.readouterr().out


def test_cli_bench_streams(monkeypatch, capsys):
    from rp2040_harness import cli

    ref: list = []
    _use_fake_device(monkeypatch, ref)
    code = cli.main(["bench", "--values", "500", "--chunk", "100", "--counter"])
    out = capsys.readouterr().out
    assert code == 0
    assert "updates/s" in out
    assert "values        : 500" in out
    assert len(ref[0].requests) > 5


def test_cli_device_not_found_exit_code(monkeypatch, capsys):
    from rp2040_harness import cli
    from rp2040_harness.errors import DeviceNotFoundError

    def boom(cls, **kwargs):
        raise DeviceNotFoundError("no device")

    monkeypatch.setattr(cli.Harness, "open", classmethod(boom))
    assert cli.main(["info"]) == cli.EXIT_CODES["not-found"] == 2
    assert "no device" in capsys.readouterr().err


def test_cli_busy_exit_code(monkeypatch, capsys):
    from rp2040_harness import cli
    from rp2040_harness.errors import DeviceBusyError

    def boom(cls, **kwargs):
        raise DeviceBusyError("already locked by PID 4242")

    monkeypatch.setattr(cli.Harness, "open", classmethod(boom))
    assert cli.main(["read"]) == cli.EXIT_CODES["busy"] == 3
    assert "4242" in capsys.readouterr().err


def test_cli_protocol_error_exit_code(monkeypatch, capsys):
    from rp2040_harness import cli
    from rp2040_harness.errors import ProtocolError

    def boom(cls, **kwargs):
        raise ProtocolError("bad magic")

    monkeypatch.setattr(cli.Harness, "open", classmethod(boom))
    assert cli.main(["read"]) == cli.EXIT_CODES["protocol"] == 4


def test_cli_parameter_error_exit_code(capsys):
    from rp2040_harness import cli

    # argparse rejects the value before any device is touched.
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--vid", "not-a-number", "info"])
    assert excinfo.value.code == 2


def test_cli_no_lock_flag_is_honoured(monkeypatch, capsys):
    from rp2040_harness import cli

    seen = {}

    def fake_open(cls, **kwargs):
        seen.update(kwargs)
        return Harness.with_transport(FakeTransport())

    monkeypatch.setattr(cli.Harness, "open", classmethod(fake_open))
    assert cli.main(["--no-lock", "read"]) == 0
    assert seen["lock"] is False
    assert seen["vid"] == 0x2E8A and seen["pid"] == 0x10C0
    capsys.readouterr()


def test_cli_hex_argument_parsing():
    from rp2040_harness import cli

    parser = cli.build_parser()
    args = parser.parse_args(["mask", "0x0000FF", "0b1010"])
    assert args.hexmask == 0xFF
    assert args.hexvalue == 0b1010
    args = parser.parse_args(["mask", "255", "10"])
    assert args.hexmask == 255 and args.hexvalue == 10


def test_cli_version_and_exit_code_table(capsys):
    from rp2040_harness import cli

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert "rp2040-harness" in capsys.readouterr().out
    assert cli.EXIT_CODES == {
        "ok": 0,
        "error": 1,
        "not-found": 2,
        "busy": 3,
        "protocol": 4,
        "timeout": 5,
        "parameter": 6,
        "device-status": 7,
    }

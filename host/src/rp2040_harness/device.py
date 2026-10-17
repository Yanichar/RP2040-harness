"""The programmer-facing API: :class:`Harness`.

Example (TASK.md §4.1)::

    from rp2040_harness import Harness

    with Harness.open() as h:
        h.configure(3, direction="out", drive="8mA", slew="fast",
                    schmitt=True, pull=None, open_drain=False)
        h.write_mask(mask=0x0000FF, value=0x0000A5)
        print(h.read_all())
        h.batch([("set", 3, 1), ("set", 4, 0)])

Every argument is validated client-side before a byte goes to USB, so a typo
raises :class:`~rp2040_harness.errors.ParameterError` (a ``ValueError``) rather
than a device error or a corrupted frame.
"""

from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import dataclass
from typing import Final, Iterable, Iterator, Mapping, Sequence

from . import protocol as P
from .errors import (
    HarnessError,
    HarnessTimeout,
    ParameterError,
    ProtocolError,
    exception_for_status,
)
from .locking import DeviceLock
from .transport import (
    DEFAULT_PID,
    DEFAULT_VID,
    PyUsbTransport,
    Transport,
)

__all__ = [
    "Harness",
    "StreamResult",
    "PinState",
    "Info",
    "DeviceStatus",
    "Pull",
    "Direction",
    "Drive",
    "Slew",
    "PULL_VALUES",
    "DRIVE_VALUES",
    "SLEW_VALUES",
    "BATCH_OPCODES",
]

# Validated value sets, straight from protocol.md §4.4-§4.6.
PULL_VALUES: Final = (None, "up", "down", "none")
DIRECTION_VALUES: Final = ("in", "out")
DRIVE_VALUES: Final = ("2mA", "4mA", "8mA", "12mA")
SLEW_VALUES: Final = ("slow", "fast")
BATCH_OPCODES: Final = ("set", "configure")

Pull = str | None
Direction = str
Drive = str
Slew = str


@dataclass(frozen=True)
class StreamResult:
    """One chunk of a :meth:`Harness.stream` run.

    ``updates_per_second`` is computed as ``applied / (duration_us * 1e-6)``
    per protocol.md §4.10.
    """

    applied: int
    duration_us: int
    toggles: int
    chunk_index: int = 0
    total_applied: int = 0
    total_duration_us: int = 0

    @property
    def updates_per_second(self) -> float:
        """Updates per second for this chunk (``inf`` if it took zero time)."""
        if self.duration_us <= 0:
            return float("inf") if self.applied > 0 else 0.0
        return self.applied / (self.duration_us * 1e-6)

    @property
    def duration_s(self) -> float:
        return self.duration_us * 1e-6

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"chunk {self.chunk_index}: {self.applied} updates in "
            f"{self.duration_us} us = {self.updates_per_second:,.0f} upd/s, "
            f"{self.toggles} toggles"
        )


# Re-exported under nicer names for ``from rp2040_harness import Info``.
Info = P.Info
PinState = P.PinState
DeviceStatus = P.DeviceStatus


class Harness:
    """Exclusive session with one RP2040 Harness device.

    Construct with :meth:`open` (or :meth:`with_transport` for tests / custom
    transports).  The object is a context manager::

        with Harness.open() as h:
            ...
    """

    #: Reply timeout in milliseconds (protocol.md §2.5).
    DEFAULT_TIMEOUT_MS: Final = 1000

    def __init__(
        self,
        transport: Transport,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        lock: DeviceLock | None = None,
    ):
        self._transport = transport
        self._timeout_ms = int(timeout_ms)
        self._lock = lock
        self._seq = 0
        self._io_lock = threading.RLock()
        self._closed = False
        self._info: P.Info | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        *,
        vid: int = DEFAULT_VID,
        pid: int = DEFAULT_PID,
        serial: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        lock: bool = True,
        lock_path: str | None = None,
        lock_timeout_ms: int = 0,
        transport: Transport | None = None,
    ) -> Harness:
        """Find the device by VID/PID and take the exclusive lock.

        Parameters
        ----------
        vid, pid:
            USB ids; defaults per protocol.md §6 (``0x2E8A`` / ``0x10C0``).
        serial:
            Optional serial number to pick one of several devices.
        timeout_ms:
            Reply timeout, default 1000 ms.
        lock:
            Take the cross-process exclusive lock (default ``True``).  Pass
            ``False`` for read-only probing / a second diagnostics session.
        lock_path:
            Override the lock file path (mainly for tests).
        lock_timeout_ms:
            Wait up to this long for a busy lock instead of failing at once.
        transport:
            Supply an already-built transport (tests).  When omitted, a real
            :class:`~rp2040_harness.transport.PyUsbTransport` is created.
        """
        device_lock = None
        if lock:
            device_lock = DeviceLock(lock_path, timeout_ms=lock_timeout_ms)
            device_lock.acquire()
        try:
            if transport is None:
                transport = PyUsbTransport(
                    vid=vid, pid=pid, serial=serial, timeout_ms=timeout_ms
                )
        except BaseException:
            if device_lock is not None:
                device_lock.release()
            raise
        return cls(transport, timeout_ms=timeout_ms, lock=device_lock)

    @classmethod
    def with_transport(
        cls,
        transport: Transport,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        lock: bool = False,
        lock_path: str | None = None,
    ) -> Harness:
        """Build a harness on an existing transport (tests, custom USB stacks)."""
        device_lock = None
        if lock:
            device_lock = DeviceLock(lock_path)
            device_lock.acquire()
        return cls(transport, timeout_ms=timeout_ms, lock=device_lock)

    # ------------------------------------------------------------------
    # Context manager / lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> Harness:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """Release the transport and the lock.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._transport.close()
        finally:
            if self._lock is not None:
                self._lock.release()
                self._lock = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def timeout_ms(self) -> int:
        return self._timeout_ms

    @timeout_ms.setter
    def timeout_ms(self, value: int) -> None:
        self._timeout_ms = _check_timeout(value)

    @property
    def transport(self) -> Transport:
        return self._transport

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        state = "closed" if self._closed else "open"
        return f"<Harness {state} on {self._transport.description}>"

    # ------------------------------------------------------------------
    # Low-level transaction
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    def request(self, opcode: int, payload: bytes = b"") -> bytes:
        """Send one request and return the reply payload, status-checked.

        Sequence numbers are per request; replies whose ``seq`` does not match
        are rejected as :class:`~rp2040_harness.errors.ProtocolError`, and a
        non-zero device status is raised as its mapped exception class.
        """
        if self._closed:
            raise HarnessError("harness is closed; call Harness.open() again")
        with self._io_lock:
            seq = self._next_seq()
            frame = P.encode_request(opcode, seq, payload)
            self._transport.write(frame)
            reply = self._read_reply(seq, opcode)
        if reply.status != P.ST_OK:
            raise exception_for_status(reply.status, opcode=opcode, seq=seq)
        return reply.payload

    def _read_reply(self, seq: int, opcode: int) -> P.Reply:
        """Read one reply frame and check its seq / CRC / length."""
        deadline = time.monotonic() + self._timeout_ms / 1000.0
        buf = bytearray()
        header: tuple[int, int, int, int, int] | None = None
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise HarnessTimeout(
                    f"no reply to {P.opcode_name(opcode)} (seq={seq}) within "
                    f"{self._timeout_ms} ms from {self._transport.description}"
                )
            if header is None:
                need = P.HEADER_SIZE - len(buf)
            else:
                need = P.HEADER_SIZE + header[3] - len(buf)
            chunk = self._transport.read(need, remaining_ms)
            if not chunk:
                # A real bulk read blocks for ``remaining_ms``; a fake or
                # already-buffered one returns at once, so yield the CPU.
                time.sleep(0.001)
                continue
            buf += chunk
            if header is None and len(buf) >= P.HEADER_SIZE:
                header = struct.unpack_from("<BBHHH", buf, 0)
                magic = header[0]
                if magic != P.REP_MAGIC:
                    raise ProtocolError(
                        f"bad reply magic 0x{magic:02X} (expected "
                        f"0x{P.REP_MAGIC:02X}) while waiting for "
                        f"{P.opcode_name(opcode)} (seq={seq}); the device lost "
                        "frame sync -- reopen the device"
                    )
                length = header[3]
                if length > P.MAX_PAYLOAD:
                    raise ProtocolError(
                        f"reply declares {length} payload bytes, protocol "
                        f"maximum is {P.MAX_PAYLOAD}"
                    )
            if header is not None and len(buf) >= P.HEADER_SIZE + header[3]:
                break
        total = P.HEADER_SIZE + header[3]
        frame = bytes(buf[:total])
        try:
            reply = P.decode_reply(frame)
        except ValueError as exc:
            raise ProtocolError(f"malformed reply frame: {exc}") from exc
        if reply.seq != seq:
            raise ProtocolError(
                f"reply sequence mismatch: got seq={reply.seq}, expected "
                f"seq={seq} for {P.opcode_name(opcode)}. The device may be "
                "running stale firmware or another host owns it."
            )
        return reply

    # ------------------------------------------------------------------
    # Device information
    # ------------------------------------------------------------------

    def get_info(self, *, cached: bool = True) -> P.Info:
        """``GET_INFO`` — protocol version, firmware version, caps, board id."""
        if cached and self._info is not None:
            return self._info
        info = P.Info.unpack(self.request(P.OP_GET_INFO))
        self._info = info
        return info

    def ping(self, nonce: int | None = None) -> int:
        """``PING`` — round-trip check.  Returns the echoed nonce.

        A random nonce is used when none is given, so a stale reply cannot be
        mistaken for a live one.
        """
        if nonce is None:
            nonce = int.from_bytes(os_random_bytes(4), "little")
        _check_u32(nonce, "nonce")
        payload = self.request(P.OP_PING, struct.pack("<I", nonce))
        if len(payload) != 4:
            raise ProtocolError(
                f"PING reply must carry 4 bytes, got {len(payload)}"
            )
        echo = struct.unpack("<I", payload)[0]
        if echo != nonce:
            raise ProtocolError(
                f"PING echo mismatch: sent 0x{nonce:08X}, got 0x{echo:08X}"
            )
        return echo

    def status(self) -> P.DeviceStatus:
        """``GET_STATUS`` — counters and stream state."""
        return P.DeviceStatus.unpack(self.request(P.OP_GET_STATUS))

    def stop(self) -> None:
        """``STOP`` — abort any stream and return all lines to safe INPUT."""
        self.request(P.OP_STOP)

    def __iter__(self) -> Iterator[int]:
        """``for i in harness`` yields 0..23 (the harness is an index space)."""
        return iter(range(P.PIN_COUNT))

    # ------------------------------------------------------------------
    # Pin configuration
    # ------------------------------------------------------------------

    def configure(
        self,
        index: int,
        *,
        direction: str = "in",
        pull: Pull = None,
        drive: Drive = "4mA",
        slew: Slew = "slow",
        schmitt: bool = True,
        open_drain: bool = False,
        level: int = 0,
    ) -> None:
        """``CONFIGURE_PIN`` — set every attribute of one pin at once.

        ``pull=None`` and ``pull="none"`` both mean "no pull".  ``schmitt`` and
        ``pull`` apply to INPUT and OUTPUT pins alike.

        ``drive``, ``slew`` and ``level`` are documented as "applies to
        OUT = 1 only" (protocol.md §4.4); the reference firmware stores
        ``drive``/``slew`` unconditionally and drives the line to 0 whenever
        ``direction="in"``.  To avoid silently resetting a pin's stored drive
        strength, read the current state first (``get_pin``) and pass its
        values back -- which is exactly what the CLI's ``configure`` does.
        """
        cfg = _build_cfg(
            index,
            direction=direction,
            pull=pull,
            drive=drive,
            slew=slew,
            schmitt=schmitt,
            open_drain=open_drain,
            level=level,
        )
        self.request(P.OP_CONFIGURE_PIN, cfg.pack())

    def configure_many(self, configs: Sequence[Mapping | Sequence]) -> int:
        """``CONFIGURE_MANY`` — configure up to 32 pins in one transaction.

        ``configs`` is a sequence of dicts (same keys as :meth:`configure`
        plus a mandatory ``index``) or of tuples ``(index, kwargs_dict)``.
        Returns the number of pins configured.
        """
        cfgs = [_coerce_cfg(item) for item in configs]
        if not cfgs:
            raise ParameterError(
                "configure_many() needs at least one configuration "
                "(CONFIGURE_MANY carries 1..32 records)"
            )
        if len(cfgs) > P.CONFIGURE_MANY_MAX:
            raise ParameterError(
                f"configure_many() got {len(cfgs)} configurations, the "
                f"protocol maximum is {P.CONFIGURE_MANY_MAX}; split the call"
            )
        payload = b"".join(cfg.pack() for cfg in cfgs)
        applied = self.request(P.OP_CONFIGURE_MANY, payload)
        return _applied_count(applied, len(cfgs), "CONFIGURE_MANY")

    def get_pin(self, index: int) -> P.PinState:
        """``GET_PIN`` — current direction, level, pull, drive, slew, Schmitt."""
        _check_pin(index)
        return P.PinState.unpack(self.request(P.OP_GET_PIN, struct.pack("<B", index)))

    # ------------------------------------------------------------------
    # Setting and reading levels
    # ------------------------------------------------------------------

    def set_pin(self, index: int, level: int) -> None:
        """``SET_PIN`` — set one OUTPUT pin.  The pin must already be OUTPUT."""
        item = _build_set_pin(index, level)
        self.request(P.OP_SET_PIN, item.pack())

    def set_pins(self, levels: Mapping[int, int] | Sequence[Sequence]) -> int:
        """``SET_MANY`` — set up to 64 OUTPUT pins in one transaction.

        ``levels`` is either a mapping ``{index: level}`` or a sequence of
        ``(index, level)`` pairs (which allows the same pin twice, matching the
        protocol, where 1..64 ``set_pin_t`` records are legal).
        """
        if isinstance(levels, Mapping):
            pairs = list(levels.items())
        else:
            pairs = [tuple(pair) for pair in levels]
        items = [_build_set_pin(index, level) for index, level in pairs]
        if not items:
            raise ParameterError("set_pins() needs at least one pin")
        if len(items) > P.SET_MANY_MAX:
            raise ParameterError(
                f"set_pins() got {len(items)} records, the protocol maximum is "
                f"{P.SET_MANY_MAX}; split the call"
            )
        payload = b"".join(item.pack() for item in items)
        applied = self.request(P.OP_SET_MANY, payload)
        return _applied_count(applied, len(items), "SET_MANY")

    def write_mask(self, mask: int, value: int) -> int:
        """``SET_MASK`` — atomically change the bits selected by ``mask``.

        Returns the resulting 24-bit output latch value reported by the device.
        Bits outside ``0x00FFFFFF`` are rejected client-side.
        """
        _check_mask(mask, "mask")
        _check_mask(value, "value")
        payload = self.request(P.OP_SET_MASK, P.SetMask(mask=mask, value=value).pack())
        return _unpack_u32(payload, "SET_MASK applied_value")

    #: Alias used by the MCP tool list in TASK.md §4.2.
    set_pins_atomic = write_mask

    def read_pin(self, index: int) -> int:
        """Level of one pin, read through ``READ_ALL`` (single transaction)."""
        index = _check_pin(index)
        return (self.read_all() >> index) & 1

    def read_all(self) -> int:
        """``READ_ALL`` — all 24 line levels as a bitmask (bit ``i`` = pin ``i``)."""
        return _unpack_u32(self.request(P.OP_READ_ALL), "READ_ALL inputs")

    #: Alias used by the MCP tool list in TASK.md §4.2.
    read_pins = read_all

    def list_pins(self) -> list[P.PinState]:
        """``get_pin()`` for every index 0..23."""
        return [self.get_pin(i) for i in range(P.PIN_COUNT)]

    # ------------------------------------------------------------------
    # Batch
    # ------------------------------------------------------------------

    def batch(self, operations: Sequence[Sequence]) -> list[int]:
        """Apply several operations in as few USB transactions as possible.

        ``operations`` is a sequence of tuples::

            ("set", index, level)
            ("configure", index, {"direction": "out", "pull": "up", ...})

        A batch must be *homogeneous in opcode*: adjacent operations with the
        same opcode are packed into one ``SET_MANY`` / ``CONFIGURE_MANY``
        frame, and a run boundary starts a new frame.  So a mixed batch is
        **split into the minimum number of transactions** (the alternative
        offered by the task statement, and the friendlier one); each resulting
        frame carries at most 64 (SET_MANY) or 32 (CONFIGURE_MANY) records.

        Returns the list of per-transaction applied counts in order.
        """
        ops = [_parse_batch_op(op) for op in operations]
        if not ops:
            return []
        results: list[int] = []
        run_kind = ops[0][0]
        run: list = []
        for kind, item in ops:
            if kind != run_kind:
                results.append(self._flush_batch_run(run_kind, run))
                run_kind, run = kind, []
            run.append(item)
        results.append(self._flush_batch_run(run_kind, run))
        return results

    def _flush_batch_run(self, kind: str, run: list) -> int:
        if kind == "set":
            limit = P.SET_MANY_MAX
        elif kind == "configure":
            limit = P.CONFIGURE_MANY_MAX
        else:  # pragma: no cover - defensive
            raise ParameterError(f"unknown batch opcode {kind!r}")
        applied_total = 0
        for start in range(0, len(run), limit):
            chunk = run[start : start + limit]
            payload = b"".join(item.pack() for item in chunk)
            if kind == "set":
                applied = self.request(P.OP_SET_MANY, payload)
                applied_total += _applied_count(applied, len(chunk), "SET_MANY")
            else:
                applied = self.request(P.OP_CONFIGURE_MANY, payload)
                applied_total += _applied_count(
                    applied, len(chunk), "CONFIGURE_MANY"
                )
        return applied_total

    # ------------------------------------------------------------------
    # High-throughput streaming
    # ------------------------------------------------------------------

    def stream(
        self,
        values: Iterable[int],
        mask: int,
        chunk: int = 250,
    ) -> Iterator[StreamResult]:
        """Push ``values`` to the lines selected by ``mask``, chunk by chunk.

        Each chunk becomes one ``STREAM`` request: the device applies the
        values back to back with no USB traffic in the loop (protocol.md
        §4.10), which is the fastest mode the harness offers.

        Yields one :class:`StreamResult` per chunk, carrying ``applied``,
        ``duration_us``, ``toggles`` and the computed ``updates_per_second``.

        ``chunk`` must be 1..250 (``16 + 4*250 = 1016`` bytes, the protocol
        maximum).  ``values`` are truncated to 24 bits, and only bits in
        ``mask`` are ever driven.
        """
        _check_mask(mask, "mask")
        if mask == 0:
            raise ParameterError(
                "stream() requires a non-zero mask: STREAM with mask=0 is "
                "ERR_BAD_PARAM by protocol.md §4.10"
            )
        if not isinstance(chunk, int) or isinstance(chunk, bool):
            raise ParameterError(f"chunk must be an int, got {chunk!r}")
        if not 1 <= chunk <= P.STREAM_COUNT_MAX:
            raise ParameterError(
                f"chunk must be 1..{P.STREAM_COUNT_MAX}, got {chunk} "
                f"(STREAM carries at most {P.STREAM_COUNT_MAX} values)"
            )
        total_applied = 0
        total_duration_us = 0
        for result in self._stream_all(values, mask, chunk):
            total_applied += result.applied
            total_duration_us += result.duration_us
            yield result

    def _stream_all(
        self, values: Iterable[int], mask: int, chunk: int
    ) -> list[StreamResult]:
        """Run the pipelined STREAM loop and return every result.

        Software pipelining: the firmware re-arms its OUT endpoint immediately
        after each request, so it can receive chunk N+1 while chunk N's reply is
        still on the wire. Writing ahead by exactly one chunk — never more, so
        the device's single receive buffer and single reply slot cannot
        overflow — removes about one round trip of latency per chunk. TASK.md
        §3.2 asks for precisely this ("минимум ожиданий round-trip").

        The whole loop is run under the I/O lock and the results are returned
        together, rather than holding the lock across ``yield``: a consumer that
        breaks out early would otherwise leave an unread reply queued on the
        device and the lock held.
        """
        if self._closed:
            raise HarnessError("harness is closed; call Harness.open() again")

        def encode_chunk(batch) -> tuple[bytes, int]:
            out_values = [int(v) & P.ALL_PINS_MASK for v in batch]
            header = P.StreamHeader(mask=mask, count=len(out_values))
            payload = header.pack() + struct.pack(
                f"<{len(out_values)}I", *out_values
            )
            return payload, len(out_values)

        batches = iter(_chunks(values, chunk))
        results: list[StreamResult] = []
        total_applied = 0
        total_duration_us = 0

        with self._io_lock:
            pending: tuple[int, int] | None = None
            first = next(batches, None)
            if first is not None:
                payload, count = encode_chunk(first)
                seq = self._next_seq()
                self._transport.write(P.encode_request(P.OP_STREAM, seq, payload))
                pending = (seq, count)

            while pending is not None:
                seq, count = pending

                # Send the next chunk BEFORE reading this reply.
                nxt = next(batches, None)
                if nxt is not None:
                    payload, next_count = encode_chunk(nxt)
                    next_seq = self._next_seq()
                    self._transport.write(
                        P.encode_request(P.OP_STREAM, next_seq, payload)
                    )
                    pending_next: tuple[int, int] | None = (next_seq, next_count)
                else:
                    pending_next = None

                reply = self._read_reply(seq, P.OP_STREAM)
                if reply.status != P.ST_OK:
                    raise exception_for_status(
                        reply.status, opcode=P.OP_STREAM, seq=seq
                    )
                result = P.StreamReply.unpack(reply.payload)
                if result.applied != count:
                    raise ProtocolError(
                        f"STREAM applied {result.applied} of {count} values "
                        f"in chunk {len(results)} (toggles={result.toggles})"
                    )
                total_applied += result.applied
                total_duration_us += result.duration_us
                results.append(
                    StreamResult(
                        applied=result.applied,
                        duration_us=result.duration_us,
                        toggles=result.toggles,
                        chunk_index=len(results),
                        total_applied=total_applied,
                        total_duration_us=total_duration_us,
                    )
                )
                pending = pending_next

        return results

    def stream_updates(
        self, values: Iterable[int], mask: int, chunk: int = 250
    ) -> tuple[int, float]:
        """Run :meth:`stream` to completion; return ``(applied, updates/s)``."""
        applied = 0
        duration_us = 0
        for result in self.stream(values, mask, chunk):
            applied = result.total_applied
            duration_us = result.total_duration_us
        if duration_us <= 0:
            return applied, float("inf") if applied else 0.0
        return applied, applied / (duration_us * 1e-6)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def all_outputs(self, levels: int, mask: int = P.ALL_PINS_MASK) -> int:
        """Configure every bit of ``mask`` as OUTPUT and drive ``levels``."""
        _check_mask(mask, "mask")
        _check_mask(levels, "levels")
        for i in range(P.PIN_COUNT):
            if mask >> i & 1:
                self.configure(i, direction="out", level=(levels >> i) & 1)
        return self.write_mask(mask, levels)

    def all_inputs(self) -> None:
        """Return every line to the safe boot state (INPUT, no pull)."""
        for i in range(P.PIN_COUNT):
            self.configure(i, direction="in", pull=None)


def os_random_bytes(n: int) -> bytes:
    """Small indirection so tests can monkeypatch the PING nonce source."""
    return os.urandom(n)


def _chunks(values: Iterable[int], size: int) -> Iterator[list[int]]:
    batch: list[int] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


# --------------------------------------------------------------------------
# Validation helpers (all raise ParameterError before any USB traffic)
# --------------------------------------------------------------------------


def _check_pin(index: int) -> int:
    if isinstance(index, bool) or not isinstance(index, int):
        raise ParameterError(
            f"pin index must be an int 0..{P.PIN_COUNT - 1}, got {index!r}"
        )
    if not 0 <= index < P.PIN_COUNT:
        raise ParameterError(
            f"pin index {index} out of range; valid indices are 0..{P.PIN_COUNT - 1} "
            "(logical indices, protocol.md §3)"
        )
    return index


def _check_level(level: int) -> int:
    if isinstance(level, bool):
        return int(level)
    if not isinstance(level, int):
        raise ParameterError(f"level must be 0 or 1, got {level!r}")
    if level not in (0, 1):
        raise ParameterError(f"level must be 0 or 1, got {level}")
    return level


def _check_direction(direction: str) -> str:
    if not isinstance(direction, str):
        raise ParameterError(f"direction must be 'in' or 'out', got {direction!r}")
    normalized = direction.strip().lower()
    if normalized in ("input",):
        normalized = "in"
    elif normalized in ("output",):
        normalized = "out"
    if normalized not in DIRECTION_VALUES:
        raise ParameterError(
            f"direction must be 'in' or 'out', got {direction!r}"
        )
    return normalized


def _check_pull(pull: Pull) -> str | None:
    if pull is None or pull == "none":
        return None
    if not isinstance(pull, str):
        raise ParameterError(f"pull must be None, 'up' or 'down', got {pull!r}")
    normalized = pull.strip().lower()
    if normalized in ("none", "off", "no", ""):
        return None
    if normalized in ("up", "pullup", "pull-up"):
        return "up"
    if normalized in ("down", "pulldown", "pull-down"):
        return "down"
    raise ParameterError(
        f"pull must be None, 'up' or 'down', got {pull!r}"
    )


def _check_drive(drive: Drive) -> str:
    if not isinstance(drive, str):
        raise ParameterError(
            f"drive must be one of {DRIVE_VALUES}, got {drive!r}"
        )
    normalized = drive.strip()
    if normalized.endswith("ma"):
        normalized = normalized[:-2] + "mA"
    aliases = {"2MA": "2mA", "4MA": "4mA", "8MA": "8mA", "12MA": "12mA"}
    normalized = aliases.get(normalized.upper(), normalized)
    if normalized not in DRIVE_VALUES:
        raise ParameterError(
            f"drive must be one of {DRIVE_VALUES}, got {drive!r}"
        )
    return normalized


def _check_slew(slew: Slew) -> str:
    if not isinstance(slew, str):
        raise ParameterError(f"slew must be 'slow' or 'fast', got {slew!r}")
    normalized = slew.strip().lower()
    if normalized not in SLEW_VALUES:
        raise ParameterError(f"slew must be 'slow' or 'fast', got {slew!r}")
    return normalized


def _check_bool(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise ParameterError(f"{name} must be a bool, got {value!r}")


def _check_mask(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParameterError(f"{name} must be an int, got {value!r}")
    if value < 0:
        raise ParameterError(f"{name} must not be negative, got {value}")
    if value > P.ALL_PINS_MASK:
        raise ParameterError(
            f"{name} = 0x{value:08X} has bits outside the 24 harness lines "
            f"(0x{P.ALL_PINS_MASK:08X}); only indices 0..23 exist"
        )
    return value


def _check_u32(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParameterError(f"{name} must be an int, got {value!r}")
    if not 0 <= value <= 0xFFFFFFFF:
        raise ParameterError(f"{name} must fit in 32 bits, got {value}")
    return value


def _check_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParameterError(f"timeout_ms must be an int, got {value!r}")
    if value <= 0:
        raise ParameterError(f"timeout_ms must be positive, got {value}")
    return value


# --------------------------------------------------------------------------
# Record construction
# --------------------------------------------------------------------------


def _build_flags(
    *, pull: Pull, schmitt: bool, open_drain: bool, direction: str
) -> int:
    normalized_pull = _check_pull(pull)
    flags = 0
    if normalized_pull == "up":
        flags |= P.FLAG_PULL_UP
    elif normalized_pull == "down":
        flags |= P.FLAG_PULL_DOWN
    if _check_bool(schmitt, "schmitt"):
        flags |= P.FLAG_SCHMITT
    open_drain = _check_bool(open_drain, "open_drain")
    if open_drain:
        if direction != "out":
            raise ParameterError(
                "open_drain=True requires direction='out' "
                "(protocol.md §4.5: open-drain is only valid on OUTPUT pins)"
            )
        flags |= P.FLAG_OPEN_DRAIN
    return flags


def _build_cfg(
    index: int,
    *,
    direction: str = "in",
    pull: Pull = None,
    drive: Drive = "4mA",
    slew: Slew = "slow",
    schmitt: bool = True,
    open_drain: bool = False,
    level: int = 0,
) -> P.CfgPin:
    index = _check_pin(index)
    direction = _check_direction(direction)
    flags = _build_flags(
        pull=pull, schmitt=schmitt, open_drain=open_drain, direction=direction
    )
    drive_code = P.DRIVE_TO_CODE[_check_drive(drive)]
    slew_code = P.SLEW_TO_CODE[_check_slew(slew)]
    level = _check_level(level)
    if direction == "in":
        level = 0
    return P.CfgPin(
        index=index,
        dir=P.DIR_OUTPUT if direction == "out" else P.DIR_INPUT,
        flags=flags,
        drive=drive_code,
        slew=slew_code,
        level=level,
    )


def _build_set_pin(index: int, level: int) -> P.SetPin:
    return P.SetPin(index=_check_pin(index), level=_check_level(level))


def _coerce_cfg(item) -> P.CfgPin:
    """Accept a dict-with-index or an ``(index, kwargs)`` tuple."""
    if isinstance(item, Mapping):
        params = dict(item)
        if "index" not in params:
            raise ParameterError(
                "each configure_many() item needs an 'index' key; got "
                f"{sorted(params)}"
            )
        index = params.pop("index")
        return _build_cfg(index, **params)
    if isinstance(item, (tuple, list)):
        if len(item) != 2:
            raise ParameterError(
                "configure_many() tuple items must be (index, {kwargs}), got "
                f"{item!r}"
            )
        index, kwargs = item
        if not isinstance(kwargs, Mapping):
            raise ParameterError(
                f"configure_many() kwargs must be a mapping, got {kwargs!r}"
            )
        return _build_cfg(index, **dict(kwargs))
    raise ParameterError(
        f"configure_many() items must be dicts or (index, kwargs) tuples, got "
        f"{item!r}"
    )


def _parse_batch_op(op) -> tuple[str, object]:
    if isinstance(op, (str, bytes)):
        raise ParameterError(
            f"batch() items must be tuples like ('set', 3, 1), got {op!r}"
        )
    try:
        parts = list(op)
    except TypeError as exc:
        raise ParameterError(
            f"batch() items must be tuples, got {op!r}"
        ) from exc
    if not parts:
        raise ParameterError("batch() item is empty")
    kind = parts[0]
    if kind not in BATCH_OPCODES:
        raise ParameterError(
            f"batch() opcode must be one of {BATCH_OPCODES}, got {kind!r}"
        )
    if kind == "set":
        if len(parts) != 3:
            raise ParameterError(
                f"batch() ('set', index, level) takes 3 items, got {parts!r}"
            )
        return kind, _build_set_pin(parts[1], parts[2])
    # configure
    if len(parts) == 2 and isinstance(parts[1], Mapping) and "index" in parts[1]:
        return kind, _coerce_cfg(parts[1])
    if len(parts) == 3 and isinstance(parts[1], int):
        if not isinstance(parts[2], Mapping):
            raise ParameterError(
                f"batch() ('configure', index, kwargs) needs a mapping, got "
                f"{parts[2]!r}"
            )
        return kind, _build_cfg(parts[1], **dict(parts[2]))
    raise ParameterError(
        f"batch() ('configure', index, {{...}}) or ('configure', {{'index':...}}) "
        f"expected, got {parts!r}"
    )


def _applied_count(payload: bytes, expected: int, what: str) -> int:
    if len(payload) != 1:
        raise ProtocolError(
            f"{what} reply must carry a u8 applied count, got {len(payload)} bytes"
        )
    applied = payload[0]
    if applied != expected:
        raise ProtocolError(
            f"{what} applied {applied} of {expected} records"
        )
    return applied


def _unpack_u32(payload: bytes, what: str) -> int:
    if len(payload) != 4:
        raise ProtocolError(
            f"{what} must carry a u32, got {len(payload)} bytes"
        )
    return struct.unpack("<I", payload)[0]

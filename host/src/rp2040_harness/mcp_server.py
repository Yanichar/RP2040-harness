"""MCP (stdio) server exposing the RP2040 Harness to an AI agent.

Implements TASK.md §4.2. The tool list follows the specification::

    get_device_info, list_pins, configure_pin, set_pin, set_pins_atomic,
    read_pins, stop, status

plus ``run_batch`` for batched work and ``stream`` for a throughput
measurement, because those are the two operations that are unusable through
one-command-per-round-trip MCP calls.

Run it with::

    python -m rp2040_harness.mcp_server

or, once installed, ``rp2040-harness-mcp``.

Device ownership (TASK.md §4.3)
-------------------------------
This is a separate process that uses the same library, and therefore the same
exclusive lock file. To keep the CLI and the Python API usable while the server
is idle, the device handle is closed again after ``RP2040_HARNESS_IDLE_TIMEOUT``
seconds (default 30) without a tool call; the lock is released with it. Set the
variable to ``0`` to hold the device for the whole server lifetime.
"""

from __future__ import annotations

import atexit
import os
import threading
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .device import Harness, StreamResult
from .errors import HarnessError
from .protocol import PIN_COUNT

__all__ = ["build_server", "main"]

IDLE_TIMEOUT_ENV = "RP2040_HARNESS_IDLE_TIMEOUT"
DEFAULT_IDLE_TIMEOUT_S = 30.0


# --------------------------------------------------------------------------
# Device session
# --------------------------------------------------------------------------


class _Session:
    """Lazily opened, idle-released, exclusive handle on the device."""

    def __init__(self, idle_timeout_s: float) -> None:
        self._idle_timeout_s = idle_timeout_s
        self._lock = threading.RLock()
        self._harness: Harness | None = None
        self._timer: threading.Timer | None = None

    # -- lifecycle ---------------------------------------------------------

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _arm_timer(self) -> None:
        self._cancel_timer()
        if self._idle_timeout_s <= 0:
            return
        self._timer = threading.Timer(self._idle_timeout_s, self.release)
        self._timer.daemon = True
        self._timer.start()

    def release(self) -> None:
        with self._lock:
            self._cancel_timer()
            if self._harness is not None:
                try:
                    # Leave the lines in the documented safe state before the
                    # lock is dropped, so a later user does not inherit output
                    # drivers that are still enabled (TASK.md §3.4).
                    self._harness.all_inputs()
                except Exception:
                    pass
                try:
                    self._harness.close()
                except Exception:
                    pass
                self._harness = None

    def acquire(self) -> Harness:
        with self._lock:
            self._cancel_timer()
            if self._harness is None or self._harness.closed:
                try:
                    self._harness = Harness.open()
                except HarnessError as exc:
                    raise ToolError(str(exc)) from exc
            self._arm_timer()
            return self._harness

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._harness is not None and not self._harness.closed


def _idle_timeout() -> float:
    raw = os.environ.get(IDLE_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_IDLE_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_IDLE_TIMEOUT_S


# --------------------------------------------------------------------------
# Serialisation helpers
# --------------------------------------------------------------------------


def _info_dict(info) -> dict[str, Any]:
    return {
        "protocol_version": info.protocol_version,
        "firmware_version": info.firmware_version,
        "pin_count": info.pin_count,
        "pin_mask": f"0x{info.pin_mask:06X}",
        "caps": f"0x{info.caps:04X}",
        "capabilities": list(info.capabilities),
        "serial": info.serial,
        "reset_interface": info.reset_iface,
    }


def _pin_dict(state) -> dict[str, Any]:
    return {
        "index": state.index,
        "direction": state.direction,
        "level": state.level,
        "pull": state.pull,
        "drive": state.drive_strength,
        "slew": state.slew_rate,
        "schmitt": state.schmitt,
        "open_drain": state.open_drain,
    }


def _status_dict(status) -> dict[str, Any]:
    return asdict(status)


def _levels(value: int, count: int = PIN_COUNT) -> list[int]:
    return [(value >> i) & 1 for i in range(count)]


def _stream_dict(result: StreamResult) -> dict[str, Any]:
    return {
        "applied": result.applied,
        "duration_us": result.duration_us,
        "toggles": result.toggles,
        "updates_per_second": result.updates_per_second,
    }


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


def build_server() -> MCPServer:
    session = _Session(_idle_timeout())
    atexit.register(session.release)

    server: MCPServer = MCPServer(
        name="rp2040-harness",
        title="RP2040 Harness",
        version="0.1.0",
        instructions=(
            "Controls a Raspberry Pi Pico W driving 24 digital lines (logical "
            "indices 0..23) over USB raw-vendor. Every line starts as an INPUT "
            "with no pull. Call configure_pin before set_pin, and prefer "
            "set_pins_atomic for simultaneous line changes and run_batch for "
            "many updates in one USB transaction. The device is exclusively "
            "owned: while this server holds it, the CLI and the Python API "
            "cannot open it."
        ),
    )

    @server.tool(description="Identify the connected harness: protocol and firmware version, line count, capabilities, board serial.")
    def get_device_info() -> dict[str, Any]:
        with _guard():
            return _info_dict(session.acquire().get_info(cached=False))

    @server.tool(description="Read the current configuration and level of all 24 lines.")
    def list_pins() -> list[dict[str, Any]]:
        with _guard():
            return [_pin_dict(p) for p in session.acquire().list_pins()]

    @server.tool(description="Configure one line: direction, pull, drive strength, slew rate, Schmitt trigger, open-drain, and initial level.")
    def configure_pin(
        index: int,
        direction: str = "in",
        pull: str | None = None,
        drive: str = "4mA",
        slew: str = "slow",
        schmitt: bool = True,
        open_drain: bool = False,
        level: int = 0,
    ) -> dict[str, Any]:
        with _guard():
            harness = session.acquire()
            harness.configure(
                index,
                direction=direction,
                pull=pull,
                drive=drive,
                slew=slew,
                schmitt=schmitt,
                open_drain=open_drain,
                level=level,
            )
            return _pin_dict(harness.get_pin(index))

    @server.tool(description="Drive one line that is already configured as an output to 0 or 1.")
    def set_pin(index: int, level: int) -> dict[str, Any]:
        with _guard():
            harness = session.acquire()
            harness.set_pin(index, level)
            return _pin_dict(harness.get_pin(index))

    @server.tool(description="Atomically set the lines selected by a 24-bit mask (bit i = logical line i). All selected lines change in the same hardware cycle.")
    def set_pins_atomic(mask: int, value: int) -> dict[str, Any]:
        with _guard():
            harness = session.acquire()
            applied = harness.write_mask(mask=mask, value=value)
            return {
                "applied": f"0x{applied:06X}",
                "mask": f"0x{mask & 0xFFFFFF:06X}",
                "levels": _levels(applied),
            }

    @server.tool(description="Read all 24 lines at once. Returns the raw 24-bit value and per-line levels.")
    def read_pins() -> dict[str, Any]:
        with _guard():
            value = session.acquire().read_all()
            return {"value": f"0x{value:06X}", "levels": _levels(value)}

    @server.tool(description="Return every line to the safe state: INPUT, no pull, output drivers disabled.")
    def stop() -> dict[str, Any]:
        with _guard():
            harness = session.acquire()
            harness.stop()
            return {"stopped": True, "levels": _levels(harness.read_all())}

    @server.tool(description="Device status counters: frame counts, CRC errors, uptime.")
    def status() -> dict[str, Any]:
        with _guard():
            return _status_dict(session.acquire().status())

    @server.tool(description="Apply many pin updates in as few USB transactions as possible. Each operation is {\"op\": \"set\", \"index\": i, \"level\": 0|1} or {\"op\": \"configure\", \"index\": i, ...}.")
    def run_batch(operations: list[dict[str, Any]]) -> dict[str, Any]:
        with _guard():
            harness = session.acquire()
            packed: list[Any] = []
            for op in operations:
                kind = op.get("op")
                if kind == "set":
                    packed.append(("set", int(op["index"]), int(op["level"])))
                elif kind == "configure":
                    options = {k: v for k, v in op.items() if k not in ("op", "index")}
                    packed.append(("configure", int(op["index"]), options))
                else:
                    raise ToolError(
                        f"operation {op!r}: 'op' must be 'set' or 'configure'"
                    )
            return {"transactions": harness.batch(packed), "operations": len(packed)}

    @server.tool(description="Push a sequence of 24-bit values through the lines as fast as USB allows and report the measured update rate. Intended for throughput measurement, not for correctness checking.")
    def stream(values: list[int], mask: int = 0xFFFFFF, chunk: int = 250) -> dict[str, Any]:
        with _guard():
            harness = session.acquire()
            chunks = list(harness.stream(values, mask=mask, chunk=chunk))
            total_applied = sum(c.applied for c in chunks)
            total_us = sum(c.duration_us for c in chunks)
            return {
                "chunks": len(chunks),
                "applied": total_applied,
                "duration_us": total_us,
                "updates_per_second": (total_applied / (total_us * 1e-6)) if total_us else None,
                "per_chunk": [_stream_dict(c) for c in chunks[:8]],
            }

    return server


class _guard:
    """Turn library exceptions into explicit, actionable tool errors."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            return False
        if isinstance(exc, ToolError):
            return False
        if isinstance(exc, HarnessError):
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc
        if isinstance(exc, (ValueError, TypeError)):
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc
        return False


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    build_server().run("stdio")


if __name__ == "__main__":
    main()

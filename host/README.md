# rp2040-harness (host)

PC-side Python API for the **RP2040 Harness**: a Raspberry Pi Pico W that drives
24 digital GPIO lines under live control from a PC over USB raw-vendor
(TinyUSB) + `pyusb`/`libusb`.

* Wire protocol: [`../docs/protocol.md`](../docs/protocol.md) v1.0 (frozen).
  `rp2040_harness.protocol` implements it byte for byte, and the record
  layouts/opcodes/status codes were cross-checked against
  `../firmware/src/protocol.h` and `../firmware/src/gpio_ctrl.c`.
* No hardware required to develop or test: `FakeTransport` is an in-memory
  device that speaks the real protocol (and mirrors the reference firmware's
  validation order and latch semantics).

## Install

```powershell
# Windows: a project venv
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install pyusb libusb-package
& .\.venv\Scripts\python.exe -m pip install -e .
```

`libusb-package` ships the libusb-1.0 DLL inside the wheel, so on Windows no
manual libusb install is needed. The firmware advertises Microsoft OS 2.0
(WCID) descriptors, so Windows binds WinUSB automatically — no Zadig.

Optional extras:

```powershell
python -m pip install -e ".[mcp]"    # MCP server for AI agents
python -m pip install -e ".[test]"   # pytest
```

## Use

```python
from rp2040_harness import Harness

with Harness.open() as h:                        # find device by VID/PID
    h.configure(3, direction="out", drive="8mA", slew="fast", schmitt=True,
                pull=None, open_drain=False)
    h.write_mask(mask=0x0000FF, value=0x0000A5)  # atomic, 24 bits
    print(h.read_all())                          # 24 input bits
    h.batch([("set", 3, 1), ("set", 4, 0)])      # one transaction
```

High-throughput streaming yields one result per `STREAM` chunk:

```python
for r in h.stream(values, mask=0x0000FF, chunk=250):
    print(r.applied, r.duration_us, r.toggles, f"{r.updates_per_second:,.0f} upd/s")
```

`batch()` accepts `("set", index, level)` and `("configure", index, kwargs)`
tuples.  A batch must be homogeneous in opcode, so a **mixed batch is split
into the minimum number of transactions** (one `SET_MANY`/`CONFIGURE_MANY` per
run of equal opcodes, at most 64 or 32 records per frame); the return value is
the list of per-transaction applied counts.  A homogeneous batch of ≤64 (sets)
or ≤32 (configures) entries is always exactly one USB transaction.

Everything is openable without hardware:

```python
from rp2040_harness import FakeTransport, Harness

with Harness.open(transport=FakeTransport(), lock=False) as h:
    h.configure(3, direction="out")
    h.set_pin(3, 1)
```

`Harness.open(lock=True)` (the default) takes a cross-process exclusive file
lock, so the API and the MCP server can never drive the device at the same
time. The lock file lives at `%LOCALAPPDATA%\rp2040-harness\device.lock` on
Windows and `~/.cache/rp2040-harness/device.lock` on POSIX.

## CLI

```powershell
python -m rp2040_harness info
python -m rp2040_harness pins
python -m rp2040_harness get 3
python -m rp2040_harness read
python -m rp2040_harness configure 3 --dir out --drive 8mA --slew fast --schmitt
python -m rp2040_harness set 3 1
python -m rp2040_harness mask 0x0000FF 0x0000A5
python -m rp2040_harness status
python -m rp2040_harness stop
python -m rp2040_harness bench --values 25000 --counter
```

Exit codes: `2` device not found, `3` device busy (lock held), `4` protocol
error, `5` timeout, `6` bad parameter, `7` device-reported status error.

## Tests

```powershell
& .\.venv\Scripts\python.exe -m pytest tests -q
```

The suite exercises the real `Harness` against `FakeTransport`, the CRC test
vector, every opcode's pack/unpack, all argument validation, and the
cross-process lock.

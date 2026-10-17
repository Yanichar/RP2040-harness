# Cypress FX2LP harness (`cypress/`)

A **second controllable 24-line harness** built on a Cypress **CY7C68013A
(EZ-USB FX2LP)** board, next to the RP2040 one in this repository.  It speaks
the *frozen* wire protocol v1.0 (`../docs/protocol.md`) **byte for byte**, so a
single Python API drives both boards and they can be wired pin-to-pin and
verified against each other:

```python
from fx2_harness import Harness        # rp2040_harness.Harness underneath

with Harness.open() as h:              # 04B4:8614
    h.configure(3, direction="out")
    h.set_pin(3, 1)
    assert h.read_all() & 8
    print(h.get_info().protocol_version)   # "1.0"
```

The codec, the `Transport` ABC, the `Harness` API and every error class are
**reused from `rp2040_harness`**; only the USB plumbing is new.  Nothing in
`firmware/`, `host/`, `tests/` or `docs/` of the RP2040 project was modified.

```
cypress/
  README.md                    this file
  firmware/
    build.ps1                  SDCC build -> build/harness_fx2.ihx
    src/harness_fx2.c          the firmware (protocol v1.0 + chunked EP0)
    src/dscr.a51               USB descriptors (04B4:8614, one vendor interface)
    build/                     generated: include/, *.rel, *.ihx, *.bin, *.map
  host/
    pyproject.toml             package "fx2-harness" (module fx2_harness)
    src/fx2_harness/
      __init__.py              public API
      loader.py                Intel HEX + 0xA0 RAM download + re-enumeration
      transport.py             ControlTransport (chunked EP0)
      harness.py               Harness.open() -> rp2040_harness.Harness
      cli.py                   python -m fx2_harness ...
      __main__.py
    tests/                     offline pytest suite (33 tests)
```

---

## 1. Hardware model

| item | value |
|---|---|
| MCU | CY7C68013A-56 (EZ-USB FX2LP), 8051 core, 16 KB RAM |
| lines | PORTA 0-7, PORTB 0-7, PORTD 0-7 = **24** |
| logical index | `0..7` → `PA0..PA7`, `8..15` → `PB0..PB7`, `16..23` → `PD0..PD7` (matches `../docs/pinmap.md`) |
| boot device | `04B4:8613` (unconfigured, RAM download target) |
| application | `04B4:8614` (this firmware; `cyfx2lp.inf` binds WinUSB by VID/PID) |
| speed | enumerates as a **high-speed** device (see §8) |
| actual hardware controls | **direction only** (IOA/OEA, IOB/OEB, IOD/OED) |

Reset state: all 24 lines **INPUT**, all output latches low, `IFCONFIG` ports
mode, `PORTACFG = 0` (PA0/PA1/PA6/PA7 stay GPIO).  The firmware re-applies that
state on boot and on every bus reset (`STOP` semantics), i.e. a USB reset always
leaves the board safe.

`GET_INFO` reports the truth about the hardware:

```
caps     = 0x000001C1   PIN_DIR | ATOMIC_MASK | STREAM | BATCH
pin_mask = 0x00FFFFFF
pin_count= 24
firmware = 1.0.0  proto 1.0
board_id = 0x4658324C50484E31   (ASCII "FX2LPHN1", a stable *model* id)
reset_iface = 0
```

There is no pull-up/pull-down, no open-drain mode, no drive-strength, slew or
Schmitt-trigger control on these pins, so `PULL`, `OPEN_DRAIN`,
`DRIVE_STRENGTH`, `SLEW` and `SCHMITT` are **clear** in `caps`.  The firmware
still *stores* `flags`, `drive`, `slew` and serves them back from `GET_PIN`
(§4.4 of the protocol requires "read back what was written") — they simply do
not change the pad.

---

## 2. Chunked EP0 transport

The board has **no bulk endpoints**: the interface descriptor declares
`bNumEndpoints = 0`, because everything runs over endpoint zero.  A protocol
frame is up to `8 + 1024 = 1032` bytes (§2.5) while EP0 moves 64 bytes per
transfer, so both sides implement this chunked transport (firmware:
`harness_vendorcommand()` in `src/harness_fx2.c`; host:
`fx2_harness.transport.ControlTransport`):

| bmRequestType | bRequest | wValue | wIndex | data stage | meaning |
|---|---|---|---|---|---|
| `0x40` | `0x20` `REQ_OUT_CHUNK` | byte offset into the RX frame | byte count (1..64) | OUT, `count` bytes | append to the frame buffer |
| `0x40` | `0x21` `REQ_EXEC` | total frame length (8..1032) | 0 | none | run the protocol dispatcher, build the reply |
| `0xC0` | `0x22` `REQ_REPLY_LEN` | 0 | 0 | IN, 2 bytes | `u16` reply frame length |
| `0xC0` | `0x23` `REQ_IN_CHUNK` | byte offset into the TX frame | byte count (1..64) | IN, `count` bytes | read reply bytes |

Everything is little-endian, exactly like the protocol.  A 1032-byte frame is
17 chunks (`ceil(1032/64)`); every request costs `chunks + 1` control transfers
plus `1 + ceil(reply/64)` to collect the reply.

Diagnostic / recovery extensions (not part of protocol v1.0, and not needed by
the API):

| bmRequestType | bRequest | data | meaning |
|---|---|---|---|
| `0x40` | `0x28` `REQ_RESET_TO_BOOT` | none | give USB back to the EZ-USB core → `04B4:8613` (CLI `boot`) |
| `0xC0` | `0x29` `REQ_DEBUG` | IN 16 bytes | `OEA,OEB,OED, IOA,IOB,IOD, transport_errors, last_status, ticks_per_ms(u16), sof_count(u32), streaming, high_speed` (CLI `debug`) |

### 2.1 The control-OUT data stage (the tricky part)

fx2lib calls `handle_vendorcommand()` from the SUDAV handler **before** the data
stage of a control transfer would arrive, and a vendor request that carries data
therefore has to start that data stage explicitly.  Per the FX2 TRM ("Endpoint
Zero") writing the expected length to `EP0BCH:EP0BCL` arms EP0; `EP0CS` bit 1
(`bmEPBUSY`) stays set until the packet has arrived; afterwards
`EP0BCH:EP0BCL` holds the received byte count and the bytes are in `EP0BUF[]`.
The firmware does exactly that, with a bounded wait (≈50 000 iterations, then
`EP0` is stalled so the host sees a hard error instead of a hang):

```c
EP0BCH = SETUPDAT[7];          /* wLength high */
EP0BCL = SETUPDAT[6];          /* wLength low: this write arms the data stage */
while (EP0CS & bmEPBUSY) { if (++guard > 50000) return 0xFFFF; }
got = EP0BCL;                  /* actual byte count */
for (i = 0; i < got; i++) rx_frame[off + i] = EP0BUF[i];
```

The reply side is a plain IN transfer: fill `EP0BUF[]`, `EP0BCH = 0`,
`EP0BCL = n`; fx2lib acknowledges the status stage after the handler returns.

### 2.2 Why the host pulls the reply inside `write()`

The device keeps its reply in RAM until the host collects it, so a second
`REQ_EXEC` would overwrite it.  `rp2040_harness.Harness.stream()` deliberately
writes chunk *N+1* before reading reply *N*, so `ControlTransport.write()`
collects the whole reply immediately and queues it; `read()` serves the queue
strictly in request order.  A reply never bleeds into the next frame.

---

## 3. Build

Requires SDCC (`C:\Program Files\SDCC\bin`) and an **fx2lib** checkout with
`include/` + `lib/` (default:
`C:\Users\Yanichar\work\JS-CY7C68013A-master-VV55\vendor\fx2lib-master`,
override with `-Fx2Lib <dir>` or `$env:FX2LIB`).

```powershell
powershell.exe -ExecutionPolicy Bypass -File cypress\firmware\build.ps1
```

The script

1. copies fx2lib's headers into `firmware/build/include` and patches the
   pre-SDCC-4.x `__at 0x80 + 0` bit-address syntax to `__at(0x80+0)` (same fix as
   the reference `build.py`);
2. compiles fx2lib (`delay`, `eputils`, `setupdat`, `serial`, `i2c`, `gpif`, all
   interrupt stubs) plus `usbav.a51` into `build/lib/fx2.lib`;
3. assembles `src/dscr.a51`, compiles `src/harness_fx2.c`;
4. links with

   ```
   -mmcs51 --code-size 0x3000 --xram-loc 0x3000 --xram-size 0x0E00
   -Wl-b DSCR_AREA=0x3e00 -Wl-b INT2JT=0x3f00
   ```

   (each `-Wl` option must be **one argv element**, otherwise SDCC 4.5 treats
   `DSCR_AREA=0x3e00` as an input file and — annoyingly — still exits 0);
5. writes `build/harness_fx2.ihx` (the loader artifact), `harness_fx2.bin`
   (raw image, for size reports) and `harness_fx2.map`.

Memory map produced by the linker:

```
code              0x0000..0x1E9D
internal xdata    0x3000..0x3873   rx_frame (1032) + tx_frame (1032) + state
USB descriptors   0x3E00           DSCR_AREA, read by the EZ-USB core
INT2 jump table   0x3F00           fx2lib autovector glue
```

---

## 4. Load

```powershell
.venv\Scripts\python.exe -m fx2_harness load
```

`fx2_harness.loader.load_firmware()` parses the `.ihx` (checks every Intel HEX
checksum, merges contiguous records), then:

1. **boot device present** (`04B4:8613`) → download there: `0xA0` to
   `CPUCS = 0xE600` with `0x01` halts the 8051, `0xA0` with `wValue = address`
   writes ≤64-byte chunks, `0xA0` to `CPUCS` with `0x00` releases it;
2. **application device present** (`04B4:8614`) → the EZ-USB *core* still
   services those `0xA0` register writes, so the image is pushed straight
   through the core while the application runs (verified — no replug, no round
   trip through `8613`);
3. if the core refuses (some foreign firmware owns the PID) → send
   `REQ_RESET_TO_BOOT` (`0x28`), wait for `04B4:8613` and use route 1.

The released 8051 runs the image, calls `RENUMERATE_UNCOND()` and re-enumerates
as **`04B4:8614`**; the loader waits for that and the CLI verifies `GET_INFO`.
The image lives in RAM, so **after a power cycle the board is `04B4:8613`
again** and needs another `load`.

Recovery notes learned on real hardware:

* a vendor request with **no** data stage is acknowledged by the USB core, so a
  successful `ctrl_transfer()` does not prove that the 8051 ran the handler —
  only a re-enumeration or a data stage proves it;
* `REQ_RESET_TO_BOOT` holds `USBCS.DISCON` for ~0.9 s (fx2lib's `delay()` is
  calibrated for a 12 MHz machine-cycle rate; this core runs one machine cycle
  in 4 clocks, so a requested delay is ~3× longer in real time);
* if the firmware has crashed, route 2/3 still recovers the board, because the
  download register writes are handled by the core, not the 8051.

---

## 5. Host package

```powershell
.venv\Scripts\python.exe -m pip install -e cypress\host
```

* `fx2_harness.ControlTransport(device)` — the chunked-EP0 `Transport`; raises
  `rp2040_harness.errors.TransportError` on any USB failure and validates reply
  lengths.
* `fx2_harness.Harness.open(vid=0x04B4, pid=0x8614, timeout_ms=1000, device=None)`
  — returns `rp2040_harness.Harness.with_transport(ControlTransport(dev), lock=False)`,
  so `configure`, `configure_many`, `set_pin`, `set_pins`, `write_mask`,
  `read_all`, `get_pin`, `batch`, `stream`, `stop`, `status`, `ping`,
  `get_info`, `all_outputs`, `all_inputs` all work unchanged.
  The RP2040 file lock is intentionally **not** taken (it belongs to the RP2040
  device); one process per board is the caller's job.
* `fx2_harness.load_firmware(image=None)`, `parse_ihx()`, `reset_to_boot()`,
  `wait_for()`, `find_boot_device()`, `find_app_device()`, `list_devices()`.

### CLI

```
python -m fx2_harness load [image] [--no-wait] [--timeout S]
python -m fx2_harness boot                   # back to 04B4:8613
python -m fx2_harness info                   # proto 1.0, fw, caps, board id
python -m fx2_harness read                   # 24 line levels as hex + bits
python -m fx2_harness pins                   # per line: dir, level, stored flags
python -m fx2_harness set <index> <0|1>      # drive one OUTPUT line
python -m fx2_harness dir <out|in>           # direction of all 24 lines
python -m fx2_harness status                 # counters and uptime
python -m fx2_harness debug                  # extension request 0x29
python -m fx2_harness walk [--dwell MS]      # walk one high line through 0..23
python -m fx2_harness selftest               # 51 hardware checks
```

Exit codes match the RP2040 CLI vocabulary (`0` ok, `1` error, `2` not found,
`4` protocol, `5` timeout, `6` bad parameter, `7` device status).  `walk` and
`selftest` leave every line driven low / returned to INPUT respectively, so the
board stays usable after any command.

---

## 6. Protocol behaviour that is worth knowing

* **Framing / CRC** — identical to the RP2040 firmware: magic `0xA5`/`0x5A`,
  CRC-16/CCITT-FALSE (poly `0x1021`, init `0xFFFF`, over `opcode|status, seq,
  len` + payload, `"123456789" → 0x29B1`), length-checked frames, `seq` echo.
  A bad magic or impossible length answers with `seq = 0` and resets the receive
  state; a CRC mismatch answers `ERR_CRC` with the request's `seq`.
* **`SET_MASK`** — one store per port (`IOA`, `IOB`, `IOD` are separate SFRs, so
  "atomic" means *per port*; three back-to-back stores).  The reply is the
  shadow latch value.
* **`READ_ALL`** reads the pads (`IOA/IOB/IOD`), so an OUTPUT line reports what
  it drives and an INPUT line reports what the outside world drives.
* **`GET_PIN`** derives direction from `OEx` and the level from the pad register.
* **`CONFIGURE_PIN` / `CONFIGURE_MANY`** validate every field first
  (index, dir, `flags & ~0x0F`, pull conflicts, open-drain on INPUT, drive ≤3,
  slew ≤1, level ≤1; `PULL_UP|PULL_DOWN` → `ERR_BAD_PARAM`) and then apply.  For
  `dir = 0` the stored drive/slew are kept and the latch is forced low (§4.4).
* **`SET_PIN` / `SET_MANY`** refuse an INPUT line with `ERR_BAD_PARAM`
  (no implicit direction change, §4.7) and validate all records before writing
  any.
* **`STOP`** makes all lines INPUT with latches low and also resets the stored
  flags to "Schmitt on, 4 mA, slow" so that `GET_PIN` matches the RP2040 boot
  state (§5.1); a USB bus reset runs the same path.
* **`STREAM`** writes `values[i]` one atomic store per port, with no USB traffic
  in the loop; it validates `mode == 0`, `1 ≤ count ≤ 250`, `mask ≠ 0` and
  `len == 16 + 4·count` and returns `applied`, `duration_us`, `toggles`.
* **Time base** — Timer 0 free-runs at the 8051 machine-cycle rate
  (measured **≈3956 ticks/ms**); the firmware calibrates it once against the USB
  SOF (4 ms window; 8 SOF/ms at high speed) and uses it for `duration_us`.
  `GET_STATUS.uptime_ms` is counted from SOF packets (`count/8` at high speed),
  which was verified against the host's wall clock to the millisecond.

---

## 7. Limits (measured on this board, high speed, WinUSB)

| operation | cost |
|---|---|
| one control transfer | ≈0.35 ms (WinUSB synchronous EP0) |
| `PING` / `READ_ALL` frame | ≈1.3–1.4 ms round trip |
| `STREAM`, 250 values, one line | ≈49 000 updates/s device time |
| `STREAM`, 250 values, all 24 lines | ≈15 700 updates/s device time |
| `STREAM` end to end | ≈5 000–6 600 values/s (16 EP0 chunks + reply per 250-value frame) |
| `selftest` | ≈5 s (about 60 protocol frames) |

The EP0-only design is what the brief asked for; the price is that a
250-value STREAM frame needs ~19 control transfers.  The RP2040 board, with
bulk endpoints, is orders of magnitude faster at the same protocol.  Pins, masks
and single transactions are limited purely by the 1 ms-ish USB control-transfer
latency.

---

## 8. Deviations from the brief (all deliberate, all documented above)

1. **bRequest codes** `0x20..0x23` (+`0x28`, `0x29`).  The brief fixed only the
   `bmRequestType` values (`0x40`/`0xC0`); `0x00..0x0B` are USB standard requests
   and `0xA0..0xAF` are reserved for EZ-USB core functions, so the four
   transport requests live at `0x20..0x23`.
2. **Two extra vendor requests**: `0x28` reset-to-boot and `0x29` debug.  They
   are extensions, not part of protocol v1.0, and the protocol itself is
   untouched.
3. **Loader prefers the core `0xA0` channel** over handing USB back to the core;
   the reset-to-boot route is the fallback.  Both are implemented and tested.
4. **`board_id` is a model constant** (`"FX2LPHN1"`), not a per-board serial:
   the CY7C68013A has no readable unique id without an external EEPROM, and the
   board has no serial-string descriptor.
5. **`reset_iface = 0`** and `CAP_RESET_IFACE` clear — there is no Pico-style
   reset interface; `boot` uses the vendor request instead.
6. **High speed.** `docs/protocol.md` §1 describes the RP2040's *full-speed*
   transport; this board enumerates as a high-speed device (the descriptor set
   mirrors the proven reference firmware and includes a device qualifier).  The
   wire protocol is bit-for-bit v1.0; only USB timing differs, and the firmware
   handles both speeds (`HISPEED` ↔ 8 SOF per millisecond).
7. **`SET_MASK` atomicity is per port**, and `STREAM` likewise issues three
   back-to-back SFR stores per value — the chip has one latch register per port,
   so a single 24-bit store does not exist.
8. **Stored-but-inert configuration**: `flags`/`drive`/`slew` are kept so
   `GET_PIN` reads back what was written, although the pads ignore them; `caps`
   says so explicitly.

---

## 9. Tests

```powershell
.venv\Scripts\python.exe -m pytest cypress\host\tests -q
```

* `tests/test_transport.py` — offline, against `FakeFx2Device`, which implements
  the chunked-EP0 firmware side and delegates the *protocol* to
  `rp2040_harness.transport.FakeTransport` (the reference implementation):
  chunking/offset bookkeeping, 1032-byte frames, reply ordering under
  write-ahead, error paths (`TransportError`, empty/oversized replies, closed
  transport), and the full `Harness` API on top of the Cypress transport
  (`get_info`, `ping`, `configure`, `set_pin`, `write_mask`, `batch`, `stream`,
  `status`, `stop`).
* `tests/test_loader.py` — `parse_ihx` on synthetic images (merge/gap/EOF,
  checksum, malformed records, extended addressing, the real build artifact when
  present) and the download sequence for all three loader routes.

Hardware verification (`load`, `info`, `selftest`) is described in §3–§4 and was
run against the board attached to this machine.

---

## 10. Quick start from zero

```powershell
# 1. build
powershell.exe -ExecutionPolicy Bypass -File cypress\firmware\build.ps1

# 2. install the host package once
.venv\Scripts\python.exe -m pip install -e cypress\host

# 3. load the firmware (board may be 04B4:8613 or already 04B4:8614)
.venv\Scripts\python.exe -m fx2_harness load

# 4. verify
.venv\Scripts\python.exe -m fx2_harness info
.venv\Scripts\python.exe -m fx2_harness selftest

# 5. use it
.venv\Scripts\python.exe -m fx2_harness dir out
.venv\Scripts\python.exe -m fx2_harness set 0 1
.venv\Scripts\python.exe -m fx2_harness read
```

Both harnesses from one script:

```python
from rp2040_harness import Harness as Rp2040Harness
from fx2_harness import Harness as Fx2Harness

with Rp2040Harness.open() as rp, Fx2Harness.open() as fx:
    print(hex(rp.get_info().board_id), hex(fx.get_info().board_id))
    rp.configure(0, direction="out"); fx.configure(0, direction="out")
    rp.set_pin(0, 1); assert fx.read_all() & 1     # wired PA0 <- GP2
```

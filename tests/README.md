# Bench tooling — logic-analyzer tests

Tools for the RP2040 Harness bench (TASK.md §5): the gusmanb 24-channel analyzer
observed the 24 driven lines, and these scripts turn captures into a pin map and a
throughput measurement.

Everything here is **stdlib-only Python** — no third-party runtime dependencies — so
the capture/analysis side works with the project venv, any system Python, or none.
`pytest` is the one test-time dependency (`requirements.txt`).

```
tests/
  README.md              this file
  requirements.txt       test dependencies (pytest)
  la_lib.py              .lac / .tcs reading & writing, edges, VCD/CSV export
  analyzer.py            TerminalCapture.exe driver: probe, capture, kill/recover
  pinmap_scan.py         empirical GP <-> analyzer-channel mapping
  measure.py             update rate, intervals, jitter from a capture
  fixture_gen.py         generates the synthetic fixture
  conftest.py            pytest fixtures
  test_la_lib.py         codec + limits + analyzer tests
  test_measure.py        measurement tests (recovers a known frequency)
  test_pinmap_scan.py    stepping plan + matcher tests
  fixtures/
    flat.lac             synthetic 24-bit counter (generated, committed)
  captures/              real captures land here (gitignored content, dir created on demand)
```

---

## 1. Wiring

TASK.md §2, docs/protocol.md §3:

| | |
|---|---|
| Driven lines | GP2–GP22 (21) + GP26–GP28 (3) = 24 logical indices `0..23` |
| Busy / not driven | GP23 (WL_ON), GP24 (VBUS_SENSE), GP25 (LED), GP29 (VSYS) |
| Left free | GP0, GP1 (UART debug) |
| Analyzer | gusmanb 24-channel, 100 Msps, 128 K buffer, `PA0-7` / `PB0-7` / `PD0-7` header |

**The analyzer header ports carry no channel numbers, so the GP ↔ channel wiring is
only knowable by observation.** `pinmap_scan.py` determines it; the result belongs in
`docs/pinmap.md` (TASK.md §8 item 7).

Connect the analyzer's ground to the Pico's ground. The lines are 3.3 V; do not feed
5 V into any line (docs/protocol.md §5.4).

### Analyzer readiness

The analyzer must appear as a **configured serial device** to be usable. Currently it
shows in USB as `Cypress FX2LP (unconfigured)` (04B4:8613), which exposes **no COM
port** — so nothing on this bench can be captured yet. The COM ports that exist
(COM1/COM3/COM4 motherboard/Bluetooth, COM14 an unrelated 1209:3020 USB-serial) are
**not** the analyzer; do not probe or disturb them.

Once the FX2LP loads its firmware it will enumerate as a CDC serial port. Then:

```powershell
# Ask the device, do not guess. Takes the real buffer/frequency limits from it.
& .\.venv\Scripts\python.exe tests\pinmap_scan.py --probe --port COMx
```

`--probe` runs the skill's `probe-device.ps1 -Json`, which performs the driver's own
handshake and reports firmware, channel count, buffer size, and the exact capture
limits. **Only one process may own the port**: close `LogicAnalyzer.exe` and any
running capture first.

---

## 2. The blocking-capture hazard (read this before capturing)

> `TerminalCapture capture` prints `Capture started, press Ctrl+C to stop.` and then
> **waits indefinitely for the trigger. There is no timeout.** If the trigger never
> fires it never returns.

`analyzer.capture()` therefore:

1. spawns `TerminalCapture.exe capture <port> <settings> <out>` as a **child process**
   with piped stdio — never a blocking foreground call;
2. polls for exit against a **hard deadline**;
3. on expiry **kills the process tree** (`taskkill /T /F` on Windows, which is the only
   reliable way to take down the .NET process tree);
4. **always follows a kill with the abort/reopen recovery** — a killed capture leaves
   the device *armed*, so the port refuses to handshake afterwards. The skill's
   `probe-device.ps1` sends the raw `0xFF` abort, waits 2 s, closes and reopens the
   port, and retries the handshake;
5. if recovery also fails, the device must be **unplugged and replugged** — no script
   can do that.

Also: an output file existing is *not* success. `TerminalCapture` writes it at the end,
but a killed process can leave a partial file, so the exit code is what decides.

---

## 3. Running the bench

### 3.1 Determine the pin map

```powershell
# Print the intended table, touch nothing, write nothing.
& .\.venv\Scripts\python.exe tests\pinmap_scan.py --dry-run

# Real scan: drives one line high at a time through all 24 indices while capturing.
& .\.venv\Scripts\python.exe tests\pinmap_scan.py --port COMx --write
```

The scan configures all 24 pins as outputs, starts the capture, then drives a
**stepping pattern** (`build_stepping_plan()`): a run of all-low steps, then a pulse on
logical index 0 — **that rising edge is the capture trigger** (`--trigger edge:1`), so
the capture is guaranteed to fire — then one step per index with exactly one line high,
then a trailing run of all-low.

Each step produces exactly one rising edge on a different analyzer channel, which is
what makes the mapping unambiguous. Matching is by **expected sample position** (the
trigger at `PreTriggerSamples`, then one step per index), not by assuming a channel
order. Defaults: 100 kHz sampling, 256 pre / 10 000 post, 2 ms per step.

Output is the GP ↔ analyzer-channel table. `--write` updates `docs/pinmap.md` — and is
**only honoured after a real capture**; `--dry-run` prints the table and writes nothing.

Requires the `rp2040_harness` host package (TASK.md §4.1), which does not exist yet;
without it the scan fails with a message naming the missing package and suggesting
`--dry-run`. The module itself imports it lazily, so `tests/` always imports cleanly.

### 3.2 Capture the counter and measure throughput

TASK.md §5.2: the counter test is driven **from the PC** through the Python API, not in
firmware, and there is **no automatic comparison against the expected sequence** — the
script emits the pattern and saves the capture; `measure.py` reports what was observed.

```powershell
# Generate settings, capture (background + hard timeout), export for viewing.
# ... drive the pattern with the host API while the capture runs ...

& .\.venv\Scripts\python.exe tests\measure.py tests\captures\counter.lac
& .\.venv\Scripts\python.exe tests\measure.py tests\captures\counter.lac --json
& .\.venv\Scripts\python.exe tests\measure.py tests\captures\counter.lac --channels 1-8 --markdown report.md
& .\.venv\Scripts\python.exe tests\measure.py tests\captures\counter.csv --frequency 20000000
```

`measure.py` reports:

* **distinct states** observed on the selected lines;
* **update frequency** = `1 / mean interval` between state changes (the `span rate` is
  also shown: `changes / span` reads high by `changes/(changes-1)`, because *N* changes
  span only *N-1* intervals);
* mean / median / min / max interval, standard deviation, peak-to-peak jitter, RMS
  jitter, coefficient of variation.

By default it analyses the **post-trigger region** (`--region post`), where the driven
pattern lives; use `--region all` or `--region pre` to change that.

> **Every report states it is a measurement, not a correctness check** (TASK.md §5.2).
> It does not know what the pattern was supposed to be, so a stream that skipped every
> other value still measures as a clean fast signal.

### 3.3 Viewing a capture

```powershell
# VCD for PulseView / GTKWave: keeps timing, so it can be decoded elsewhere.
& .\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'tests'); import la_lib; la_lib.write_vcd('out.vcd', la_lib.read_lac('tests/captures/counter.lac', require_samples=True))"
```

**The GUI's own sigrok decoders do not work on this machine and nothing here promises
decoded output.** `LogicAnalyzer.exe` bridges to Python via pythonnet, and its bundled
`Python.Runtime` carries type-offset tables only up to Python ABI **3.13**, while this
machine has only **3.14.2** — so every decoder load fails (the reason is written to
`PythonInitLog.txt` in the app directory). The 133 bundled decoders cannot run
standalone either: `decoders/sigrokdecode.py` is a stub that forwards to a C# object
that only exists inside the app.

Two routes, neither exercised here:

1. install Python 3.13 and write the app's `python.cfg` with the full path to its DLL
   (no trailing newline) so the app stops auto-detecting the highest version;
2. export VCD and decode elsewhere with PulseView or `sigrok-cli` (neither is
   installed on this machine).

---

## 4. Tests

Offline; no hardware required.

```powershell
& C:\Users\Yanichar\work\RP2040-harness\.venv\Scripts\python.exe -m pytest C:\Users\Yanichar\work\RP2040-harness\tests -q
```

Expected: `100 passed`.

The measurement tests are the meaningful ones: `fixtures/flat.lac` is a synthetic
counter whose update period is fixed by `fixture_gen`'s constants (8 samples at 20 MHz
= 400 ns), and `measure.py` must **recover that known frequency within 1 %** — as well
as the 8 distinct states and the zero jitter a synthetic sample-aligned counter must
show. Regenerate the fixture with:

```powershell
& .\.venv\Scripts\python.exe tests\fixture_gen.py
```

The suite also cross-checks the limits engine against the driver's own rules
(`max pre = total/10`, `max post = total-2`, mode by highest channel used), the trigger
encoding (`edge:4` → `TriggerChannel 3`; `complex:3:101` → pattern `0b101` with the
**leftmost digit on the trigger channel**, not a binary number), and the VCD
round trip.

---

## 5. File formats

Both are plain JSON, serialized by Newtonsoft with default settings: **compact,
single-line, no indentation**. `la_lib` reads and writes them; never hand-edit one into
a state the device would reject — regenerate with `write_tcs`.

### `.lac` — capture

```
{ "Settings": <CaptureSession> }
```

### `.tcs` — settings

A bare `CaptureSession` (no `Settings` wrapper). A `.lac` is accepted wherever a `.tcs`
is; only its settings are used and the capture repeats.

### `CaptureSession` fields

Serialized in declaration order:

| Field | Type | Notes |
|---|---|---|
| `Frequency` | int | Sample rate in Hz |
| `PreTriggerSamples` | int | Samples before the trigger |
| `PostTriggerSamples` | int | Samples after it (per pass) |
| `TotalSamples` | int | **Computed**: `post * (loop + 1) + pre`; ignored on load |
| `LoopCount` | int | 0–254 extra post-trigger passes |
| `MeasureBursts` | bool | Ask the driver to fill `Bursts[]` |
| `CaptureChannels` | array | The channels, in file order |
| `Bursts` | array/null | Burst metadata |
| `TriggerType` | int | 0 Edge, 1 Complex, 2 Fast, 3 Blast |
| `TriggerChannel` | int | **0-based** channel number |
| `TriggerInverted` | bool | Falling instead of rising (edge/blast) |
| `TriggerBitCount` | int | Pattern length; 0 for edge/blast |
| `TriggerPattern` | ushort | Bit *i* = the *(i+1)*-th channel of the pattern |

### `AnalyzerChannel` fields

| Field | Type | Notes |
|---|---|---|
| `TextualChannelNumber` | string | Computed `"Channel {n+1}"`; ignored on load |
| `ChannelNumber` | int | **0-based**; what everything keys on |
| `ChannelName` | string | Free text |
| `ChannelColor` | uint/null | ARGB |
| `Hidden` | bool | GUI display state |
| `Samples` | base64 string | One **byte per sample**, values `0`/`1` |

`samples.length == TotalSamples`; the trigger instant is sample index
`PreTriggerSamples`, so the pre-trigger region is `[0, PreTriggerSamples)`.

### Limits (what the driver enforces)

```
mode      = highest channel used < 8 ? 8ch : (< 16 ? 16ch : 24ch)
total     = floor(BufferSize / (8ch ? 1 : 16ch ? 2 : 24ch ? 4))
maxPre    = floor(total / 10)
maxPost   = total - 2
minFreq   = floor(MaxFrequency * 2 / 65535)
requested = post * (loop + 1) + pre
```

With the V6 board (buffer 131072, max 100 MHz, 24 channels):

| Mode | Channels | max total | max pre | max post |
|---|---|---|---|---|
| 8 | 1–8 | 131072 | 13107 | 131070 |
| 16 | 1–16 | 65536 | 6553 | 65534 |
| 24 | 1–24 | 32768 | 3276 | 32766 |

**The trap:** limits follow the channels *actually selected*. Adding channel 17+ drops
the usable buffer 4×, so a pre-trigger that is comfortable on 8 channels exceeds
`maxPre` in 24-channel mode. `write_tcs` checks all of this *before writing*, and
refuses to produce a file the device would reject.

---

## 6. What is verified, and what is not

**Verified (offline, by tests):** the `.lac`/`.tcs` codecs against the documented field
names and encodings; the limits engine; the trigger encoding and pattern bit order;
edge extraction; the VCD writer/reader round trip; frequency, interval and jitter
recovery from a known synthetic pattern; the stepping plan and the mapping matcher;
locating the analyzer install and the skill scripts.

**Not verified (no hardware attached):**

* **No capture has ever been run by this tooling.** The analyzer shows as
  `Cypress FX2LP (unconfigured)` and exposes no COM port, so `analyzer.capture()`,
  `probe()`, `recover()`, and `available_ports()` have only been exercised as far as
  their construction, not against a device.
* The **timeout/kill/recovery path** — the code is written to the skill's documented
  behaviour (raw `0xFF` abort, 2 s wait, close, reopen), but a real kill-and-recover has
  not been performed.
* The **real GP ↔ channel pin map**: `docs/pinmap.md` has not been written, because
  writing it requires a real capture.
* **Real throughput, latency and jitter** (TASK.md §3.2, §8 item 4) — the numbers
  `measure.py` produces on the synthetic fixture are a self-test of the arithmetic, not
  a bench measurement.
* `pinmap_scan.py` has not driven real pins: it needs the `rp2040_harness` package,
  which does not exist yet.

Unverified items are also stated inline in the relevant module docstrings.

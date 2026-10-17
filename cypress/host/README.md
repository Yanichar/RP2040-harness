# fx2-harness (Cypress FX2LP)

Host-side control of a Cypress **CY7C68013A (EZ-USB FX2LP)** 24-line harness
that speaks the same wire protocol v1.0 as the RP2040 harness, so one API drives
both boards.

The full documentation — hardware model, the chunked-EP0 transport, the build
and load procedure, CLI reference, measured limits and the list of deliberate
deviations — lives in the project README:

    ../README.md        (cypress/README.md)

Quick start:

```powershell
# build the firmware (SDCC + fx2lib)
powershell.exe -ExecutionPolicy Bypass -File ..\firmware\build.ps1

# install this package (from the repository root)
python -m pip install -e cypress\host

# load it and verify
python -m fx2_harness load
python -m fx2_harness info
python -m fx2_harness selftest
```

```python
from fx2_harness import Harness
from rp2040_harness import Harness as Rp2040Harness

with Harness.open() as fx:              # 04B4:8614
    fx.configure(0, direction="out")
    fx.set_pin(0, 1)
    assert fx.get_info().protocol_version == "1.0"

# the two boards can be driven side by side (one process per device)
with Rp2040Harness.open() as rp, Harness.open() as fx:
    rp.configure(0, direction="out")
    fx.configure(0, direction="in")
    rp.set_pin(0, 1)
    assert fx.read_all() & 1
```

Tests (no hardware needed):

```powershell
python -m pytest cypress\host\tests -q
```

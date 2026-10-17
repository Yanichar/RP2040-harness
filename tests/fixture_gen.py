"""Generate the synthetic ``.lac`` fixtures used by the offline tests.

Deterministic and stdlib-only: it builds :class:`la_lib.Capture` objects in memory
and writes them with :func:`la_lib.write_lac`, which is the *same* serialiser the
tests read back through :func:`la_lib.read_lac`. That round trip is deliberate —
it exercises the encoder and decoder against each other, and the byte-exact
expectations attached to the fixture (frequency, sample counts, trigger index)
come from this module, not from the file.

Run directly to (re)create ``tests/fixtures/flat.lac``::

    .venv\\Scripts\\python.exe tests\\fixture_gen.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from la_lib import Capture, Channel, write_lac  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FLAT_LAC = FIXTURES / "flat.lac"

# --- "flat.lac" definition -------------------------------------------------
# A 24-bit parallel counter whose low 8 bits are wired to analyzer channels 0-7.
# 20 MHz sampling, 64 samples before the trigger and 256 after; the counter holds
# each value for FLAT_HOLD_SAMPLES samples, so the state changes every 8 samples
# and the whole 8-value sweep completes exactly 32 times in the post region.
#
# Expected numbers the tests assert against, all derived from these constants:
#   sample period   = 50 ns
#   update period   = 8 * 50 ns = 400 ns  -> 2.5 MHz update frequency
#   state changes   = 31  in the post-trigger region (changes land at post-relative
#                         samples 8, 16, ..., 248; sample 0 has no predecessor, so
#                         FLAT_POST/FLAT_HOLD - 1 rather than FLAT_POST/FLAT_HOLD)
#   intervals       = 30
#   distinct states = 8          (values 0..7 on the three counter bits)
FLAT_CHANNELS = 8
FLAT_FREQUENCY = 20_000_000
FLAT_PRE = 64
FLAT_POST = 256
FLAT_HOLD_SAMPLES = 8
FLAT_COUNTER_BITS = 3


def flat_capture() -> Capture:
    """The flat (zero-jitter) counter capture the offline tests measure."""
    total = FLAT_PRE + FLAT_POST
    samples: list[bytearray] = [bytearray(total) for _ in range(FLAT_CHANNELS)]
    for index in range(total):
        value = (index // FLAT_HOLD_SAMPLES) & ((1 << FLAT_COUNTER_BITS) - 1)
        for bit in range(FLAT_CHANNELS):
            samples[bit][index] = (value >> bit) & 1

    return Capture(
        frequency=FLAT_FREQUENCY,
        pre_trigger_samples=FLAT_PRE,
        post_trigger_samples=FLAT_POST,
        channels=[
            Channel(
                number=number,
                name=f"CNT{number} (GP{2 + number})",
                samples=bytes(samples[number]),
            )
            for number in range(FLAT_CHANNELS)
        ],
        trigger_type=0,
        trigger_channel=0,
        trigger_inverted=False,
        source="synthetic: flat 24-bit counter, low 3 bits",
    )


def expected_flat_metrics() -> dict[str, float]:
    """The values ``measure.py`` must recover from ``flat.lac``, from the constants.

    Region is the default ``"post"``: samples ``[FLAT_PRE, FLAT_PRE + FLAT_POST)``.
    """
    sample_period_us = 1e6 / FLAT_FREQUENCY
    update_period_us = FLAT_HOLD_SAMPLES * sample_period_us
    # Changes land at post-relative samples 8, 16, ...; the first sample of the
    # region has no predecessor inside it, so one fewer change than hold periods.
    changes = FLAT_POST // FLAT_HOLD_SAMPLES - 1
    return {
        "frequency": float(FLAT_FREQUENCY),
        "sample_period_us": sample_period_us,
        "update_period_us": update_period_us,
        "update_hz": 1e6 / update_period_us,
        "changes": float(changes),
        "intervals": float(changes - 1),
        "analysed_samples": float(FLAT_POST),
        "distinct_states": float(2**FLAT_COUNTER_BITS),
        "elapsed_us": (changes - 1) * update_period_us,
        "pre_trigger_samples": float(FLAT_PRE),
    }


def write_flat(path: str | Path = FLAT_LAC) -> Path:
    """Write ``flat.lac`` and return its path."""
    return write_lac(path, flat_capture())


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    target = write_flat()
    metrics = expected_flat_metrics()
    size = target.stat().st_size
    print(f"wrote {target} ({size} bytes, {FLAT_PRE + FLAT_POST} samples x {FLAT_CHANNELS} channels)")
    print(f"  update period  : {metrics['update_period_us']:.3f} us -> {metrics['update_hz']:,.0f} Hz")
    print(f"  state changes  : {int(metrics['changes'])}")
    print(f"  distinct states: {int(metrics['distinct_states'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

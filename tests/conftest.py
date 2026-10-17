"""Shared pytest fixtures for the offline logic-analyzer tooling tests.

Everything here runs without hardware:

* ``flat_lac`` — the synthetic capture fixture, regenerated from
  :mod:`fixture_gen` when missing so a fresh checkout can run the suite. It is
  committed as ``fixtures/flat.lac`` so the tests are readable and the file's
  contents are reviewable.
* ``flat_capture`` — the same data parsed, ready to measure.
* ``expected_metrics`` — the numbers :mod:`measure` must recover, derived from the
  fixture's own constants rather than hard-coded, so changing the generator
  updates the expectations with it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import fixture_gen  # noqa: E402
import la_lib  # noqa: E402


@pytest.fixture(scope="session")
def flat_lac_path() -> Path:
    """Path to the synthetic ``.lac`` fixture, (re)created if absent."""
    path = fixture_gen.FLAT_LAC
    if not path.is_file():
        fixture_gen.write_flat(path)
    return path


@pytest.fixture(scope="session")
def flat_capture(flat_lac_path: Path) -> la_lib.Capture:
    """The fixture parsed through :func:`la_lib.read_lac`."""
    return la_lib.read_lac(flat_lac_path, require_samples=True)


@pytest.fixture(scope="session")
def expected_metrics() -> dict[str, float]:
    """Recoverable measurements implied by the fixture's generator constants."""
    return fixture_gen.expected_flat_metrics()

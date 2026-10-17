"""Cross-process exclusive device lock."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from rp2040_harness import FakeTransport, Harness
from rp2040_harness.errors import DeviceBusyError, HarnessError
from rp2040_harness.locking import DeviceLock, default_lock_path, read_lock_owner


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "device.lock"


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def test_default_lock_path_is_per_user(monkeypatch, tmp_path):
    monkeypatch.setenv("RP2040_HARNESS_LOCK", str(tmp_path / "custom.lock"))
    assert default_lock_path() == tmp_path / "custom.lock"

    monkeypatch.delenv("RP2040_HARNESS_LOCK")
    if sys.platform.startswith("win"):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        expected = tmp_path / "rp2040-harness" / "device.lock"
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        expected = tmp_path / "rp2040-harness" / "device.lock"
    assert default_lock_path() == expected
    assert "rp2040-harness" in str(default_lock_path())


# --------------------------------------------------------------------------
# Basic behaviour
# --------------------------------------------------------------------------


def test_acquire_creates_file_and_records_pid(lock_path):
    assert not lock_path.exists()
    with DeviceLock(lock_path) as lock:
        assert lock.held
        assert lock_path.exists()
        assert read_lock_owner(lock_path) == os.getpid()
    assert not lock.held
    # The file stays behind (that is normal for a lock file), but is unlocked.
    assert lock_path.exists()


def test_release_is_idempotent(lock_path):
    lock = DeviceLock(lock_path)
    lock.acquire()
    lock.release()
    lock.release()
    assert not lock.held


def test_acquire_is_idempotent_while_held(lock_path):
    lock = DeviceLock(lock_path)
    lock.acquire()
    lock.acquire()  # same object -> no-op, must not self-deadlock
    assert lock.held
    lock.release()


def test_second_lock_in_same_process_fails_and_names_owner(lock_path):
    first = DeviceLock(lock_path)
    first.acquire()
    try:
        second = DeviceLock(lock_path)
        with pytest.raises(DeviceBusyError) as excinfo:
            second.acquire()
        message = str(excinfo.value)
        assert str(os.getpid()) in message
        assert str(lock_path) in message
        assert excinfo.value.pid == os.getpid()
        assert excinfo.value.path == str(lock_path)
        assert not second.held
    finally:
        first.release()


def test_lock_is_reusable_after_release(lock_path):
    first = DeviceLock(lock_path)
    first.acquire()
    first.release()
    second = DeviceLock(lock_path)
    second.acquire()
    assert second.held
    second.release()


def test_waiting_acquire_succeeds_after_release(lock_path, monkeypatch):
    """With a timeout, the lock is retried instead of failing at once."""
    import threading
    import time

    holder = DeviceLock(lock_path)
    holder.acquire()

    def release_later():
        time.sleep(0.2)
        holder.release()

    thread = threading.Thread(target=release_later)
    thread.start()
    try:
        waiter = DeviceLock(lock_path, timeout_ms=3000, poll_ms=20)
        waiter.acquire()
        assert waiter.held
        waiter.release()
    finally:
        thread.join()
        holder.release()


def test_repr_reports_state(lock_path):
    lock = DeviceLock(lock_path)
    assert "free" in repr(lock)
    lock.acquire()
    assert "held" in repr(lock)
    lock.release()


# --------------------------------------------------------------------------
# Harness integration
# --------------------------------------------------------------------------


def test_harness_open_takes_lock_by_default(lock_path):
    fake = FakeTransport()
    h = Harness.open(transport=fake, lock=True, lock_path=lock_path)
    try:
        assert lock_path.exists()
        assert read_lock_owner(lock_path) == os.getpid()
    finally:
        h.close()
    # After close the lock is free again.
    again = DeviceLock(lock_path)
    again.acquire()
    again.release()


def test_two_harness_open_in_one_process_second_raises_busy(lock_path):
    first = Harness.open(
        transport=FakeTransport(), lock=True, lock_path=lock_path
    )
    try:
        with pytest.raises(DeviceBusyError, match="already locked"):
            Harness.open(
                transport=FakeTransport(), lock=True, lock_path=lock_path
            )
    finally:
        first.close()

    # Once the first harness is closed the lock is available again.
    second = Harness.open(
        transport=FakeTransport(), lock=True, lock_path=lock_path
    )
    second.close()


def test_harness_with_lock_disabled_does_not_take_it(lock_path):
    a = Harness.open(transport=FakeTransport(), lock=False, lock_path=lock_path)
    b = Harness.open(transport=FakeTransport(), lock=False, lock_path=lock_path)
    try:
        assert not lock_path.exists()
        a.ping(1)
        b.ping(2)
    finally:
        a.close()
        b.close()


def test_lock_released_when_transport_creation_fails(lock_path, monkeypatch):
    """A failed open() must not leak the lock."""
    import rp2040_harness.device as device_module
    from rp2040_harness.errors import DeviceNotFoundError

    class Boom:
        def __init__(self, *args, **kwargs):
            raise DeviceNotFoundError("no device")

    monkeypatch.setattr(device_module, "PyUsbTransport", Boom)
    with pytest.raises(DeviceNotFoundError):
        Harness.open(lock=True, lock_path=lock_path)

    lock = DeviceLock(lock_path)
    lock.acquire()
    lock.release()


def test_lock_released_on_context_manager_exception(lock_path):
    with pytest.raises(RuntimeError):
        with Harness.open(
            transport=FakeTransport(), lock=True, lock_path=lock_path
        ):
            raise RuntimeError("boom")
    lock = DeviceLock(lock_path)
    lock.acquire()
    lock.release()


# --------------------------------------------------------------------------
# Real cross-process exclusion
# --------------------------------------------------------------------------


_CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, sys.argv[3])
    from rp2040_harness.locking import DeviceLock
    lock = DeviceLock(sys.argv[1])
    try:
        lock.acquire()
    except Exception as exc:
        print(f"BUSY {type(exc).__name__}: {exc}")
        sys.exit(3)
    print("LOCKED")
    sys.stdout.flush()
    try:
        time.sleep(float(sys.argv[2]))
    finally:
        lock.release()
    """
)


def _src_path() -> str:
    import rp2040_harness

    return str(Path(rp2040_harness.__file__).resolve().parents[1])


@pytest.mark.skipif(
    os.environ.get("RP2040_HARNESS_SKIP_SUBPROCESS") == "1",
    reason="subprocess lock test disabled by environment",
)
def test_other_process_cannot_take_our_lock(lock_path):
    holder = DeviceLock(lock_path)
    holder.acquire()
    try:
        result = subprocess.run(
            [sys.executable, "-c", _CHILD, str(lock_path), "0.1", _src_path()],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 3, result.stdout + result.stderr
        assert "BUSY DeviceBusyError" in result.stdout
        assert str(os.getpid()) in result.stdout
    finally:
        holder.release()


@pytest.mark.skipif(
    os.environ.get("RP2040_HARNESS_SKIP_SUBPROCESS") == "1",
    reason="subprocess lock test disabled by environment",
)
def test_other_process_can_lock_after_we_release(lock_path):
    holder = DeviceLock(lock_path)
    holder.acquire()
    holder.release()
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(lock_path), "0", _src_path()],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LOCKED" in result.stdout


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def test_unwritable_lock_location_raises_harness_error(tmp_path):
    # A path whose parent is an existing *file* cannot be created as a dir.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="ascii")
    lock = DeviceLock(blocker / "device.lock")
    with pytest.raises(HarnessError):
        lock.acquire()
    assert not lock.held


def test_read_lock_owner_on_missing_or_garbage_file(tmp_path):
    assert read_lock_owner(tmp_path / "nope.lock") is None
    garbage = tmp_path / "garbage.lock"
    garbage.write_text("not-a-pid", encoding="ascii")
    assert read_lock_owner(garbage) is None
    good = tmp_path / "good.lock"
    good.write_text("4242", encoding="ascii")
    assert read_lock_owner(good) == 4242

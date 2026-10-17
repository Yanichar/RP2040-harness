"""Cross-process exclusive device lock.

The protocol has no arbitration mechanism (``docs/protocol.md`` §5.3), so
exclusivity is enforced on the host with an advisory file lock:

* Windows — ``msvcrt.locking(fd, LK_NBLCK, 1)`` on byte 0 of the lock file.
* POSIX — ``fcntl.flock(fd, LOCK_EX | LOCK_NB)``.

Lock file location:

* Windows: ``%LOCALAPPDATA%\\rp2040-harness\\device.lock``
* POSIX:   ``~/.cache/rp2040-harness/device.lock`` (``$XDG_CACHE_HOME`` honoured)

The owner PID is written into the file so a rejected second opener can tell the
user *who* holds the device.
"""

from __future__ import annotations

import atexit
import os
import sys
from pathlib import Path
from typing import IO, Final

from .errors import DeviceBusyError, HarnessError

__all__ = [
    "default_lock_path",
    "DeviceLock",
    "read_lock_owner",
]

_IS_WINDOWS: Final = sys.platform.startswith("win")

#: Byte offset of the Windows byte-range lock.  It must not overlap the PID
#: text at offset 0, because Windows locks are mandatory for readers too.
_LOCK_OFFSET: Final = 4096


def _default_dir() -> Path:
    if _IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "rp2040-harness"
        return Path.home() / "AppData" / "Local" / "rp2040-harness"
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "rp2040-harness"
    return Path.home() / ".cache" / "rp2040-harness"


def default_lock_path() -> Path:
    """The per-user lock file path (see the module docstring)."""
    override = os.environ.get("RP2040_HARNESS_LOCK")
    if override:
        return Path(override)
    return _default_dir() / "device.lock"


def read_lock_owner(path: Path) -> int | None:
    """Return the PID recorded in ``path``, or ``None`` if unreadable/empty."""
    try:
        text = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


class DeviceLock:
    """An exclusive, cross-process advisory lock on a lock file.

    Usable as a context manager.  Acquiring an already-held lock raises
    :class:`~rp2040_harness.errors.DeviceBusyError` naming the owning PID.

    Parameters
    ----------
    path:
        Lock file path.  Defaults to :func:`default_lock_path`.
    timeout_ms:
        How long to keep retrying before giving up.  ``0`` (default) means try
        once and fail immediately.
    poll_ms:
        Retry interval when ``timeout_ms > 0``.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        timeout_ms: int = 0,
        poll_ms: int = 100,
    ):
        self.path = Path(path) if path is not None else default_lock_path()
        self.timeout_ms = int(timeout_ms)
        self.poll_ms = max(int(poll_ms), 1)
        self._fd: int | None = None
        self._handle: IO[bytes] | None = None
        self._owner_pid: int | None = None

    # -- state --------------------------------------------------------------

    @property
    def held(self) -> bool:
        """True while this object owns the lock."""
        return self._fd is not None

    def __enter__(self) -> "DeviceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    # -- acquire / release --------------------------------------------------

    def acquire(self) -> "DeviceLock":
        """Take the lock.  Idempotent while already held by this object."""
        if self.held:
            return self
        import time

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HarnessError(
                f"cannot create the lock directory {self.path.parent}: {exc}"
            ) from exc

        deadline = time.monotonic() + self.timeout_ms / 1000.0
        while True:
            try:
                self._try_acquire()
            except DeviceBusyError:
                if self.timeout_ms <= 0 or time.monotonic() >= deadline:
                    raise
                time.sleep(self.poll_ms / 1000.0)
                continue
            return self

    def _try_acquire(self) -> None:
        try:
            try:
                handle = open(self.path, "r+b")
            except FileNotFoundError:
                handle = open(self.path, "w+b")
        except OSError as exc:
            raise HarnessError(f"cannot open lock file {self.path}: {exc}") from exc
        fd = handle.fileno()
        try:
            _lock_fd(fd)
        except OSError as exc:
            owner = read_lock_owner(self.path)
            handle.close()
            who = f" PID {owner}" if owner is not None else " another process"
            raise DeviceBusyError(
                f"the harness device is already locked by{who} "
                f"(lock file {self.path}). Close the other process, or pass "
                f"lock=False if you know what you are doing. "
                f"[{exc.__class__.__name__}: {exc}]",
                pid=owner,
                path=str(self.path),
            ) from exc
        # We own the lock: record our PID for whoever comes next.
        self._fd = fd
        self._handle = handle
        self._owner_pid = os.getpid()
        _active_locks.add(self)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("ascii"))
            handle.flush()
        except OSError:
            pass

    def release(self) -> None:
        """Release the lock.  Idempotent; never raises."""
        if self._fd is None:
            return
        fd, handle = self._fd, self._handle
        self._fd = None
        self._handle = None
        _active_locks.discard(self)
        try:
            _unlock_fd(fd)
        except OSError:
            pass
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def __del__(self) -> None:  # pragma: no cover - GC timing dependent
        try:
            self.release()
        except Exception:
            pass

    def __repr__(self) -> str:
        state = f"held by pid {self._owner_pid}" if self.held else "free"
        return f"<DeviceLock {self.path} ({state})>"


# --------------------------------------------------------------------------
# Platform primitives
# --------------------------------------------------------------------------


def _lock_fd(fd: int) -> None:
    """Try to take a non-blocking exclusive lock on ``fd``."""
    if _IS_WINDOWS:
        import msvcrt

        # Windows byte-range locks are mandatory: a locked byte cannot be read
        # through another handle.  Lock one byte at _LOCK_OFFSET, well away
        # from the PID text at offset 0, so a rejected opener can still read
        # who owns the lock.
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        # flock is advisory, so readers are unaffected wherever we lock.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if _IS_WINDOWS:
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


# Make sure an interpreter exit (including SystemExit in tests) never leaves a
# stale Windows byte-range lock behind; POSIX locks die with the descriptor.
_active_locks: set[DeviceLock] = set()


def _release_all() -> None:  # pragma: no cover - interpreter shutdown
    for lock in list(_active_locks):
        lock.release()


atexit.register(_release_all)

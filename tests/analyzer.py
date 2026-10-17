"""analyzer — driver around the gusmanb ``TerminalCapture.exe`` CLI.

    ####################################################################
    #  THE BLOCKING-CAPTURE HAZARD -- READ BEFORE CALLING capture()     #
    #                                                                  #
    #  ``TerminalCapture capture`` prints "Capture started, press      #
    #  Ctrl+C to stop." and then WAITS INDEFINITELY for the trigger.   #
    #  There is NO timeout in the tool itself. If the trigger never    #
    #  arrives it never returns.                                        #
    #                                                                  #
    #  Therefore this module NEVER runs a capture in the foreground.   #
    #  capture() spawns it as a child process, polls for the output    #
    #  file with a hard deadline, and on expiry kills the process      #
    #  tree. A killed capture leaves the device ARMED: the port then   #
    #  refuses to handshake. The kill path therefore always follows    #
    #  up with the skill's ``probe-device.ps1`` abort/reopen recovery  #
    #  (raw 0xFF, close, reopen, retry) -- see recover().              #
    ####################################################################

Interface:

* :func:`find_install`      locate the all-in-one directory.
* :func:`available_ports`   candidate COM ports (uses ``probe-device.ps1`` with no args).
* :func:`probe`             run ``probe-device.ps1 <port> -Json`` -> dict.
* :func:`recover`           abort + reopen recovery after a killed capture.
* :func:`capture`           background capture with hard timeout + kill path.

Nothing here touches the RP2040 harness package (see ``pinmap_scan.py`` for that),
and nothing here is imported by the library modules — ``analyzer.py`` is the only
file that shells out.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from la_lib import Capture, DeviceLimits, read_lac, write_tcs

__all__ = [
    "DEFAULT_INSTALL",
    "SKILL_SCRIPTS",
    "AnalyzerError",
    "CaptureTimeout",
    "CaptureResult",
    "find_install",
    "skill_scripts_dir",
    "available_ports",
    "probe",
    "recover",
    "build_settings",
    "capture",
]

#: The known all-in-one install on this machine (TASK.md §5.1, skill §0).
DEFAULT_INSTALL = Path(r"C:\Users\Yanichar\Downloads\all-in-one_6.0.0.1-win-x64")

#: Where the skill keeps ``probe-device.ps1`` and ``la.mjs``.
SKILL_SCRIPTS = Path(r"C:\Users\Yanichar\.dsh\skills\logic-analyzer\scripts")


class AnalyzerError(RuntimeError):
    """The analyzer CLI could not be run, or returned an unusable result."""


class CaptureTimeout(AnalyzerError):
    """The capture did not produce its output file before the hard deadline.

    The child process has been killed and the device is (probably) still armed;
    read :attr:`recovery` for what the abort/reopen attempt reported.
    """

    def __init__(self, message: str, *, recovery: "dict[str, Any] | None" = None):
        self.recovery = recovery
        super().__init__(message)


# ------------------------------------------------------------------ discovery


def find_install(start: str | Path | None = None) -> Path:
    """Locate the all-in-one directory holding the two ``.exe`` files.

    Order of preference:

    1. an explicit ``start`` path (CLI ``--install``) — **authoritative**: if it
       does not hold ``TerminalCapture.exe`` this raises instead of quietly
       falling back to some other install, because a caller who names a directory
       means that directory;
    2. ``$RP2040_LA_INSTALL``;
    3. the known default from TASK.md §5.1;
    4. a glob fallback over the usual download roots for
       ``all-in-one*\\TerminalCapture.exe`` and ``**\\TerminalCapture.exe``.

    Raises :class:`AnalyzerError` when nothing usable is found, listing what was
    tried, so the failure is actionable rather than a bare ``FileNotFoundError``.
    """
    tried: list[str] = []

    def usable(candidate: Path) -> bool:
        return (candidate / "TerminalCapture.exe").is_file()

    def consider(candidate: Path | None) -> Path | None:
        if candidate is None:
            return None
        candidate = Path(candidate)
        tried.append(str(candidate))
        return candidate.resolve() if usable(candidate) else None

    if start:
        explicit = Path(start)
        found = consider(explicit)
        if found:
            return found
        # Accept a path to the executable itself, for convenience.
        if explicit.is_file() and explicit.name.lower() == "terminalcapture.exe":
            return explicit.parent.resolve()
        raise AnalyzerError(
            f"{explicit} does not look like a LogicAnalyzer install "
            "(no TerminalCapture.exe inside).\nTried:\n  " + "\n  ".join(tried)
        )

    env = os.environ.get("RP2040_LA_INSTALL")
    found = consider(Path(env) if env else None)
    if found:
        return found

    found = consider(DEFAULT_INSTALL)
    if found:
        return found

    roots = [
        Path.home() / "Downloads",
        Path.home() / "Desktop",
        Path("C:/Users/Yanichar/Downloads"),
        Path("C:/tools"),
        Path("C:/Program Files"),
    ]
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        # Prefer the directory literally named all-in-one*, then anything holding
        # a TerminalCapture.exe, shallowest first so a stale nested copy loses.
        hits: list[Path] = sorted(root.glob("all-in-one*/TerminalCapture.exe"))
        hits += sorted(root.glob("**/TerminalCapture.exe"), key=lambda p: len(p.parts))
        for hit in hits:
            candidate = hit.parent
            if candidate in seen:
                continue
            seen.add(candidate)
            found = consider(candidate)
            if found:
                return found

    raise AnalyzerError(
        "could not locate the LogicAnalyzer all-in-one install "
        "(need TerminalCapture.exe + LogicAnalyzer.exe).\n"
        "Tried:\n  " + "\n  ".join(tried or ["(nothing)"]) + "\n"
        "Pass --install <dir> or set $RP2040_LA_INSTALL."
    )


def skill_scripts_dir(start: str | Path | None = None) -> Path:
    """Locate this skill's ``scripts/`` folder (``probe-device.ps1``, ``la.mjs``)."""
    if start:
        candidate = Path(start)
        if (candidate / "probe-device.ps1").is_file():
            return candidate.resolve()
        if (candidate / "scripts" / "probe-device.ps1").is_file():
            return (candidate / "scripts").resolve()

    env = os.environ.get("RP2040_LA_SKILL")
    if env:
        candidate = Path(env)
        if (candidate / "probe-device.ps1").is_file():
            return candidate.resolve()

    tried: list[str] = []
    for candidate in (
        SKILL_SCRIPTS,
        Path.home() / ".dsh" / "skills" / "logic-analyzer" / "scripts",
        Path(__file__).resolve().parent.parent / ".dsh" / "skills" / "logic-analyzer" / "scripts",
    ):
        tried.append(str(candidate))
        if (candidate / "probe-device.ps1").is_file():
            return candidate.resolve()
    for root in (Path.home() / ".dsh" / "skills", Path.home() / ".claude" / "skills"):
        if not root.is_dir():
            continue
        for hit in sorted(root.glob("**/probe-device.ps1")):
            return hit.parent.resolve()

    raise AnalyzerError(
        "could not locate probe-device.ps1 (the logic-analyzer skill's scripts folder).\n"
        "Tried:\n  " + "\n  ".join(tried)
    )


# -------------------------------------------------------------------- ports


def _powershell() -> str:
    for name in ("pwsh", "powershell"):
        located = shutil.which(name)
        if located:
            return located
    raise AnalyzerError("neither pwsh nor powershell is on PATH")


def _run_powershell(script: Path, args: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess:
    command = [
        _powershell(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *[str(a) for a in args],
    ]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AnalyzerError(
            f"{script.name} {' '.join(str(a) for a in args)} timed out after {timeout}s"
        ) from exc


def available_ports(*, timeout: float = 60.0) -> list[str]:
    """Candidate COM ports, via ``probe-device.ps1`` with no port argument.

    This only enumerates ``[System.IO.Ports.SerialPort]::GetPortNames()`` — it
    opens nothing, so it cannot disturb another program's port. The skill's
    script exits 2 after listing them, which is not an error here.

    On this machine the list is COM1/COM3/COM4 (motherboard/Bluetooth) and COM14
    (an unrelated 1209:3020 USB-serial). **None of them is the analyzer**: while
    the analyzer shows as ``Cypress FX2LP (unconfigured)`` in USB it exposes no
    COM port at all, so this list cannot identify it. Use :func:`probe` to test a
    candidate port once the FX2LP loads its firmware.
    """
    script = skill_scripts_dir() / "probe-device.ps1"
    result = _run_powershell(script, [], timeout=timeout)
    ports = [
        line.strip()
        for line in (result.stdout or "").splitlines()
        if line.strip().upper().startswith("COM")
    ]
    return sorted(set(ports))


def probe(port: str, *, timeout: float = 30.0, no_recovery: bool = False) -> dict[str, Any]:
    """Ask the device itself for its capabilities.

    Runs ``probe-device.ps1 <port> -Json`` and returns the parsed object:

    ``{port, version, channelCount, bufferSize, maxFrequency, blastFrequency,
    minFrequency, recovered, limits[], rawLines[]}`` — or ``{"error": "...",
    "lines": [...], "hint": "..."}`` when communication failed.

    The script performs the driver's own handshake (framed ``0x00`` request,
    five response lines) and, unless ``no_recovery``, sends the raw ``0xFF``
    abort and reopens the port if the first attempt times out — the same
    recovery ``StopCapture`` performs. That is exactly what un-arms a device left
    armed by a killed capture.

    Only one process may own the port: close ``LogicAnalyzer.exe`` and any
    running ``TerminalCapture`` capture first.

    Raises :class:`AnalyzerError` when the handshake fails. Returning a dict
    that happens to carry an ``"error"`` key instead would let a caller mistake
    an unreachable port for a ready analyzer — which is exactly how a
    bench-readiness report once came out green for a port that had failed.
    """
    script = skill_scripts_dir() / "probe-device.ps1"
    args = [port, "-Json"]
    if no_recovery:
        args.append("-NoRecovery")
    result = _run_powershell(script, args, timeout=timeout)
    text = (result.stdout or "").strip()
    if not text:
        raise AnalyzerError(
            f"probe-device.ps1 produced no output for {port} "
            f"(exit {result.returncode}): {(result.stderr or '').strip()[:200]}"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnalyzerError(
            f"probe-device.ps1 returned non-JSON output for {port} "
            f"(exit {result.returncode}): {text[:200]}"
        ) from exc
    if not isinstance(data, dict):
        raise AnalyzerError(f"probe-device.ps1 returned {type(data).__name__}, not an object")

    data.setdefault("exitCode", result.returncode)

    # The script reports failure in-band; treat it as a failure here too.
    error = data.get("error")
    if error:
        raise AnalyzerError(
            f"probe of {port} failed: {error}"
            + (f" ({data['hint']})" if data.get("hint") else "")
        )
    if result.returncode not in (0, None):
        raise AnalyzerError(
            f"probe-device.ps1 exited {result.returncode} for {port}: "
            f"{(result.stderr or '').strip()[:200]}"
        )

    # A successful probe must actually have identified the device.
    if not data.get("version") and not data.get("channelCount"):
        raise AnalyzerError(
            f"probe of {port} returned no device identity ({text[:200]})"
        )
    return data


def recover(port: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Un-arm a device left armed by a killed capture, and confirm it answers.

    Thin, deliberately-loud wrapper over :func:`probe`. ``probe-device.ps1``
    sends the raw ``0xFF`` abort, waits 2 s, closes and reopens the port, then
    retries the handshake and reports ``recovered: true``.

    A capture that was killed is **not** a clean stop: without this the port
    refuses to handshake and the *next* capture fails with an unclear error.
    If the retry still fails, the device must be unplugged and replugged — this
    function cannot do that for you and says so in the returned dict.
    """
    try:
        return probe(port, timeout=timeout, no_recovery=False)
    except AnalyzerError as exc:
        return {
            "recovered": False,
            "error": str(exc),
            "hint": (
                "Abort/reopen did not restore the handshake. Unplug and replug the "
                "analyzer, then probe again. If the port is held by another process, "
                "close LogicAnalyzer.exe and any running TerminalCapture capture first."
            ),
        }


# ---------------------------------------------------------------- settings


@dataclass
class CapturePlan:
    """Everything needed to start one capture, resolved up front."""

    port: str
    settings_path: Path
    output_path: Path
    frequency: int
    pre_trigger_samples: int
    post_trigger_samples: int
    channels: list[int]
    trigger: str
    limits: DeviceLimits
    session: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    @property
    def total_samples(self) -> int:
        return self.session["TotalSamples"]


def build_settings(
    settings_path: str | Path,
    *,
    channels: Sequence[int] | str = tuple(range(1, 25)),
    frequency: int = 20_000_000,
    pre_trigger_samples: int = 512,
    post_trigger_samples: int = 4000,
    trigger: str = "edge:1",
    names: dict[int, str] | None = None,
    buffer_size: int = 131_072,
    max_frequency: int = 100_000_000,
    channel_count: int = 24,
    blast_frequency: int = 200_000_000,
    force: bool = False,
) -> dict[str, Any]:
    """Generate a ``.tcs`` via :func:`la_lib.write_tcs`, with limits enforced.

    ``write_tcs`` raises :class:`la_lib.SettingsError` listing every violation
    before writing anything, so a file the device would reject is never produced
    unless ``force=True``. The device numbers come from :func:`probe` in normal
    use; the defaults are the verified V6 values.
    """
    return write_tcs(
        settings_path,
        channels=channels,
        frequency=frequency,
        pre_trigger_samples=pre_trigger_samples,
        post_trigger_samples=post_trigger_samples,
        names=names,
        trigger=trigger,
        buffer_size=buffer_size,
        max_frequency=max_frequency,
        channel_count=channel_count,
        blast_frequency=blast_frequency,
        force=force,
    )


# ---------------------------------------------------------------- capture


@dataclass
class CaptureResult:
    """Outcome of one :func:`capture` call."""

    ok: bool
    output: Path | None
    capture: Capture | None
    elapsed_s: float
    killed: bool
    exit_code: int | None
    stdout: str
    stderr: str
    recovery: dict[str, Any] | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": str(self.output) if self.output else None,
            "elapsedSeconds": round(self.elapsed_s, 3),
            "killed": self.killed,
            "exitCode": self.exit_code,
            "recovery": self.recovery,
            "detail": self.detail,
        }


def capture(
    port: str,
    settings_path: str | Path,
    output_path: str | Path,
    *,
    timeout: float = 30.0,
    install: str | Path | None = None,
    poll_interval: float = 0.25,
    recover_on_timeout: bool = True,
    kill_tree: bool = True,
) -> CaptureResult:
    """Run ``TerminalCapture capture <port> <settings> <out>`` in the background.

    **This is the whole point of this module.** The CLI blocks until the trigger
    fires and has no timeout of its own, so the process is started as a child,
    its output file is polled against a hard ``timeout`` deadline, and on expiry
    the process (tree) is killed:

    1. write no settings here — pass a ``.tcs`` produced by :func:`build_settings`
       or an existing ``.lac`` (only its settings are used; the capture repeats);
    2. ``subprocess.Popen([...TerminalCapture.exe, 'capture', port, settings, out])``
       with piped stdio;
    3. poll for ``out`` to appear **and** the process to exit, up to ``timeout``
       seconds. An output file alone is not success: the tool writes it at the
       end, but a killed process can leave a partial file, so the exit code is
       what decides;
    4. on deadline expiry: kill the process tree (``taskkill /T /F`` on Windows,
       ``kill`` elsewhere), then — because a killed capture leaves the device
       armed — call :func:`recover` unless ``recover_on_timeout=False``;
    5. parse the ``.lac`` with :func:`la_lib.read_lac` and return a
       :class:`CaptureResult`.

    Deadlines are reported as :class:`CaptureTimeout` only when recovery also
    fails; otherwise a timeout comes back as ``ok=False, killed=True`` with the
    recovery dict attached, because that is actionable rather than fatal.

    ``timeout`` must be generous enough for the trigger to be produced by the
    caller's own pattern (``pinmap_scan.py`` drives the pins right after starting
    the capture) or by the user's hand.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    install_dir = find_install(install)
    executable = install_dir / "TerminalCapture.exe"
    if not executable.is_file():
        raise AnalyzerError(f"TerminalCapture.exe not found in {install_dir}")

    settings_path = Path(settings_path).resolve()
    if not settings_path.is_file():
        raise AnalyzerError(f"settings file does not exist: {settings_path}")
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        # A stale file from an earlier run would look like instant success.
        output_path.unlink()

    command = [str(executable), "capture", port, str(settings_path), str(output_path)]
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=str(install_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    killed = False
    deadline = started + timeout
    try:
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                break
            if time.monotonic() >= deadline:
                killed = True
                _kill_process_tree(process, kill_tree=kill_tree)
                break
            time.sleep(poll_interval)
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process, kill_tree=kill_tree)
        killed = True
        stdout, stderr = process.communicate(timeout=10)
        exit_code = process.poll()

    elapsed = time.monotonic() - started

    if killed:
        recovery = recover(port) if recover_on_timeout else None
        detail = (
            f"capture did not finish within {timeout}s; the process was killed and the "
            "device was left armed, so the abort/reopen recovery was run."
        )
        if recovery and recovery.get("error"):
            detail += f" Recovery FAILED: {recovery['error']}"
        return CaptureResult(
            ok=False,
            output=None,
            capture=None,
            elapsed_s=elapsed,
            killed=True,
            exit_code=process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            recovery=recovery,
            detail=detail,
        )

    if exit_code != 0 or not output_path.is_file():
        detail = f"TerminalCapture exited {exit_code} without producing {output_path.name}"
        if stderr and stderr.strip():
            detail += f"; stderr: {stderr.strip()[:400]}"
        return CaptureResult(
            ok=False,
            output=None,
            capture=None,
            elapsed_s=elapsed,
            killed=False,
            exit_code=exit_code,
            stdout=stdout or "",
            stderr=stderr or "",
            detail=detail,
        )

    parsed = read_lac(output_path, require_samples=True)
    return CaptureResult(
        ok=True,
        output=output_path,
        capture=parsed,
        elapsed_s=elapsed,
        killed=False,
        exit_code=exit_code,
        stdout=stdout or "",
        stderr=stderr or "",
        detail=f"captured {parsed.sample_count} samples on {len(parsed.data_channels())} channels",
    )


def _kill_process_tree(process: subprocess.Popen, *, kill_tree: bool) -> None:
    """Kill the capture and, on Windows, any children it spawned.

    ``taskkill /T /F`` is the only reliable way to take down the .NET process
    tree; ``Popen.kill()`` alone can leave a child holding the serial port, which
    would keep the device armed and block the next capture.
    """
    if process.poll() is not None:
        return
    if kill_tree and sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if process.poll() is not None:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass

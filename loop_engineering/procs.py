"""Process groups without setuid helpers.

Lessons from v1: sandboxed hosts (Codex seatbelt) refuse to exec the setuid
``/bin/ps``, and Darwin returns EPERM from ``killpg`` when a group holds only
zombies. Membership is therefore read through libproc (macOS) or /proc
(Linux), and EPERM on a zombie-only group counts as already exited.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

POSIX = os.name == "posix"


@dataclass(frozen=True)
class Member:
    pid: int
    pgid: int
    zombie: bool
    start: float | None


# --------------------------------------------------------------------------- libproc

_LIBPROC = None


class _BSDInfo(ctypes.Structure):
    _fields_ = [("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64)]


_PROC_PIDTBSDINFO = 3
_PROC_PGRP_ONLY = 2
_PROC_ALL_PIDS = 1
_SZOMB = 5


def _libproc():
    global _LIBPROC
    if _LIBPROC is None and sys.platform == "darwin":
        _LIBPROC = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
    return _LIBPROC


def _darwin_info(pid: int) -> Member | None:
    lib = _libproc()
    info = _BSDInfo()
    size = lib.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        return None
    start = info.pbi_start_tvsec + info.pbi_start_tvusec / 1e6
    return Member(int(info.pbi_pid), int(info.pbi_pgid), info.pbi_status == _SZOMB, start)


def _darwin_pids(kind: int, arg: int) -> list[int]:
    lib = _libproc()
    capacity = 4096
    while True:
        buffer = (ctypes.c_int * capacity)()
        used = lib.proc_listpids(kind, arg, buffer, ctypes.sizeof(buffer))
        if used <= 0:
            return []
        count = used // ctypes.sizeof(ctypes.c_int)
        if count < capacity:
            return [buffer[i] for i in range(count) if buffer[i] > 0]
        capacity *= 2


# --------------------------------------------------------------------------- /proc

_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if POSIX and hasattr(os, "sysconf") else 100


def _linux_info(pid: int) -> Member | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    # The command name may contain spaces and parentheses; split after the last ')'.
    rest = raw[raw.rfind(")") + 2:].split()
    state, pgid, start_ticks = rest[0], int(rest[2]), int(rest[19])
    return Member(pid, pgid, state in {"Z", "X"}, start_ticks / _CLOCK_TICKS)


def _linux_pids() -> list[int]:
    return [int(name) for name in os.listdir("/proc") if name.isdigit()]


# --------------------------------------------------------------------------- public API


def info(pid: int) -> Member | None:
    if not POSIX or pid <= 0:
        return None
    if sys.platform == "darwin":
        return _darwin_info(pid)
    if Path("/proc").is_dir():
        return _linux_info(pid)
    return _ps_info(pid)


def _ps_info(pid: int) -> Member | None:
    try:
        out = subprocess.run(["ps", "-o", "pid=,pgid=,stat=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    return Member(int(out[0]), int(out[1]), out[2].startswith("Z"), None) if len(out) >= 3 else None


def group_members(pgid: int) -> list[Member] | None:
    """Current members of a process group; None when membership is unobservable."""
    if not POSIX:
        return None
    try:
        if sys.platform == "darwin":
            pids = _darwin_pids(_PROC_PGRP_ONLY, pgid)
        elif Path("/proc").is_dir():
            pids = _linux_pids()
        else:
            return None
    except OSError:
        return None
    members = []
    for pid in pids:
        member = info(pid)
        if member and member.pgid == pgid:
            members.append(member)
    return members


def identity(pid: int) -> str | None:
    """Stable identity string (pid + start time) to detect pid reuse."""
    member = info(pid)
    if not member or member.zombie:
        return None
    return f"{member.pid}@{member.start:.2f}" if member.start is not None else str(member.pid)


def alive(pid: int | None, expected_identity: str | None = None) -> bool:
    if not pid:
        return False
    current = identity(pid)
    if current is None:
        if not POSIX:
            return _windows_alive(pid)
        return False
    return expected_identity is None or current == expected_identity


def _windows_alive(pid: int) -> bool:  # pragma: no cover - exercised only on Windows
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=5)
        return str(pid) in out.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def group_quiescent(pgid: int) -> bool:
    members = group_members(pgid)
    return members is not None and all(m.zombie for m in members)


def signal_group(pgid: int, sig: int) -> str:
    """Return 'sent', 'gone' or 'zombie'. Raises only for a real permission problem."""
    if not POSIX:  # pragma: no cover
        try:
            os.kill(pgid, sig)
            return "sent"
        except OSError:
            return "gone"
    try:
        os.killpg(pgid, sig)
        return "sent"
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        if group_quiescent(pgid):
            return "zombie"
        raise


def terminate_group(pgid: int, grace: float = 3.0) -> str:
    """TERM, wait for the group to drain, then KILL. Never raises for exited groups."""
    try:
        first = signal_group(pgid, signal.SIGTERM)
    except PermissionError:
        return "denied"
    if first != "sent":
        return first
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if group_quiescent(pgid) or group_members(pgid) == []:
            return "terminated"
        time.sleep(0.05)
    try:
        return "killed" if signal_group(pgid, signal.SIGKILL) == "sent" else "terminated"
    except PermissionError:
        return "denied"


@dataclass
class Completed:
    exit_code: int | None
    outcome: str  # completed | timeout | cancelled | unavailable | output_limit
    duration_ms: int
    stdout: Path
    stderr: Path
    cleanup: str


def run(argv: list[str] | str, *, cwd: Path, logs: Path, timeout: float, stdin_text: str | None = None,
        env: dict | None = None, shell: bool = False, cancelled: Callable[[], bool] = lambda: False,
        on_start: Callable[[int], None] = lambda pid: None, max_output_bytes: int = 64 << 20,
        poll: float = 0.1) -> Completed:
    """Run one command in its own process group with bounded time and output."""
    logs.mkdir(parents=True, exist_ok=True)
    out_path, err_path = logs / "stdout.log", logs / "stderr.log"
    started = time.monotonic()
    full_env = {**os.environ, **(env or {})}
    with out_path.open("wb") as out, err_path.open("wb") as err:
        try:
            process = subprocess.Popen(argv, cwd=cwd, shell=shell, env=full_env,
                                       stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                       stdout=out, stderr=err, start_new_session=POSIX)
        except OSError as exc:
            err.write(str(exc).encode())
            return Completed(None, "unavailable", 0, out_path, err_path, "none")
        try:
            on_start(process.pid)
        except BaseException:
            terminate_group(process.pid, grace=0.5)
            process.wait()
            raise
        if stdin_text is not None:
            try:
                process.stdin.write(stdin_text.encode("utf-8"))
                process.stdin.close()
            except BrokenPipeError:
                pass
        outcome = "completed"
        try:
            while not _exited(process):
                if time.monotonic() - started > timeout:
                    outcome = "timeout"
                    break
                if cancelled():
                    outcome = "cancelled"
                    break
                if out_path.stat().st_size + err_path.stat().st_size > max_output_bytes:
                    outcome = "output_limit"
                    break
                time.sleep(poll)
        finally:
            # Clean the group while the leader is unreaped so its pgid cannot be reused.
            cleanup = terminate_group(process.pid, grace=2.0 if outcome == "completed" else 3.0)
            process.wait()
    return Completed(process.returncode, outcome, int((time.monotonic() - started) * 1000),
                     out_path, err_path, cleanup)


def _exited(process: subprocess.Popen) -> bool:
    """Detect exit without reaping, so the leader keeps the group id reserved."""
    if POSIX and hasattr(os, "waitid"):
        try:
            return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True
    return process.poll() is not None


def spawn_detached(argv: list[str], *, cwd: Path, log: Path, env: dict | None = None) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as handle:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
                                   start_new_session=POSIX, env={**os.environ, **(env or {})},
                                   creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    # Reap it when it exits so a long-lived parent (MCP server) accumulates no zombies.
    threading.Thread(target=process.wait, name="loop-reaper", daemon=True).start()
    return process.pid


def errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, str(exc.errno))

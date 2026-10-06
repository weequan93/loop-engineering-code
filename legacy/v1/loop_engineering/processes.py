"""Bounded child execution with retained logs and cooperative cancellation."""

from dataclasses import dataclass
from contextlib import ExitStack
import os
from datetime import datetime
from pathlib import Path
import signal
import subprocess
import time
from typing import Callable


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    outcome: str
    elapsed_ms: int
    stdout: Path
    stderr: Path


def execute(argv: list[str], cwd: Path, logs: Path, *, timeout: float,
            input_text: str | None = None, cancelled: Callable[[], bool] = lambda: False,
            on_started: Callable[[int], None] = lambda pid: None,
            max_output_bytes: int | None = None, env_overrides: dict | None = None) -> ProcessResult:
    logs.mkdir(parents=True, exist_ok=False)
    output, error = logs / "stdout.txt", logs / "stderr.txt"
    started = time.monotonic()
    with ExitStack() as stack:
        stdout = stack.enter_context(output.open("wb"))
        stderr = stack.enter_context(error.open("wb"))
        stdin = subprocess.DEVNULL
        if input_text is not None:
            source = logs / "stdin.txt"
            source.write_bytes(input_text.encode("utf-8"))
            stdin = stack.enter_context(source.open("rb"))
        try:
            process = subprocess.Popen(argv, cwd=cwd, stdin=stdin,
                                       stdout=stdout, stderr=stderr, start_new_session=os.name == "posix",
                                       env={**os.environ, **env_overrides} if env_overrides else None)
        except OSError as exc:
            stderr.write(str(exc).encode("utf-8"))
            return ProcessResult(None, "unavailable", int((time.monotonic() - started) * 1000), output, error)
        try:
            on_started(process.pid)
        except BaseException:
            terminate_group(process.pid)
            process.wait()
            raise
        outcome = "completed"
        try:
            while True:
                if max_output_bytes is not None and output.stat().st_size + error.stat().st_size > max_output_bytes:
                    outcome = "output_limit"
                    break
                if cancelled():
                    outcome = "cancelled"
                    break
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    outcome = "timeout"
                    break
                try:
                    process.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            # Stop owned background children even when the foreground exited.
            terminate_group(process.pid)
            process.wait()
    if max_output_bytes is not None and output.stat().st_size + error.stat().st_size > max_output_bytes:
        outcome = "output_limit"
    return ProcessResult(process.returncode, outcome, int((time.monotonic() - started) * 1000), output, error)


def terminate_group(pid: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def process_identity(pid: int) -> str | None:
    if os.name != "posix":
        return None
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "pgid="],
                                capture_output=True, text=True, timeout=2,
                                env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def same_process_identity(observed: str | None, recorded: str | None) -> bool:
    """Compare current ps identity with legacy English date layouts.

    Locale changes may reorder the month/day without changing the process.
    Missing observations and different birth times/groups still fail closed.
    """
    if not observed or not recorded:
        return False
    if observed == recorded:
        return True
    months = {name: number for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
    def parse(value):
        parts = value.split()
        if len(parts) != 6 or parts[0] not in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}:
            return None
        day, month = (parts[2], parts[1]) if parts[1] in months else (parts[1], parts[2])
        try:
            hour, minute, second = map(int, parts[3].split(":"))
            return datetime(int(parts[4]), months[month], int(day), hour, minute, second), int(parts[5])
        except (ValueError, KeyError):
            return None
    current, prior = parse(observed), parse(recorded)
    return current is not None and prior is not None and current == prior


def process_alive(pid: int) -> bool:
    """A denied observer is unknown, so conservatively consider it alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_group_alive(pid: int) -> bool:
    if os.name != "posix":
        return process_alive(pid)
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

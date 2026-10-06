"""Small shared helpers: time, ids, canonical hashing and atomic files."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid


class LoopError(Exception):
    """An expected, user-facing refusal. The message says what to do next."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def age_seconds(value: str | None) -> float | None:
    stamp = parse_time(value)
    return None if stamp is None else max(0.0, time.time() - stamp)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def slug(value: str, field: str = "id") -> str:
    if not isinstance(value, str) or not SLUG.match(value):
        raise LoopError(f"{field} must be 1-64 chars of lowercase letters, digits, '.', '_' or '-': {value!r}")
    return value


def slugify(text: str, fallback: str = "goal") -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    return value or fallback


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def atomic_write(path: Path, data: bytes | str, mode: int = 0o644) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, value) -> None:
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def tail(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def read_tail(path: Path, limit: int = 4000) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - limit * 4))
            return tail(handle.read().decode("utf-8", errors="replace"), limit)
    except FileNotFoundError:
        return ""


def text(value, field: str, limit: int = 20000, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise LoopError(f"{field} must be non-empty text")
    if len(value) > limit:
        raise LoopError(f"{field} is longer than {limit} characters")
    return value.strip()

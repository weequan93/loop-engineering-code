"""Content-addressed snapshots and preconditioned local file edits.

This protects the broker's file operations. It is not a process sandbox.
"""

import fnmatch
import hashlib
import os
import re
from pathlib import Path
import stat
import tempfile

from reference.core import canonical_digest
from .contracts import ContractError, relative_path


def byte_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def matches(path: str, patterns: list[str]) -> bool:
    # fnmatch's '*' can span '/'; the exact semantics are documented in runtime.md.
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def safe_path(root: Path, text: str) -> Path:
    relative_path(text)
    current = root.resolve()
    parts = text.split("/")
    for index, part in enumerate(parts):
        parent = current
        current = current / part
        if current.is_symlink():
            raise ContractError(f"Symlink traversal is not permitted: {text}")
        if current.exists() and part not in os.listdir(parent):
            raise ContractError(f"Use exact path spelling; filesystem aliases are not supported: {text}")
        if current.exists() and index < len(parts) - 1 and not current.is_dir():
            raise ContractError(f"Parent path is not a directory: {text}")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ContractError(f"Path escapes workspace: {text}")
    return current


def check_write_scope(root: Path, path: str, task: dict) -> Path:
    target = safe_path(root, path)
    if path.split("/")[0].casefold() in {".git", ".loop"}:
        raise ContractError("The local broker never edits .git or .loop control files")
    scope = task["scope"]
    if matches(path, scope["write_deny"]) or not matches(path, scope["write_allow"]):
        raise ContractError(f"Write is outside authorized scope: {path}")
    if target.exists() and not target.is_file():
        raise ContractError(f"Target is not a regular file: {path}")
    return target


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".loop-write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Snapshots:
    def __init__(self, store: Path):
        self.blobs = store / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)

    def blob(self, digest: str) -> Path:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ContractError("Invalid blob identity")
        return self.blobs / digest[7:]

    def put(self, data: bytes) -> str:
        digest = byte_digest(data)
        target = self.blob(digest)
        if not target.exists():
            atomic_write(target, data)
        elif byte_digest(target.read_bytes()) != digest:
            raise ContractError("Stored blob failed its integrity check")
        return digest

    def read(self, digest: str) -> bytes:
        data = self.blob(digest).read_bytes()
        if byte_digest(data) != digest:
            raise ContractError("Stored blob failed its integrity check")
        return data

    def capture(self, root: Path, profile: dict) -> dict:
        root = root.resolve()
        policy = profile["snapshot"]
        files, total = [], 0
        for directory, subdirs, names in os.walk(root, followlinks=False):
            directory = Path(directory)
            kept = []
            for name in sorted(subdirs):
                path = directory / name
                relative = path.relative_to(root).as_posix()
                if relative.split("/")[0].casefold() in {".git", ".loop"}:
                    continue
                if matches(relative, policy["exclude"]) or matches(relative + "/", policy["exclude"]):
                    continue
                if path.is_symlink():
                    files.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
                else:
                    kept.append(name)
            subdirs[:] = kept
            for name in sorted(names):
                path = directory / name
                relative = path.relative_to(root).as_posix()
                if relative.split("/")[0].casefold() in {".git", ".loop"}:
                    continue
                if matches(relative, policy["exclude"]):
                    continue
                information = path.lstat()
                if stat.S_ISLNK(information.st_mode):
                    files.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
                    continue
                if not stat.S_ISREG(information.st_mode):
                    raise ContractError(f"Unsupported snapshot input: {relative}")
                if information.st_size > policy["max_file_bytes"]:
                    raise ContractError(f"Snapshot input exceeds the configured file limit: {relative}")
                data = path.read_bytes()
                total += len(data)
                if total > policy["max_total_bytes"]:
                    raise ContractError("Snapshot exceeds the configured total byte limit")
                files.append({"path": relative, "kind": "file", "mode": stat.S_IMODE(information.st_mode),
                              "size": len(data), "sha256": self.put(data)})
        body = {"algorithm": "full-tree-v1", "files": sorted(files, key=lambda item: item["path"]),
                "policy_digest": canonical_digest(policy)}
        return {"digest": canonical_digest(body), "manifest": body}

    def materialize(self, snapshot: dict, root: Path) -> None:
        if canonical_digest(snapshot["manifest"]) != snapshot["digest"]:
            raise ContractError("Checkpoint manifest was modified")
        if root.exists():
            raise ContractError("Materialization target must be a fresh directory")
        root.mkdir(parents=True)
        for item in snapshot["manifest"]["files"]:
            if item["kind"] != "file":
                raise ContractError("Local verification does not support symlink inputs; use a protected runtime")
            target = safe_path(root, item["path"])
            atomic_write(target, self.read(item["sha256"]), item["mode"])

    def prepare_changes(self, root: Path, task: dict, changes: list[dict]) -> list[dict]:
        from .file_changes import task_change_bytes
        if changes and "edit_workspace" not in task["authorization"]["allowed_actions"]:
            raise ContractError("Task does not authorize workspace edits")
        prepared = []
        for change in changes:
            target = check_write_scope(root, change["path"], task)
            old = target.read_bytes() if target.exists() else None
            old_digest = byte_digest(old) if old is not None else None
            if old_digest != change["expected_sha256"]:
                raise ContractError(f"File precondition failed: {change['path']}")
            new = task_change_bytes(change, task)
            prepared.append({"path": change["path"], "old": self.put(old) if old is not None else None,
                             "new": self.put(new) if new is not None else None,
                             "mode": stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644})
        return prepared

    def apply_prepared(self, root: Path, task: dict, prepared: list[dict]) -> None:
        # Reconcile every path before any write, including on crash recovery.
        for change in prepared:
            target = check_write_scope(root, change["path"], task)
            current = byte_digest(target.read_bytes()) if target.exists() else None
            if current not in {change["old"], change["new"]}:
                raise ContractError(f"Unrecognized effect or user change at {change['path']}; manual reconciliation required")
        for change in prepared:
            target = check_write_scope(root, change["path"], task)
            current = byte_digest(target.read_bytes()) if target.exists() else None
            if current != change["new"]:
                if change["new"] is None:
                    target.unlink()
                else:
                    atomic_write(target, self.read(change["new"]), change["mode"])

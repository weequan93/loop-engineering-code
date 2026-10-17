"""Goals that run in parallel, each in its own git worktree of the project.

A worktree goal starts from a snapshot of the main working tree (uncommitted and untracked files included,
made without touching the main index or HEAD). Its agents and checks work in the worktree; its state stays in
the main project's `.loop`. When the goal is done, the controller turns the worktree's changes into a patch and
applies it to the main working tree, uncommitted, like any other loop work. A patch that does not apply cleanly
is saved for a person. Nothing is ever committed to a branch.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .util import LoopError

IDENTITY = {"GIT_AUTHOR_NAME": "loop", "GIT_AUTHOR_EMAIL": "loop@localhost",
            "GIT_COMMITTER_NAME": "loop", "GIT_COMMITTER_EMAIL": "loop@localhost"}


def _git(root: Path, *args: str, env: dict | None = None, check: bool = True, stdin: str | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, input=stdin,
                            env={**os.environ, **IDENTITY, **(env or {})}, timeout=600)
    if check and result.returncode != 0:
        raise LoopError(f"git {' '.join(args[:3])} failed in {root}: {(result.stderr or result.stdout).strip()[:800]}")
    return result.stdout.strip()


def snapshot_commit(root: Path) -> str:
    """A commit object of the working tree as it is now (ignored files excluded), via a temporary index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        head = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
        if head:
            _git(root, "read-tree", head, env=env)
        _git(root, "add", "-A", "--", ".", ":(exclude).loop", env=env)
        tree = _git(root, "write-tree", env=env)
        return _git(root, "commit-tree", tree, *(["-p", head] if head else []), "-m", "loop worktree snapshot",
                    env=env)


def path_for(main, goal_id: str) -> Path:
    return main.loop / "worktrees" / goal_id


def prepare(main, goal_id: str) -> dict:
    """Create (or reuse) the goal's worktree. Returns {"path", "base"}."""
    if not (main.root / ".git").exists():
        raise LoopError("Parallel goals need a git repository (they run in a git worktree)")
    path = path_for(main, goal_id)
    ref = f"refs/loop/{goal_id}/base"
    base = _git(main.root, "rev-parse", "--verify", "-q", ref, check=False)
    if path.is_dir() and base:
        return {"path": str(path), "base": base}
    base = snapshot_commit(main.root)
    _git(main.root, "update-ref", ref, base)  # keeps the base reachable while the goal runs
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(main.root, "worktree", "add", "--detach", str(path), base)
    _clone_ignored(main.root, path)
    return {"path": str(path), "base": base}


def _clone_ignored(source: Path, target: Path) -> None:
    """Dependencies and build outputs the checks need (node_modules, .venv, ...) are git-ignored, so the
    worktree lacks them. Clone them copy-on-write where the file system can (APFS); otherwise skip."""
    from .checks import _clonefile
    clone = _clonefile()
    if clone is None:
        return
    listing = _git(source, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", check=False)
    for rel in listing.splitlines():
        rel = rel.rstrip("/")
        destination = target / rel
        if not rel or rel == ".loop" or rel.startswith(".loop/") or destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        clone(os.fsencode(source / rel), os.fsencode(destination), 0x0001)


def integrate(main, goal_id: str, record: dict, patch_path: Path) -> dict:
    """Apply the worktree's changes to the main working tree. Returns {"status": merged|empty|conflict, ...}."""
    path, base = Path(record["path"]), record["base"]
    final = snapshot_commit(path)
    diff = subprocess.run(["git", "diff", "--binary", base, final, "--", ".", ":(exclude).loop"], cwd=main.root,
                          capture_output=True, timeout=600)
    if diff.returncode != 0:
        raise LoopError(f"git diff failed: {diff.stderr.decode(errors='replace')[:800]}")
    files = _git(main.root, "diff", "--name-only", base, final, "--", ".", ":(exclude).loop").splitlines()
    if not diff.stdout.strip():
        remove(main, goal_id, record)
        return {"status": "empty", "files": []}
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(diff.stdout)
    check = subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=main.root, capture_output=True,
                           text=True, timeout=600)
    if check.returncode != 0:
        return {"status": "conflict", "files": files, "patch": str(patch_path),
                "error": (check.stderr or check.stdout).strip()[:2000]}
    _git(main.root, "apply", str(patch_path))
    remove(main, goal_id, record)
    return {"status": "merged", "files": files, "patch": str(patch_path)}


def remove(main, goal_id: str, record: dict) -> None:
    _git(main.root, "worktree", "remove", "--force", record["path"], check=False)
    if Path(record["path"]).exists():
        shutil.rmtree(record["path"], ignore_errors=True)
        _git(main.root, "worktree", "prune", check=False)
    _git(main.root, "update-ref", "-d", f"refs/loop/{goal_id}/base", check=False)

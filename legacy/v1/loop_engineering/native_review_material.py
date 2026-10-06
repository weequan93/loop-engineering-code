"""Complete, bounded copies of authenticated evidence for a native reviewer.

The bundle is inspection material, not a new judgment or an evidence cache.
Original records retain their exact bindings and digests. Only their recorded
artifacts are copied; arbitrary links in project text are never followed.
"""

from copy import deepcopy
import json
from pathlib import Path
import stat
from urllib.parse import unquote, urlparse

from reference.core import canonical_digest
from .contracts import ContractError
from .workspace import atomic_write, byte_digest

MAX_RECORDS = 128
MAX_ARTIFACTS = 512
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def collect(controller, child, snapshot, comparison):
    """Validate bytes before dispatch/spend; preserve current/history labels."""
    store = controller.store
    entries, contents = [], {}
    selected = set(child["selected_evidence"])
    environment = controller.environment_digest(child)
    seen = set()

    def add(digest, record, relation):
        if digest in seen:
            return
        if len(entries) >= MAX_RECORDS:
            raise ContractError("Complete review material exceeds its record limit")
        copies = []
        for artifact in record["artifacts"]:
            parsed = urlparse(artifact["uri"])
            source = Path(unquote(parsed.path))
            if (parsed.scheme != "file" or parsed.netloc or source.is_symlink()
                    or not source.resolve().is_relative_to(store.directory)
                    or source.resolve().is_relative_to(store.authorities.directory)
                    or not stat.S_ISREG(source.stat().st_mode)):
                raise ContractError("Review artifact must be a regular collector-owned non-authority file")
            if source.stat().st_size > MAX_FILE_BYTES:
                raise ContractError("Complete review artifact exceeds its file byte limit")
            with source.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES or byte_digest(raw) != artifact["sha256"]:
                raise ContractError("Review artifact bytes changed while preparing complete material")
            name = "artifacts/" + artifact["sha256"][7:]
            if name not in contents:
                if len(contents) >= MAX_ARTIFACTS or sum(map(len, contents.values())) + len(raw) > MAX_TOTAL_BYTES:
                    raise ContractError("Complete review material exceeds its artifact or total byte limit")
                contents[name] = raw
            copies.append({"path": name, "sha256": artifact["sha256"], "bytes": len(raw)})
        controller._check_artifacts([record])
        entries.append({"relation": relation, "evidence_digest": digest,
            "record": deepcopy(record), "artifact_copies": copies,
            "current_for_review": record["snapshot_digest"] == snapshot["digest"]
                                  and record["environment_digest"] == environment})
        seen.add(digest)

    digests = list(dict.fromkeys([*child["selected_evidence"], *child["native"]["external_origins"]]))
    for digest, record in zip(digests, store.evidence(child["run_id"], digests)):
        if digest in child["native"]["external_origins"]:
            controller._origin(child, digest, record)
        add(digest, record, "selected" if digest in selected else "prior_judgment")
    for dependency in comparison["verified_dependencies"]:
        for record in dependency["checks"]:
            add(canonical_digest(record), record, "verified_dependency")
    manifest = {"schema_version": "1.0", "kind": "native-review-material",
        "task_id": child["task"]["task_id"], "snapshot_digest": snapshot["digest"],
        "environment_digest": environment, "entries": entries,
        "instruction": "Read artifact_copies relative to this manifest directory. Original record URIs are provenance only; do not follow them outside the inspection copies. Historical or dependency records do not substitute current final checks."}
    raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ContractError("Complete review material manifest exceeds its byte limit")
    contents["manifest.json"] = raw
    descriptor = {"manifest_path": "../review-evidence/manifest.json",
        "manifest_sha256": byte_digest(raw), "record_count": len(entries),
        "artifact_count": len(contents) - 1,
        "artifact_bytes": sum(len(value) for name, value in contents.items() if name != "manifest.json"),
        "complete_recorded_artifacts": True}
    return {"descriptor": descriptor, "contents": contents}


def write(directory, material):
    """Create a separate inspection copy without changing the candidate."""
    directory = Path(directory)
    if directory.exists() or directory.is_symlink():
        raise ContractError("Review material destination already exists")
    directory.mkdir(parents=True)
    for name, raw in material["contents"].items():
        atomic_write(directory / name, raw, 0o444)
    verify(directory, material)


def verify(directory, material):
    """Refuse mutations, omissions, redirection and added material."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ContractError("Reviewer modified its complete evidence copy")
    found = set()
    for path in directory.rglob("*"):
        name = path.relative_to(directory).as_posix()
        if path.is_symlink() or (path.is_dir() and name != "artifacts"):
            raise ContractError("Reviewer modified its complete evidence copy")
        if path.is_dir():
            continue
        raw = material["contents"].get(name)
        if (raw is None or not path.is_file() or path.stat().st_size != len(raw)
                or byte_digest(path.read_bytes()) != byte_digest(raw)):
            raise ContractError("Reviewer modified its complete evidence copy")
        found.add(name)
    if found != set(material["contents"]):
        raise ContractError("Reviewer modified its complete evidence copy")


def signed_artifacts(directory, material):
    return [Path(directory) / name for name in sorted(material["contents"])]

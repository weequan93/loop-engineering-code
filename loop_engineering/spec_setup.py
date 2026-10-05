"""Document-first requirements setup; semantic decisions belong to the host."""

import json
from pathlib import Path

from .contracts import ContractError, ROOT
from .scenarios import inputs, read_text
from .team_memory import build as memory_context

ROLE = "requirements_reviewer"
INSTRUCTIONS = ".loop/setup.md"
MAX_SPEC_BYTES = 1048576


def intake(brief=None, instructions=None):
    if brief is not None and (not brief.strip() or len(brief.encode()) > 16384):
        raise ContractError("Setup brief must be nonblank and at most 16384 bytes")
    return {"role": ROLE, "brief": brief, "state": "COLLECTING", "summary": None,
            "draft": "", "sources": [],
            "instructions": instructions or read_text(ROOT / "templates/scenarios/development/setup.md")}


def request_context(team, data):
    root = Path(data["workspace"])
    manifest, documents = inputs(root)
    role = next((r for r in manifest["roles"] if r["id"] == ROLE), None)
    if role is None:
        raise ContractError("This preset lacks the requirements setup role")
    profile = team.profile(data)
    snapshot = team.snapshots.capture(root, profile)
    preferred = profile["context_paths"]
    candidates = []
    for item in snapshot["manifest"]["files"]:
        name = item["path"]
        path = Path(name)
        if item["kind"] != "file":
            raise ContractError("Requirements setup needs regular snapshot files")
        if name in {"LOOP.md", "AGENTS.md"}:
            continue  # Supplied as mandatory repository instructions below.
        if name in preferred or (path.suffix.lower() in {".md", ".txt", ".rst", ".adoc"}
                                 and (len(path.parts) == 1 or path.parts[0] in {"docs", "doc", "documentation"})):
            candidates.append(item)
    candidates.sort(key=lambda f: (preferred.index(f["path"]) if f["path"] in preferred else len(preferred), f["path"]))
    included, omitted, used = [], [], 0
    limit = min(262144, profile["context_max_bytes"] // 4)
    for item in candidates:
        raw = team.snapshots.read(item["sha256"])
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            omitted.append(item["path"])
            continue
        if item["path"] == "spec.md" and content == documents[".loop/spec.md"]:
            continue
        if not content.strip() or "\0" in content or used + len(raw) > limit:
            omitted.append(item["path"])
            continue
        included.append({"path": item["path"], "sha256": item["sha256"], "content": content})
        used += len(raw)
    answers = [{"source": f"answer:{q['id']}:{i + 1}", "question": q["question"], "answer": answer}
               for q in json.loads(documents[".loop/questions.json"])["questions"]
               for i, answer in enumerate(q["answers"])]
    names = ["LOOP.md", ".loop/agent-instructions.md", role["instructions"]]
    if "agent_protocol" in manifest:
        names.append(manifest["agent_protocol"])
    if "AGENTS.md" in documents:
        names.append("AGENTS.md")
    context = {"role": role, "candidate": snapshot["digest"],
               "memory": memory_context(team, data, role=ROLE, candidate=snapshot["digest"], documents=documents),
               "instructions": [{"path": name, "content": documents[name]} for name in names],
               "setup_instructions": data["specification"]["instructions"],
               "brief": data["specification"]["brief"], "current_spec": documents[".loop/spec.md"],
               "draft": data["specification"]["draft"], "answers": answers,
               "questions": json.loads(documents[".loop/questions.json"])["questions"],
               "source_documents": included,
               "preparation_feedback": data.get("specification_feedback", [])[-8:],
               "source_coverage": {"eligible": len(candidates), "included": len(included),
                                   "omitted_count": len(omitted), "omitted_paths": omitted[:32]}}
    known = {item["path"] for item in included} | {item["source"] for item in answers}
    if context["brief"]:
        known.add("brief")
    if context["current_spec"].strip():
        known.add(".loop/spec.md")
    if "AGENTS.md" in documents:
        known.add("AGENTS.md")
    context["allowed_source_refs"] = sorted(known)
    return context


def validate_result(response, context, questions):
    draft = response["spec_markdown"]
    if "\0" in draft or len(draft.encode()) > MAX_SPEC_BYTES:
        raise ContractError("Requirements draft exceeds the UTF-8 byte limit or contains NUL")
    if len(response["sources"]) != len(set(response["sources"])) or not set(response["sources"]) <= set(context["allowed_source_refs"]):
        raise ContractError("Requirements agent cited a source that was not supplied")
    known = {q["id"] for q in questions["questions"]}
    for question in response["questions"]:
        if question["id"] in known:
            raise ContractError("Requirements agent cannot replace an existing question or actual answer")
        known.add(question["id"])
        questions["questions"].append({**question, "answers": []})
    from .contracts import validate
    validate("questions", questions)
    waiting = any(q["blocking"] and not q["answers"] for q in questions["questions"])
    if response["status"] == "ready":
        if waiting or not draft.strip() or not response["sources"]:
            raise ContractError("A ready spec needs nonblank content, supplied sources and no unanswered blocking questions")
    elif not waiting:
        raise ContractError("Requirements collection needs a concrete blocking question")
    return waiting

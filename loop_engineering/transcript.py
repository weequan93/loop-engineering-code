"""Readable transcripts of agent turns from the raw adapter output.

Each turn directory (``.loop/goals/<id>/runs/turn-NNNN/``) keeps ``prompt.md`` (what
the controller sent), ``stdout.log`` (the agent's event stream) and ``stderr.log``.
This module turns Codex JSONL and Claude stream-json into uniform entries::

    {"kind": "message|reasoning|command|tool|file|error|usage|other",
     "title": str, "body": str, "status": "running|ok|failed|None"}

Unknown formats fall back to plain text lines, so any command adapter shows something.
"""

from __future__ import annotations

import json
from pathlib import Path

BODY_LIMIT = 6000
MAX_ENTRIES = 3000


def _clip(text, limit: int = BODY_LIMIT) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, indent=1, default=str)
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters in the raw log)"


def entry(kind, title, body="", status=None) -> dict:
    return {"kind": kind, "title": title, "body": _clip(body), "status": status}


def _codex(events: list[dict]) -> list[dict]:
    rows, index = [], {}
    for event in events:
        kind = event.get("type")
        item = event.get("item") or {}
        itype = item.get("type")
        if kind in {"item.started", "item.updated", "item.completed"} and itype:
            row = _codex_item(item, kind)
            if row is None:
                continue
            key = item.get("id")
            if key in index:  # started → completed: update the same row in place
                rows[index[key]] = row
            else:
                index[key] = len(rows)
                rows.append(row)
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            rows.append(entry("usage", "Turn finished", " · ".join(f"{k}={v:,}" for k, v in usage.items()
                                                                    if isinstance(v, int)), "ok"))
        elif kind in {"turn.failed", "error"}:
            rows.append(entry("error", "Turn failed", event.get("error") or event.get("message") or event, "failed"))
        elif kind == "thread.started":
            rows.append(entry("other", f"Session {event.get('thread_id')}", "", None))
    return rows


def _codex_item(item: dict, phase: str) -> dict | None:
    itype = item["type"]
    running = phase != "item.completed"
    if itype == "agent_message":
        return entry("message", "Agent", item.get("text", ""), None if running else "ok")
    if itype == "reasoning":
        return entry("reasoning", "Reasoning", item.get("text") or item.get("summary") or "", None)
    if itype == "command_execution":
        code = item.get("exit_code")
        status = "running" if running else ("ok" if code == 0 else "failed")
        title = f"$ {item.get('command', '')}"
        body = item.get("aggregated_output", "")
        if code not in (None, 0):
            body = f"[exit {code}]\n{body}"
        return entry("command", title[:300], body, status)
    if itype == "mcp_tool_call":
        status = "running" if running else ("failed" if item.get("error") or item.get("status") == "failed" else "ok")
        body = {"arguments": item.get("arguments")}
        if item.get("error"):
            body["error"] = item["error"]
        if item.get("result") is not None:
            body["result"] = _mcp_result(item["result"])
        return entry("tool", f"{item.get('server')}.{item.get('tool')}", body, status)
    if itype == "file_change":
        changes = item.get("changes") or []
        return entry("file", f"Edited {len(changes)} file(s)",
                     "\n".join(f"{c.get('kind', '?'):>7}  {c.get('path', '')}" for c in changes),
                     "running" if running else ("ok" if item.get("status") != "failed" else "failed"))
    if itype == "web_search":
        return entry("tool", "web_search", item.get("query", ""), None if running else "ok")
    if itype == "todo_list":
        return entry("other", "Plan", "\n".join(f"[{'x' if i.get('completed') else ' '}] {i.get('text', '')}"
                                                 for i in item.get("items", [])), None)
    if itype == "error":
        # Codex reports config warnings and recoverable problems as error items; the
        # turn itself only failed if a turn.failed event follows.
        return entry("notice", "Host message", item.get("message", ""), None)
    return entry("other", itype, {k: v for k, v in item.items() if k not in {"id", "type"}}, None)


def _mcp_result(result):
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except (json.JSONDecodeError, TypeError):
            return joined
    return result


def _claude(events: list[dict]) -> list[dict]:
    rows, tools = [], {}
    for event in events:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            rows.append(entry("other", f"Session {event.get('session_id')}",
                              f"model={event.get('model')} tools={len(event.get('tools') or [])}", None))
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "text":
                    rows.append(entry("message", "Agent", block.get("text", ""), "ok"))
                elif btype == "thinking":
                    rows.append(entry("reasoning", "Thinking", block.get("thinking", ""), None))
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    tools[block.get("id")] = len(rows)
                    title = f"$ {block['input'].get('command')}" if name == "Bash" and isinstance(
                        block.get("input"), dict) else name
                    rows.append(entry("command" if name == "Bash" else "tool", str(title)[:300],
                                      {"input": block.get("input")}, "running"))
        elif kind == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    position = tools.get(block.get("tool_use_id"))
                    content = block.get("content")
                    if isinstance(content, list):
                        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                    status = "failed" if block.get("is_error") else "ok"
                    if position is not None:
                        row = rows[position]
                        row["body"] = _clip((row["body"] + "\n\n" if row["body"] else "") + _clip(content, 4000))
                        row["status"] = status
                    else:
                        rows.append(entry("tool", "result", content, status))
        elif kind == "result":
            usage = event.get("usage") or {}
            cost = event.get("total_cost_usd")
            parts = [f"cost=${cost:.4f}"] if isinstance(cost, (int, float)) else []
            parts += [f"{k}={v:,}" for k, v in usage.items() if isinstance(v, int)]
            rows.append(entry("usage", "Turn finished", " · ".join(parts),
                              "failed" if event.get("is_error") else "ok"))
    return rows


def parse(stdout: Path, stderr: Path | None = None) -> list[dict]:
    events, plain = [], []
    try:
        with stdout.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("{"):
                    try:
                        value = json.loads(stripped)
                        if isinstance(value, dict):
                            events.append(value)
                            continue
                    except json.JSONDecodeError:
                        pass
                if stripped:
                    plain.append(line.rstrip("\n"))
    except FileNotFoundError:
        pass
    types = {e.get("type") for e in events}
    if types & {"item.completed", "item.started", "thread.started"}:
        rows = _codex(events)
    elif types & {"assistant", "result"} or any(e.get("type") == "system" for e in events):
        rows = _claude([e for e in events if not e.get("parent_tool_use_id")])
    else:
        rows = [entry("other", "Output", "\n".join(plain[-400:]), None)] if plain else []
        plain = []
    if plain:
        rows.append(entry("other", "Other output", "\n".join(plain[-200:]), None))
    if stderr and stderr.is_file() and stderr.stat().st_size:
        text = stderr.read_text(encoding="utf-8", errors="replace")
        rows.append(entry("error", "stderr", text[-BODY_LIMIT:], None))
    return rows[-MAX_ENTRIES:]


# ---------------------------------------------------------------- sub-agents
# Codex stores each spawned sub-agent as its own rollout file whose session_meta names
# the parent thread. Claude stream-json tags sub-agent events with parent_tool_use_id.

ENCRYPTED = "[encrypted by the host; not readable outside the model]"


def _looks_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith("gAAAA") and " " not in value[:200]


def codex_thread_id(stdout: Path) -> str | None:
    try:
        with stdout.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"thread.started"' in line:
                    return json.loads(line).get("thread_id")
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _first_line(path: Path, limit: int = 1 << 20) -> dict | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return json.loads(handle.readline(limit))
    except (OSError, json.JSONDecodeError):
        return None


def find_codex_subagents(thread_id: str, since: float, sessions: Path | None = None,
                         max_files: int = 800) -> list[dict]:
    import os
    from datetime import datetime, timedelta
    root = sessions or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    if not thread_id or not root.is_dir():
        return []
    start = datetime.fromtimestamp(since) - timedelta(days=1)
    days, day = [], start.date()
    while day <= datetime.now().date():
        days.append(root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}")
        day += timedelta(days=1)
    candidates = []
    for directory in days:
        if directory.is_dir():
            candidates += [p for p in directory.glob("rollout-*.jsonl") if p.stat().st_mtime >= since - 120]
    metas = []
    for path in sorted(candidates)[:max_files]:
        first = _first_line(path)
        payload = (first or {}).get("payload") or {}
        spawn = ((payload.get("source") or {}).get("subagent") or {}).get("thread_spawn") if isinstance(
            payload.get("source"), dict) else None
        if spawn:
            metas.append({"path": path, "id": payload.get("id"), "parent": spawn.get("parent_thread_id"),
                          "name": spawn.get("agent_nickname") or spawn.get("agent_path"),
                          "agent_path": spawn.get("agent_path"), "role": spawn.get("agent_role"),
                          "depth": spawn.get("depth")})
    found, frontier = [], {thread_id}
    while frontier:  # include nested sub-agents of sub-agents
        children = [m for m in metas if m["parent"] in frontier and m not in found]
        found += children
        frontier = {m["id"] for m in children}
    return sorted(found, key=lambda m: (m.get("depth") or 0, str(m["path"])))


def parse_codex_rollout(path: Path) -> list[dict]:
    rows, calls = [], {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [entry("error", "Rollout unreadable", str(path), "failed")]
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "response_item":
            continue
        item = record.get("payload") or {}
        itype = item.get("type")
        if itype == "message" and item.get("role") in {"assistant", "user"}:
            text = "\n".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
            rows.append(entry("message" if item["role"] == "assistant" else "instruction",
                              "Sub-agent reply" if item["role"] == "assistant" else "Instruction", text, "ok"))
        elif itype == "agent_message":  # inter-agent message (task or follow-up from another agent)
            parts = []
            for c in item.get("content") or []:
                if isinstance(c, dict):
                    parts.append(ENCRYPTED if c.get("type") == "encrypted_content" else c.get("text", ""))
            rows.append(entry("instruction", f"From {item.get('author')} to {item.get('recipient')}",
                              "".join(parts), None))
        elif itype == "reasoning":
            summary = "\n".join(s.get("text", "") for s in item.get("summary") or [] if isinstance(s, dict))
            if summary.strip():
                rows.append(entry("reasoning", "Reasoning summary", summary, None))
        elif itype in {"function_call", "custom_tool_call", "local_shell_call"}:
            raw = item.get("arguments") if itype == "function_call" else item.get("input") or item.get("action")
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    raw = {k: (ENCRYPTED if _looks_encrypted(v) else v) for k, v in parsed.items()} \
                        if isinstance(parsed, dict) else parsed
                except json.JSONDecodeError:
                    raw = ENCRYPTED if _looks_encrypted(raw) else raw
            name = item.get("name") or itype
            if item.get("namespace"):
                name = f"{item['namespace']}.{name}"
            calls[item.get("call_id")] = len(rows)
            rows.append(entry("command" if name in {"exec", "exec_command", "shell", "local_shell_call"} else "tool",
                              name, raw, "running"))
        elif itype in {"function_call_output", "custom_tool_call_output"}:
            output = item.get("output")
            if isinstance(output, list):
                output = "\n".join(o.get("text", "") for o in output if isinstance(o, dict))
            position = calls.get(item.get("call_id"))
            if position is not None:
                row = rows[position]
                row["body"] = _clip((row["body"] + "\n\n── output ──\n" if row["body"] else "") + _clip(output, 4000))
                row["status"] = "ok"
            else:
                rows.append(entry("tool", "output", output, "ok"))
    return rows[-MAX_ENTRIES:]


def claude_subagents(stdout: Path) -> list[dict]:
    groups, titles = {}, {}
    try:
        lines = stdout.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in {"Task", "Agent"}:
                titles[block.get("id")] = (block.get("input") or {}).get("description") or block.get("name")
    for event in events:
        parent = event.get("parent_tool_use_id")
        if parent:
            groups.setdefault(parent, []).append({k: v for k, v in event.items() if k != "parent_tool_use_id"})
    return [{"id": key, "name": titles.get(key, key), "entries": _claude(value)} for key, value in groups.items()]


def subagents(turn_dir: Path, started_at: float | None) -> list[dict]:
    stdout = turn_dir / "stdout.log"
    thread = codex_thread_id(stdout)
    if thread:
        since = started_at or stdout.stat().st_mtime - 86400
        return [{"id": m["id"], "name": m["name"], "agent_path": m["agent_path"], "depth": m["depth"],
                 "entries": parse_codex_rollout(m["path"])}
                for m in find_codex_subagents(thread, since)]
    return claude_subagents(stdout)

"""Tell a person when the loop needs them (or finished), without anyone polling.

Fires once per new attention item: an open question or approval request, a blocker,
a budget limit, or completion/failure. Channels, configured in ``.loop/notify.json``
or ``~/.config/loop/notify.json``::

    {"desktop": true,                                  # macOS / Linux notification (default on)
     "telegram": {"bot_token": "123:ABC", "chat_id": "42"},   # or "bot_token_env": "MY_VAR"
     "command": ["sh", "-c", "curl -s -d @- ntfy.sh/my-topic"]}   # optional: JSON on stdin

`loop telegram-setup` writes the telegram entry for you (token typed in your terminal,
stored in ~/.config/loop/notify.json with mode 600, never in the project).

``LOOP_NOTIFY=0`` disables everything (tests, CI). Delivery is best effort and never
breaks a state change. An item counts as notified only after a channel delivered it,
so a sandboxed process that cannot notify leaves it for the runner to deliver.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from .util import read_json, write_json

ATTENTION_STATUSES = {"blocked": "Blocked", "limit": "Budget limit reached", "done": "Done", "failed": "Failed"}


def attention(goal: dict, state: dict) -> list[dict]:
    items = []
    for q in state["questions"]:
        if q["answer"] is None:
            label = "Approval needed" if q["kind"] == "approval" else "Question"
            items.append({"key": f"q:{q['id']}", "title": f"{label} · {goal['title']}",
                          "message": q["text"][:1500], "command": f"loop answer {q['id']} \"...\"", "id": q["id"],
                          "type": "question", "kind": q["kind"], "options": q.get("options") or []})
    from .engine import contract_changes
    from .model import contract_digest, contract_matches
    if not state.get("approved_digest"):
        acceptance = ", ".join(c["id"] for c in goal["acceptance"]) or "none"
        reviews = ", ".join(r["id"] for r in goal["reviews"]) or "none"
        items.append({"key": f"v:{contract_digest(goal)}", "title": f"New goal to approve · {goal['title']}",
                      "message": f"{goal['objective'][:700]}\n\nAcceptance: {acceptance}\nReviews: {reviews}\n"
                                 f"Budget: {goal['policy']['max_iterations'] or 'unlimited'} turns, "
                                 f"{goal['policy']['max_hours'] or 'unlimited'}h",
                      "command": f"loop approve --goal {goal['id']}", "type": "approve"})
    elif not contract_matches(goal, state["approved_digest"]):
        changes = contract_changes(goal, state)
        items.append({"key": f"v:{contract_digest(goal)}", "title": f"Approve contract change · {goal['title']}",
                      "message": "goal.json changed after approval:\n- " + "\n- ".join(changes),
                      "command": "loop approve", "type": "approve"})
    # A task waiting for a person (blocked, or needing a quiet machine, a device or a person) is announced as
    # soon as it is held: the other tasks keep the goal running, so the goal itself may not block for days.
    blocker_tasks = set((state.get("blocker") or {}).get("held_tasks") or [])
    for task in state["tasks"]:
        hold = task.get("hold") or {}
        if task["id"] in blocker_tasks or not (task["status"] == "blocked" or task.get("needs")) or not hold:
            continue
        if task["status"] not in {"blocked", "deferred"}:
            continue
        items.append({"key": f"h:{task['id']}:{hold.get('at')}", "title": f"Task waiting for you · {goal['title']}",
                      "message": f"{task['id']}: {task.get('title', '')}\n{hold.get('reason', '')[:900]}",
                      "command": f"loop task resume {task['id']} --reason \"...\"", "type": "held", "task": task["id"]})
    status = state["status"]
    if status in ATTENTION_STATUSES:
        reason = state["status_reason"]
        command = {"blocked": "loop unblock --note \"...\"", "limit": "loop resume",
                   "done": "loop status", "failed": "loop status"}[status]
        items.append({"key": f"s:{status}:{reason[:200]}", "title": f"{ATTENTION_STATUSES[status]} · {goal['title']}",
                      "message": reason[:1200], "command": command, "type": status})
    return items


def progress_item(goal: dict, state: dict) -> dict:
    """One message per finished agent turn (opt-in with "progress": true)."""
    turn = state["turns"][-1]
    tasks = state["tasks"]
    done = sum(1 for t in tasks if t["status"] == "done")
    checks = [state["checks"].get(f"acceptance.{c['id']}", {}).get("status") for c in goal["acceptance"]]
    passed = sum(1 for c in checks if c == "pass")
    flag = "" if turn.get("progress") else " · ⚠️ no progress"
    return {"key": f"t:{turn['n']}:{turn['outcome']}", "type": "progress", "turn": turn["n"],
            "outcome": turn["outcome"], "action": turn["action"], "minutes": round(turn.get("seconds", 0) / 60),
            "summary": (turn.get("summary") or "").strip()[:600],
            "title": f"Turn {turn['n']} {turn['action']} · {turn['outcome']} · {goal['title']}",
            "message": (f"Tasks {done}/{len(tasks)} · acceptance {passed}/{len(checks)} · "
                        f"{turn.get('seconds', 0) / 60:.0f} min{flag}\n{(turn.get('summary') or '').strip()[:600]}"),
            "command": "loop status"}


def set_progress(enabled: bool) -> Path:
    path = user_config_path()
    current = read_json(path, {}) if path.is_file() else {}
    current["progress"] = enabled
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, current)
    os.chmod(path, 0o600)
    return path


def config(project_root: Path) -> dict:
    merged = {"desktop": True, "command": None}
    for path in (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "loop" / "notify.json",
                 project_root / ".loop" / "notify.json"):
        if path.is_file():
            try:
                data = read_json(path)
                if isinstance(data, dict):
                    merged.update(data)
            except (OSError, ValueError):
                pass
    return merged


def maybe_notify(store, goal: dict, state: dict) -> list[str]:
    if os.environ.get("LOOP_NOTIFY") == "0":
        return []
    path = store.dir / "notified.json"
    try:
        seen = set(read_json(path, {"keys": []}).get("keys", []))
    except (OSError, ValueError):
        seen = set()
    items = attention(goal, state)
    settings_cache = None
    if state["turns"]:
        settings_cache = config(store.project.root)
        if settings_cache.get("progress"):
            items.append(progress_item(goal, state))
    current = {i["key"] for i in items}
    fresh = [i for i in items if i["key"] not in seen]
    delivered = []
    if fresh:
        settings = settings_cache or config(store.project.root)
        context = card_context(store, goal, state)
        for item in fresh:
            payload = {**item, "goal": goal["id"], "project": str(store.project.root), "status": state["status"],
                       "status_file": str(store.project.loop / "STATUS.md"), "context": context}
            if send(settings, payload):
                delivered.append(item["key"])
    # Forget resolved items, remember delivered ones.
    keep = (seen & current) | set(delivered)
    if keep != seen:
        try:
            write_json(path, {"keys": sorted(keep)})
        except OSError:
            pass
    return delivered


def send(settings: dict, payload: dict) -> bool:
    ok = False
    project = Path(payload["project"]).name
    if settings.get("desktop", True):
        ok = desktop(payload["title"], f"{payload['message']}\n{payload['command']}", project) or ok
    telegram = settings.get("telegram")
    if isinstance(telegram, dict) and telegram.get("chat_id"):
        markup, key = None, None
        if payload.get("type") in {"question", "blocked", "limit", "approve", "held"} and payload.get("goal"):
            from .telegram_bot import markup_for
            try:
                markup, key = markup_for(payload)
            except Exception:
                markup, key = None, None
        message_id = telegram_send_message(telegram, telegram_card(payload), markup, html=True)
        if message_id and key:
            from .telegram_bot import sent
            sent(key, message_id)
        ok = bool(message_id) or ok
    command = settings.get("command")
    if isinstance(command, list) and command and all(isinstance(c, str) for c in command):
        try:
            result = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False), text=True,
                                    capture_output=True, timeout=15)
            ok = result.returncode == 0 or ok
        except (OSError, subprocess.SubprocessError):
            pass
    return ok


CARD_HEADERS = {"question": ("❓", "QUESTION"), "approval": ("🔐", "APPROVAL NEEDED"), "blocked": ("⛔", "BLOCKED"),
                "limit": ("⏳", "BUDGET LIMIT"), "done": ("🎉", "DONE"), "failed": ("❌", "FAILED"),
                "held": ("⏸", "TASK WAITING FOR YOU")}
CARD_HINTS = {"question": "👇 Tap a button, or reply to this message with your answer",
              "blocked": "👇 Fix it, then tap Continue, or reply with what you did",
              "limit": "👇 Tap to add another budget window and continue",
              "approve": "👇 Review the change, then tap Approve",
              "held": "👇 Other tasks continue. When it can go on, tap Resume or reply with what changed"}
RULE = "━━━━━━━━━━━━━━━"


def card_context(store, goal: dict, state: dict) -> dict:
    """What a card shows besides the item itself: goal, stage, turn, tasks, acceptance."""
    tasks = state["tasks"]
    checks = [state["checks"].get(f"acceptance.{c['id']}", {}).get("status") for c in goal["acceptance"]]
    context = {"goal_title": goal["title"], "turn": state["iteration"], "stage": None,
               "tasks_done": sum(1 for t in tasks if t["status"] == "done"), "tasks_total": len(tasks),
               "acceptance_passed": sum(1 for c in checks if c == "pass"), "acceptance_total": len(checks)}
    try:
        from .status import summary
        s = summary(store, goal, state)
        current = next((p["stage"] for p in s["pipeline"] or [] if p["state"] == "current"), None)
        context["stage"] = current or s["next"]["kind"]
    except Exception:  # a card must never fail for want of a stage
        pass
    return context


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def rich(text: str) -> str:
    """Agent markdown → Telegram HTML: `code`, **bold**, web links; local file links become their label."""
    out = esc(text)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', out)
    out = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", out)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", out)
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)


def telegram_card(payload: dict) -> str:
    """HTML card: header, goal, progress line, quoted message, options, what to do, footer."""
    kind = payload.get("type")
    context = payload.get("context") or {}
    project = Path(payload["project"]).name
    if kind == "question" and payload.get("kind") == "approval":
        icon, label = CARD_HEADERS["approval"]
    elif kind == "approve":
        icon, label = "📝", ("APPROVE NEW GOAL" if payload.get("title", "").startswith("New goal") else "APPROVE CHANGE")
    elif kind == "progress":
        icon = "🔄" if payload.get("outcome") == "ok" else "⚠️"
        label = f"TURN {payload.get('turn')} {payload.get('action', '')} · {payload.get('outcome')} · {payload.get('minutes', 0)} min"
    else:
        icon, label = CARD_HEADERS.get(kind, ("🔔", "LOOP"))
    title = context.get("goal_title") or payload["title"].rpartition(" · ")[2]
    lines = [f"<b>{icon} {esc(label)} · {esc(project)}</b>", f"<i>{esc(title)}</i>", RULE]
    if context:
        stage = f"🧭 Stage: {esc(context['stage'])}" if context.get("stage") else "🧭"
        lines.append(stage if kind == "progress" else f"{stage} · Turn {context.get('turn', '?')}")
        lines.append(f"📋 Tasks {context.get('tasks_done', 0)}/{context.get('tasks_total', 0)} · "
                     f"✅ Acceptance {context.get('acceptance_passed', 0)}/{context.get('acceptance_total', 0)}")
    body = str(payload.get("summary" if kind == "progress" else "message") or "").strip()
    if body:
        quote = "<blockquote expandable>" if len(body) > 500 else "<blockquote>"
        lines += ["", f"{quote}{rich(body)}</blockquote>"]
    if kind == "question":
        from .telegram_bot import option_lines
        listed = option_lines(payload.get("options") or [])
        if listed:
            lines += ["", esc(listed)]
    hint = CARD_HINTS.get(kind) or f"▶ <code>{esc(payload.get('command', 'loop status'))}</code>"
    footer = f"🆔 {esc(payload['id'])} · " if payload.get("id") else ""
    lines += ["", hint, f"{footer}🕒 {time.strftime('%H:%M')}"]
    return "\n".join(lines)


def telegram_token(settings: dict) -> str | None:
    return settings.get("bot_token") or os.environ.get(settings.get("bot_token_env") or "LOOP_TELEGRAM_BOT_TOKEN")


def telegram_api(token: str, method: str, params: dict, timeout: float = 10.0) -> dict:
    data = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def telegram_send(settings: dict, text: str) -> bool:
    return bool(telegram_send_message(settings, text))


def telegram_send_message(settings: dict, text: str, markup: dict | None = None, html: bool = False) -> int | None:
    """Send a message (optionally HTML, optionally with inline buttons); returns its message_id."""
    token = telegram_token(settings)
    if not token:
        return None
    params = {"chat_id": str(settings["chat_id"]), "text": text[:4000], "disable_web_page_preview": "true"}
    if html:
        params["parse_mode"] = "HTML"
    if markup:
        params["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    try:
        result = telegram_api(token, "sendMessage", params)
        return (result.get("result") or {}).get("message_id") or (1 if result.get("ok") else None)
    except (OSError, ValueError):
        return None  # e.g. no network inside a sandbox; the runner retries on its next change


def user_config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "loop" / "notify.json"


def telegram_setup(token: str, chat_id: str | None = None, wait_seconds: int = 180, out=print, sleep=None) -> dict:
    """Verify the bot, discover the chat id from a message the user sends it, save, send a test."""
    import time
    sleep = sleep or time.sleep
    me = telegram_api(token, "getMe", {})
    if not me.get("ok"):
        raise ValueError("Telegram rejected the bot token")
    username = me["result"].get("username")
    if not chat_id:
        out(f"Open Telegram and send any message (for example /start) to @{username} ... waiting {wait_seconds}s")
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and not chat_id:
            updates = telegram_api(token, "getUpdates", {"timeout": "0"}).get("result", [])
            chats = [u.get("message", {}).get("chat", {}).get("id") for u in updates if u.get("message")]
            chats = [c for c in chats if c is not None]
            if chats:
                chat_id = str(chats[-1])
                break
            sleep(2)
        if not chat_id:
            raise ValueError(f"No message reached @{username}; send it a message and run setup again")
    path = user_config_path()
    current = read_json(path, {}) if path.is_file() else {}
    current["telegram"] = {"bot_token": token, "chat_id": str(chat_id)}
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, current)
    os.chmod(path, 0o600)
    sent = telegram_send(current["telegram"], f"✅ Loop notifications connected (@{username}). You will get questions, "
                                              "approvals, blockers and results here.")
    return {"bot": username, "chat_id": str(chat_id), "config": str(path), "test_message_sent": sent}


def desktop(title: str, message: str, subtitle: str) -> bool:
    try:
        if sys.platform == "darwin":
            # Pass text as argv: no AppleScript quoting, any language works.
            argv = ["osascript", "-e", "on run argv", "-e",
                    'display notification (item 1 of argv) with title (item 2 of argv) '
                    'subtitle (item 3 of argv) sound name "Glass"', "-e", "end run",
                    message.replace("\n", " — "), title, "Loop · " + subtitle]
            return subprocess.run(argv, capture_output=True, timeout=10).returncode == 0
        if shutil.which("notify-send"):
            return subprocess.run(["notify-send", title, message], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    return False

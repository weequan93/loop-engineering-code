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

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
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
                          "message": q["text"][:300], "command": f"loop answer {q['id']} \"...\"", "id": q["id"]})
    status = state["status"]
    if status in ATTENTION_STATUSES:
        reason = state["status_reason"]
        command = {"blocked": "loop unblock --note \"...\"", "limit": "loop resume",
                   "done": "loop status", "failed": "loop status"}[status]
        items.append({"key": f"s:{status}:{reason[:200]}", "title": f"{ATTENTION_STATUSES[status]} · {goal['title']}",
                      "message": reason[:300], "command": command})
    return items


def progress_item(goal: dict, state: dict) -> dict:
    """One message per finished agent turn (opt-in with "progress": true)."""
    turn = state["turns"][-1]
    tasks = state["tasks"]
    done = sum(1 for t in tasks if t["status"] == "done")
    checks = [state["checks"].get(f"acceptance.{c['id']}", {}).get("status") for c in goal["acceptance"]]
    passed = sum(1 for c in checks if c == "pass")
    flag = "" if turn.get("progress") else " · ⚠️ no progress"
    return {"key": f"t:{turn['n']}:{turn['outcome']}",
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
        for item in fresh:
            payload = {**item, "goal": goal["id"], "project": str(store.project.root), "status": state["status"],
                       "status_file": str(store.project.loop / "STATUS.md")}
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
        text = (f"🔔 {payload['title']}\n{payload['message']}\n\n▶ {payload['command']}\n📁 {payload['project']}")
        ok = telegram_send(telegram, text) or ok
    command = settings.get("command")
    if isinstance(command, list) and command and all(isinstance(c, str) for c in command):
        try:
            result = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False), text=True,
                                    capture_output=True, timeout=15)
            ok = result.returncode == 0 or ok
        except (OSError, subprocess.SubprocessError):
            pass
    return ok


def telegram_token(settings: dict) -> str | None:
    return settings.get("bot_token") or os.environ.get(settings.get("bot_token_env") or "LOOP_TELEGRAM_BOT_TOKEN")


def telegram_api(token: str, method: str, params: dict, timeout: float = 10.0) -> dict:
    data = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def telegram_send(settings: dict, text: str) -> bool:
    token = telegram_token(settings)
    if not token:
        return False
    try:
        result = telegram_api(token, "sendMessage", {"chat_id": str(settings["chat_id"]), "text": text[:4000],
                                                     "disable_web_page_preview": "true"})
        return bool(result.get("ok"))
    except (OSError, ValueError):
        return False  # e.g. no network inside a sandbox; the runner retries on its next change


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

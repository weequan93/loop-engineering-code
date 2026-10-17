"""Two-way Telegram: answer questions, approve, unblock and check status from the chat.

One small daemon per machine (``loop telegram-bot``; ``loop start`` launches it when Telegram
is configured) long-polls the bot and acts on:

- inline buttons sent with question / approval / blocker / limit notifications;
- a reply to such a notification (the reply text becomes the answer or unblock note);
- commands: /status, /pause <project>, /resume <project>, /help.

Only the configured chat is obeyed. After an answer or unblock it restarts the project's
runner if none is running, so nothing needs a terminal.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import time

from . import notify
from .util import LoopError, code_stamp, now, read_json, write_json

MAP_LIMIT = 500


def config_dir() -> Path:
    return notify.user_config_path().parent


# ---------------------------------------------------------------- project registry


def register(project_root: Path) -> None:
    path = config_dir() / "projects.json"
    data = read_json(path, {"projects": []}) if path.is_file() else {"projects": []}
    root = str(Path(project_root).resolve())
    if root not in data["projects"]:
        data["projects"].append(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, data)


def projects() -> list[Path]:
    path = config_dir() / "projects.json"
    data = read_json(path, {"projects": []}) if path.is_file() else {"projects": []}
    return [Path(p) for p in data["projects"] if (Path(p) / ".loop" / "goals").is_dir()]


# ---------------------------------------------------------------- message ↔ action map


def _map_path() -> Path:
    return config_dir() / "telegram-map.json"


def remember(entry: dict) -> str:
    path = _map_path()
    data = read_json(path, {}) if path.is_file() else {}
    key = secrets.token_hex(4)
    data[key] = {**entry, "at": now()}
    if len(data) > MAP_LIMIT:
        for old in sorted(data, key=lambda k: data[k].get("at", ""))[:-MAP_LIMIT]:
            data.pop(old, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)
    os.chmod(path, 0o600)
    return key


def _set_message(key: str, message_id: int) -> None:
    data = read_json(_map_path(), {})
    if key in data:
        data[key]["message_id"] = message_id
        write_json(_map_path(), data)


def lookup(key: str | None = None, message_id: int | None = None) -> tuple[str, dict] | tuple[None, None]:
    data = read_json(_map_path(), {}) if _map_path().is_file() else {}
    if key and key in data:
        return key, data[key]
    if message_id is not None:
        for k, v in data.items():
            if v.get("message_id") == message_id:
                return k, v
    return None, None


# ---------------------------------------------------------------- outgoing (used by notify)


def markup_for(payload: dict) -> tuple[dict | None, str | None]:
    """Inline keyboard + map key for an attention payload."""
    kind = payload.get("type")
    base = {"project": payload["project"], "goal": payload["goal"]}
    if kind == "question":
        options = payload.get("options") or []
        key = remember({**base, "action": "answer", "question": payload["id"], "options": options})
        rows = [[{"text": button_label(i, o), "callback_data": f"a:{key}:{i}"}] for i, o in enumerate(options[:6])
                if not o.startswith("<")]
        return {"inline_keyboard": rows + [tools(key, pause=True)]}, key
    if kind == "blocked":
        key = remember({**base, "action": "unblock"})
        return {"inline_keyboard": [[{"text": "▶️ Continue", "callback_data": f"u:{key}"}], tools(key, pause=True)]}, key
    if kind == "approve":
        key = remember({**base, "action": "approve"})
        label = "✅ Approve new goal" if payload.get("title", "").startswith("New goal") else "✅ Approve change"
        return {"inline_keyboard": [[{"text": label, "callback_data": f"v:{key}"}], tools(key)]}, key
    if kind == "held":
        key = remember({**base, "action": "resume_task", "task": payload["task"]})
        return {"inline_keyboard": [[{"text": "▶️ Resume task", "callback_data": f"t:{key}"}], tools(key)]}, key
    if kind == "limit":
        key = remember({**base, "action": "resume"})
        return {"inline_keyboard": [[{"text": "▶️ Add budget & continue", "callback_data": f"r:{key}"}], tools(key)]}, key
    return None, None


def tools(key: str, pause: bool = False) -> list[dict]:
    """The row under every actionable card: this project's status, and pause where it makes sense."""
    row = [{"text": "📊 Status", "callback_data": f"s:{key}"}]
    return row + [{"text": "⏸ Pause", "callback_data": f"p:{key}"}] if pause else row


NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]


def button_label(index: int, option: str) -> str:
    """Short, obviously tappable label; the full option text is in the message body."""
    fixed = {"approve": "✅ Approve", "deny": "❌ Deny"}
    if option in fixed:
        return fixed[option]
    head = option.split("：")[0].split(":")[0].split("，")[0].split(",")[0].strip()
    return f"{NUMBERS[index]} {head[:18]}"


def option_lines(options: list[str]) -> str:
    shown = [(i, o) for i, o in enumerate(options[:6]) if not o.startswith("<")]
    return "\n".join(f"{NUMBERS[i]} {o}" for i, o in shown if o not in {"approve", "deny"})


def sent(key: str | None, message_id: int | None) -> None:
    if key and message_id:
        _set_message(key, message_id)


# ---------------------------------------------------------------- incoming


def _service(project_root: Path):
    from .api import Service
    from .store import Project
    return Service(Project(project_root), actor="human (telegram)", interactive=True)


def _ensure_runner(project_root: Path, goal: str) -> str:
    from .runner import goal_to_run, start_background
    from .store import Project
    try:
        project = Project(project_root)
        result = start_background(project, goal_to_run(project, goal), None)
        return "runner already running" if result.get("already_running") else "runner started"
    except Exception as exc:  # e.g. the goal needs approval first
        return f"runner not started: {exc}"


def perform(entry: dict, choice: str | None, text: str | None) -> str:
    project, goal = Path(entry["project"]), entry["goal"]
    service = _service(project)
    action = entry["action"]
    if action == "answer":
        reply = text if text is not None else choice
        if not reply:
            return "Reply with text to answer"
        service.answer(entry["question"], reply, goal, by="human (telegram)")
        return f"✅ Answered {entry['question']}: {reply[:200]} · {_ensure_runner(project, goal)}"
    if action == "unblock":
        service.unblock(text or "Continue approved from Telegram", goal, by="human (telegram)")
        return f"▶️ Unblocked · {_ensure_runner(project, goal)}"
    if action == "resume_task":
        service.task("resume", entry["task"], goal_id=goal, reason=text or "Resumed from Telegram")
        return f"▶️ Task {entry['task']} resumed · {_ensure_runner(project, goal)}"
    if action == "approve":
        service.goal_approve(True, goal, by="human (telegram)")
        return f"✅ Contract approved · {_ensure_runner(project, goal)}"
    if action == "pause":
        service.control("pause", goal, by="human (telegram)")
        return "⏸ Pause requested (stops after the current turn)"
    if action == "resume":
        service.control("resume", goal, by="human (telegram)")
        return f"▶️ Resumed · {_ensure_runner(project, goal)}"
    return "Unknown action"


def resend_pending(settings: dict) -> int:
    """Send every open question / blocker / limit again, each with its buttons."""
    from .store import Project
    count = 0
    for root in projects():
        try:
            project = Project(root)
            store = project.goal()
            goal, state = store.goal(), store.state()
        except LoopError:
            continue
        context = notify.card_context(store, goal, state)
        for item in notify.attention(goal, state):
            if item.get("type") in {"question", "blocked", "limit", "approve", "held"}:
                notify.send({"desktop": False, "telegram": settings},
                            {**item, "goal": goal["id"], "project": str(project.root), "context": context})
                count += 1
    return count


STATUS_ICONS = {"running": "🟢", "ready": "🟢", "waiting": "🟡", "idle": "🟡", "paused": "⏸", "blocked": "⛔",
                "limit": "⏳", "done": "✅", "failed": "❌", "draft": "📝"}


def project_status(root: Path) -> str:
    """HTML block for one project: status, goal, stage, progress bar, questions, runner."""
    from .status import summary
    from .store import Project
    try:
        s = summary(Project(root).goal())
    except Exception as exc:  # one broken project must not hide the others
        return f"⚠️ <b>{notify.esc(root.name)}</b>: {notify.esc(exc)}"
    t = s["tasks"]
    stage = next((p["stage"] for p in s["pipeline"] or [] if p["state"] == "current"), None) or s["next"]["kind"]
    filled = round(10 * t["done"] / t["total"]) if t["total"] else 0
    bar = "▓" * filled + "░" * (10 - filled)
    percent = f"{100 * t['done'] // t['total']}%" if t["total"] else "-"
    live = s["runner"]["state"] == "running"
    status = s["status"]
    if not live and status in {"running", "ready", "waiting", "idle"}:  # nothing is driving it: say so
        icon, status = "⚪", f"stopped (runner {s['runner']['state'].split(' (')[0]})"
    else:
        icon = STATUS_ICONS.get(status, "•")
    lines = [f"{icon} <b>{notify.esc(root.name)}</b> · {notify.esc(status)}",
             f"   <i>{notify.esc(s['title'][:60])}</i>",
             f"   🧭 {notify.esc(stage)} · Turn {s['iteration']} · Tasks {t['done']}/{t['total']}",
             f"   {bar} {percent}"]
    questions = s["open_questions"]
    if questions:
        lines.append(f"   ❓ {len(questions)} open: " + notify.esc(questions[0]["text"][:90]))
    return "\n".join(lines)


def status_text(only: Path | None = None) -> str:
    roots = [only] if only else projects()
    if not roots:
        return "No registered projects. Run <code>loop install</code> in a project."
    head = f"<b>📊 LOOP STATUS</b> · 🕒 {time.strftime('%H:%M')}\n{notify.RULE}"
    return head + "\n" + "\n\n".join(project_status(root) for root in roots)


def _by_name(name: str) -> Path | None:
    for root in projects():
        if root.name == name or str(root) == name:
            return root
    return None


def handle(update: dict, settings: dict, api) -> None:
    """Process one Telegram update. ``api(method, params)`` calls the Bot API."""
    chat = str(settings["chat_id"])
    if "callback_query" in update:
        query = update["callback_query"]
        message = query.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != chat:
            return
        kind, _, rest = (query.get("data") or "").partition(":")
        key, _, index = rest.partition(":")
        if kind == "x":
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Already handled"})
            return
        _, entry = lookup(key=key)
        if not entry:
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "This button has expired"})
            return
        if kind == "s":  # look only: keep the card's buttons
            api("answerCallbackQuery", {"callback_query_id": query["id"]})
            api("sendMessage", {"chat_id": chat, "text": status_text(Path(entry["project"]))[:4000], "parse_mode": "HTML",
                                "reply_to_message_id": message.get("message_id")})
            return
        action = {"a": entry["action"], "u": "unblock", "p": "pause", "r": "resume", "v": "approve",
                  "t": "resume_task"}.get(kind)
        choice = None
        if kind == "a":
            options = entry.get("options") or []
            choice = options[int(index)] if index.isdigit() and int(index) < len(options) else None
        try:
            result = perform({**entry, "action": action}, choice, None)
        except LoopError as exc:
            result = f"⚠️ {exc}"
        api("answerCallbackQuery", {"callback_query_id": query["id"], "text": result[:190]})
        done = [[{"text": result.split(" · ")[0][:60], "callback_data": "x:"}]]  # the card now shows what was done
        api("editMessageReplyMarkup", {"chat_id": chat, "message_id": message.get("message_id"),
                                       "reply_markup": json.dumps({"inline_keyboard": done}, ensure_ascii=False)})
        api("sendMessage", {"chat_id": chat, "text": result, "reply_to_message_id": message.get("message_id")})
        return
    message = update.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != chat:
        return
    text_ = (message.get("text") or "").strip()
    reply_to = (message.get("reply_to_message") or {}).get("message_id")
    if reply_to:
        _, entry = lookup(message_id=reply_to)
        if entry:
            try:
                result = perform(entry, None, text_)
            except LoopError as exc:
                result = f"⚠️ {exc}"
            api("sendMessage", {"chat_id": chat, "text": result, "reply_to_message_id": message.get("message_id")})
            return
    if text_.startswith("/questions"):
        sent_any = resend_pending(settings)
        if not sent_any:
            api("sendMessage", {"chat_id": chat, "text": "✅ No open questions"})
    elif text_.startswith("/status"):
        api("sendMessage", {"chat_id": chat, "text": status_text()[:4000], "parse_mode": "HTML"})
    elif text_.startswith(("/pause", "/resume")):
        command, _, name = text_.partition(" ")
        root = _by_name(name.strip())
        if not root:
            api("sendMessage", {"chat_id": chat, "parse_mode": "HTML",
                                "text": "Usage: <code>/pause &lt;project&gt;</code> or <code>/resume &lt;project&gt;</code>\n\n"
                                        + status_text()})
            return
        from .store import Project
        goal = Project(root).current_id()
        result = perform({"project": str(root), "goal": goal, "action": command[1:]}, None, None)
        api("sendMessage", {"chat_id": chat, "text": result})
    elif text_.startswith(("/help", "/start")):
        api("sendMessage", {"chat_id": chat, "parse_mode": "HTML", "text": (
            "<b>🤖 Loop bot</b>\n" + notify.RULE + "\n"
            "• Answer a question with its buttons, or reply to the card with text\n"
            "• Blocked? Fix it, then tap <b>Continue</b>, or reply with what you did\n"
            "• 📊 <b>Status</b> on any card shows that project\n\n"
            "/questions: resend every open question with buttons\n/status: all projects\n"
            "/pause &lt;project&gt; · /resume &lt;project&gt;")})


COMMANDS = [
    {"command": "questions", "description": "Resend every open question with buttons"},
    {"command": "status", "description": "Progress and open questions for all projects"},
    {"command": "pause", "description": "Pause a project: /pause <project>"},
    {"command": "resume", "description": "Resume a project: /resume <project>"},
    {"command": "help", "description": "How to answer and unblock from Telegram"},
]
SCOPES = [None, {"type": "all_private_chats"}, {"type": "all_group_chats"}, {"type": "all_chat_administrators"}]
LANGUAGES = ["", "zh", "en"]


def set_commands(api) -> None:
    """Replace whatever "/" menu the bot had (e.g. from another project) with Loop's commands."""
    for scope in SCOPES:
        for language in LANGUAGES:
            params = {}
            if scope:
                params["scope"] = json.dumps(scope)
            if language:
                params["language_code"] = language
            try:
                api("deleteMyCommands", params)
            except OSError:
                pass
    api("setMyCommands", {"commands": json.dumps(COMMANDS, ensure_ascii=False)})
    try:
        api("setChatMenuButton", {"menu_button": json.dumps({"type": "commands"})})
    except OSError:
        pass


def run(once: bool = False, log=print) -> None:
    """Long-poll the bot. Exits if another bot process holds the lock."""
    from .store import try_lock
    settings = notify.config(Path.cwd()).get("telegram") or {}
    token = notify.telegram_token(settings)
    if not token or not settings.get("chat_id"):
        raise LoopError("Telegram is not configured; run `loop telegram-setup`")
    lock = try_lock(config_dir() / "telegram-bot.lock")
    if lock is None:
        if once:
            return
        raise LoopError("The Telegram bot is already running")
    state_path = config_dir() / "telegram-offset.json"
    offset = (read_json(state_path, {}) if state_path.is_file() else {}).get("offset")

    def api(method, params):
        return notify.telegram_api(token, method, params, timeout=40)
    try:
        set_commands(api)
    except OSError as exc:
        log(f"could not set the command menu: {exc}")
    log(f"loop telegram bot: polling for chat {settings['chat_id']}")
    stamp = code_stamp()
    try:
        while True:
            params = {"timeout": "0" if once else "25", "allowed_updates": json.dumps(["message", "callback_query"])}
            if offset is not None:
                params["offset"] = str(offset)
            try:
                result = api("getUpdates", params)
            except OSError as exc:
                if "409" in str(exc):
                    log("Another program is polling this bot (HTTP 409). Use a dedicated bot for Loop.")
                    time.sleep(60)
                else:
                    time.sleep(5)
                if once:
                    return
                continue
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle(update, settings, api)
                except Exception as exc:  # never let one bad update stop the bot
                    log(f"update {update.get('update_id')} failed: {type(exc).__name__}: {exc}")
                write_json(state_path, {"offset": offset})
            if once:
                return
            if code_stamp() != stamp:  # the framework was updated: never answer with stale code
                import os
                import sys
                log("framework code changed; restarting the bot")
                lock.close()
                os.execv(sys.executable, [sys.executable, "-m", "loop_engineering", *sys.argv[1:]])
    finally:
        lock.close()


def ensure_running(project_root: Path) -> None:
    """Start the bot daemon in the background when Telegram is configured (idempotent)."""
    import sys
    from . import procs
    from .store import try_lock
    if os.environ.get("LOOP_NOTIFY") == "0":
        return
    settings = notify.config(project_root).get("telegram") or {}
    if not settings.get("chat_id") or not notify.telegram_token(settings):
        return
    register(project_root)
    probe = try_lock(config_dir() / "telegram-bot.lock")
    if probe is None:
        return  # already running
    probe.close()
    package = str(Path(__file__).resolve().parent.parent)
    procs.spawn_detached([sys.executable, "-m", "loop_engineering", "telegram-bot"], cwd=Path(project_root),
                         log=config_dir() / "telegram-bot.log",
                         env=procs.user_env({"PYTHONPATH": package + os.pathsep + os.environ.get("PYTHONPATH", "")}))

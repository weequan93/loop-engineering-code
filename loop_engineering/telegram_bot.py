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
        return ({"inline_keyboard": rows} if rows else None), key
    if kind == "blocked":
        key = remember({**base, "action": "unblock"})
        return {"inline_keyboard": [[{"text": "▶️ 继续（解除阻塞）", "callback_data": f"u:{key}"},
                                     {"text": "⏸ 暂停", "callback_data": f"p:{key}"}]]}, key
    if kind == "approve":
        key = remember({**base, "action": "approve"})
        label = "✅ 批准新目标" if payload.get("title", "").startswith("New goal") else "✅ 批准变更"
        return {"inline_keyboard": [[{"text": label, "callback_data": f"v:{key}"}]]}, key
    if kind == "limit":
        key = remember({**base, "action": "resume"})
        return {"inline_keyboard": [[{"text": "▶️ 追加一份预算继续", "callback_data": f"r:{key}"}]]}, key
    return None, None


NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]


def button_label(index: int, option: str) -> str:
    """Short, obviously tappable label; the full option text is in the message body."""
    fixed = {"approve": "✅ 批准", "deny": "❌ 拒绝"}
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
            return "请回复文字作为回答"
        service.answer(entry["question"], reply, goal, by="human (telegram)")
        return f"✅ 已回答 {entry['question']}: {reply[:200]} · {_ensure_runner(project, goal)}"
    if action == "unblock":
        service.unblock(text or "Continue approved from Telegram", goal, by="human (telegram)")
        return f"▶️ 已解除阻塞 · {_ensure_runner(project, goal)}"
    if action == "approve":
        service.goal_approve(True, goal, by="human (telegram)")
        return f"✅ 合同变更已批准 · {_ensure_runner(project, goal)}"
    if action == "pause":
        service.control("pause", goal, by="human (telegram)")
        return "⏸ 已请求暂停（当前一轮结束后停止）"
    if action == "resume":
        service.control("resume", goal, by="human (telegram)")
        return f"▶️ 已继续 · {_ensure_runner(project, goal)}"
    return "未知操作"


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
        for item in notify.attention(goal, state):
            if item.get("type") in {"question", "blocked", "limit", "approve"}:
                notify.send({"desktop": False, "telegram": settings},
                            {**item, "goal": goal["id"], "project": str(project.root)})
                count += 1
    return count


def status_text() -> str:
    from .status import line
    from .store import Project
    rows = []
    for root in projects():
        try:
            project = Project(root)
            rows.append(f"• {root.name}: {line(project.goal())}")
            for q in project.goal().state()["questions"]:
                if q["answer"] is None:
                    rows.append(f"   ❓ {q['id']}: {q['text'][:120]}")
        except LoopError as exc:
            rows.append(f"• {root.name}: {exc}")
    return "\n".join(rows) or "No registered projects. Run `loop install` in a project."


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
        _, entry = lookup(key=key)
        if not entry:
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "This button has expired"})
            return
        action = {"a": entry["action"], "u": "unblock", "p": "pause", "r": "resume", "v": "approve"}.get(kind)
        choice = None
        if kind == "a":
            options = entry.get("options") or []
            choice = options[int(index)] if index.isdigit() and int(index) < len(options) else None
        try:
            result = perform({**entry, "action": action}, choice, None)
        except LoopError as exc:
            result = f"⚠️ {exc}"
        api("answerCallbackQuery", {"callback_query_id": query["id"], "text": result[:190]})
        api("editMessageReplyMarkup", {"chat_id": chat, "message_id": message.get("message_id"),
                                       "reply_markup": json.dumps({"inline_keyboard": []})})
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
            api("sendMessage", {"chat_id": chat, "text": "✅ 没有待回答的问题"})
    elif text_.startswith("/status"):
        api("sendMessage", {"chat_id": chat, "text": status_text()[:4000]})
    elif text_.startswith(("/pause", "/resume")):
        command, _, name = text_.partition(" ")
        root = _by_name(name.strip())
        if not root:
            api("sendMessage", {"chat_id": chat, "text": "用法：/pause <项目名> 或 /resume <项目名>\n" + status_text()})
            return
        from .store import Project
        goal = Project(root).current_id()
        result = perform({"project": str(root), "goal": goal, "action": command[1:]}, None, None)
        api("sendMessage", {"chat_id": chat, "text": result})
    elif text_.startswith(("/help", "/start")):
        api("sendMessage", {"chat_id": chat, "text": (
            "Loop 机器人：\n• 点问题下方按钮回答，或直接回复那条消息写回答\n• 阻塞时点「继续」，或回复写明处理说明\n"
            "• /questions 重新发送所有待回答的问题（带按钮）\n• /status 查看所有项目\n• /pause <项目> /resume <项目>")})


COMMANDS = [
    {"command": "questions", "description": "重新发送所有待回答的问题（带按钮）"},
    {"command": "status", "description": "所有项目的进度和待回答的问题"},
    {"command": "pause", "description": "暂停项目：/pause <项目名>"},
    {"command": "resume", "description": "恢复项目：/resume <项目名>"},
    {"command": "help", "description": "怎么用按钮回答、解除阻塞"},
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

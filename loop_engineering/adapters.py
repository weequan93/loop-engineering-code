"""Agent adapters: build one headless turn and parse what the agent reported.

Built in: ``claude`` (Claude Code ``claude -p``) and ``codex`` (``codex exec``).
Any other CLI agent is a ``command`` adapter declared in ``.loop/adapters.json``
or ``~/.config/loop/adapters.json``::

    {"gemini": {"argv": ["gemini", "-p", "{prompt}"], "stdin": false},
     "aider":  {"argv": ["aider", "--yes", "--message-file", "{prompt_file}"]}}

Placeholders: {prompt}, {prompt_file}, {project}, {mcp_config} (a JSON file in
the Claude/Cursor ``mcpServers`` format), {mcp_command} and {model}. The prompt
goes to stdin unless the argv uses {prompt} or {prompt_file}, or "stdin" is false.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import sys

from .util import LoopError, read_json, write_json


@dataclass
class Turn:
    argv: list[str]
    stdin: str | None
    env: dict = field(default_factory=dict)


@dataclass
class Report:
    session_id: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    summary: str = ""
    error: str | None = None


def mcp_server_spec(project_root: Path, autonomous: bool = True, extra: list[str] | None = None) -> dict:
    """How any host launches the loop MCP server for this project."""
    package_root = str(Path(__file__).resolve().parent.parent)
    args = ["-m", "loop_engineering", "mcp", "--project", str(project_root)]
    if autonomous:
        args.append("--autonomous")
    args += list(extra or [])
    return {"command": sys.executable, "args": args,
            "env": {"PYTHONPATH": package_root}}


class Adapter:
    name = "base"

    def __init__(self, config: dict):
        self.config = config

    def available(self) -> str | None:
        """Return a reason when the adapter cannot run here."""
        return None

    def build(self, prompt: str, project: Path, workdir: Path, goal: dict, session: str | None,
              mcp_args: list[str] | None = None, readonly: bool = False) -> Turn:
        """``readonly`` turns (independent reviews) get no edit tools / a read-only sandbox."""
        raise NotImplementedError

    def parse(self, stdout: Path, stderr: Path, workdir: Path) -> Report:
        return Report(summary=_tail_text(stdout))


def _tail_text(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:].strip()
    except FileNotFoundError:
        return ""


class ClaudeAdapter(Adapter):
    """Claude Code in print mode with stream-json output and the loop MCP server."""
    name = "claude"

    def available(self):
        return None if shutil.which(self.config.get("executable", "claude")) else "claude CLI not found on PATH"

    def build(self, prompt, project, workdir, goal, session, mcp_args=None, readonly=False):
        mcp = workdir / "mcp.json"
        write_json(mcp, {"mcpServers": {"loop": mcp_server_spec(project, extra=mcp_args)}})
        agent = goal["agent"]
        argv = [shutil.which(self.config.get("executable", "claude")) or "claude", "-p",
                "--output-format", "stream-json", "--verbose", "--mcp-config", str(mcp)]
        if readonly:
            argv += ["--permission-mode", "default", "--allowedTools", "mcp__loop,Read,Glob,Grep,Bash",
                     "--disallowedTools", "Edit,Write,NotebookEdit"]
        else:
            mode = agent.get("permission_mode") or self.config.get("permission_mode") or "acceptEdits"
            argv += ["--permission-mode", mode]
            if mode not in {"bypassPermissions"}:
                # Headless turns cannot answer prompts: allow the loop tools and the
                # tools a coding turn needs. Hosts can narrow this with extra_args.
                allowed = self.config.get("allowed_tools") or ["mcp__loop", "Bash", "Edit", "Write", "Read", "Glob",
                                                                "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite"]
                argv += ["--allowedTools", ",".join(allowed)]
        if agent.get("model"):
            argv += ["--model", agent["model"]]
        if agent.get("effort"):
            argv += ["--effort", {"minimal": "low"}.get(agent["effort"], agent["effort"])]
        cap = goal["policy"].get("max_cost_usd")
        if cap:
            argv += ["--max-budget-usd", f"{float(cap):.2f}"]
        if session:
            argv += ["--resume", session]
        argv += list(agent.get("extra_args") or [])
        return Turn(argv=argv, stdin=prompt)

    def parse(self, stdout, stderr, workdir):
        report = Report()
        texts = []
        for line in _lines(stdout):
            if line.get("type") == "system" and line.get("session_id"):
                report.session_id = line["session_id"]
            elif line.get("type") == "assistant":
                for block in (line.get("message") or {}).get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
            elif line.get("type") == "result":
                report.session_id = line.get("session_id") or report.session_id
                if isinstance(line.get("total_cost_usd"), (int, float)):
                    report.cost_usd = float(line["total_cost_usd"])
                usage = line.get("usage") or {}
                report.input_tokens = _int(usage.get("input_tokens"), usage.get("cache_read_input_tokens"),
                                           usage.get("cache_creation_input_tokens"))
                report.output_tokens = _int(usage.get("output_tokens"))
                if line.get("result"):
                    texts.append(str(line["result"]))
                if line.get("is_error"):
                    report.error = str(line.get("result") or line.get("subtype") or "error")[:2000]
        report.summary = (texts[-1] if texts else _tail_text(stderr))[-2000:]
        return report


class CodexAdapter(Adapter):
    """Codex CLI ``exec`` with JSONL events, workspace-write sandbox and the loop MCP server."""
    name = "codex"

    def available(self):
        return None if shutil.which(self.config.get("executable", "codex")) else "codex CLI not found on PATH"

    def _supports(self, flag: str) -> bool:
        if not hasattr(self, "_help"):
            import subprocess
            try:
                self._help = subprocess.run([self.config.get("executable", "codex"), "--help"], capture_output=True,
                                            text=True, timeout=20).stdout
            except (OSError, subprocess.SubprocessError):
                self._help = ""
        return flag in self._help

    def build(self, prompt, project, workdir, goal, session, mcp_args=None, readonly=False):
        spec = mcp_server_spec(project, extra=mcp_args)
        agent = goal["agent"]
        executable = shutil.which(self.config.get("executable", "codex")) or "codex"
        sandbox = "read-only" if readonly else (agent.get("permission_mode") or self.config.get("sandbox")
                                                or "workspace-write")
        # Global options precede the subcommand; `exec resume` accepts no -C/--sandbox.
        argv = [executable] + (["--no-daemon"] if self._supports("--no-daemon") else [])
        argv += ["-a", "never", "-s", sandbox, "exec"]
        if session:
            argv += ["resume", session]
        else:
            argv += ["-C", str(project)]
        argv += ["--skip-git-repo-check", "--json", "-o", str(workdir / "last-message.txt")]
        overrides = {"mcp_servers.loop.command": spec["command"], "mcp_servers.loop.args": spec["args"],
                     "mcp_servers.loop.env": spec["env"], "mcp_servers.loop.tool_timeout_sec": 180,
                     "mcp_servers.loop.startup_timeout_sec": 30,
                     # With approval policy "never", Codex rejects every MCP call that needs approval
                     # (all non-read-only tools). The loop server is the controller boundary, so its
                     # tools are pre-approved; the sandbox still governs shell commands.
                     "mcp_servers.loop.default_tools_approval_mode": "approve",
                     # Readable reasoning summaries in the event stream (raw reasoning stays encrypted).
                     "model_reasoning_summary": self.config.get("reasoning_summary", "detailed")}
        if goal["policy"].get("allow_network") and sandbox == "workspace-write":
            overrides["sandbox_workspace_write.network_access"] = True
        if agent.get("effort"):
            overrides["model_reasoning_effort"] = {"max": "xhigh"}.get(agent["effort"], agent["effort"])
        for key, value in overrides.items():
            argv += ["-c", f"{key}={_toml(value)}"]
        if agent.get("model"):
            argv += ["--model", agent["model"]]
        argv += list(agent.get("extra_args") or [])
        argv.append("-")
        return Turn(argv=argv, stdin=prompt)

    def parse(self, stdout, stderr, workdir):
        report = Report()
        inputs = outputs = 0
        seen_usage = False
        texts = []
        for line in _lines(stdout):
            kind = line.get("type")
            if kind == "thread.started":
                report.session_id = line.get("thread_id")
            elif kind == "turn.completed":
                usage = line.get("usage") or {}
                if isinstance(usage.get("input_tokens"), int) and isinstance(usage.get("output_tokens"), int):
                    inputs += usage["input_tokens"]
                    outputs += usage["output_tokens"]
                    seen_usage = True
            elif kind in {"turn.failed", "error"}:
                report.error = json.dumps(line.get("error") or line.get("message") or line)[:2000]
            elif kind == "item.completed":
                item = line.get("item") or {}
                if item.get("type") in {"agent_message", "assistant_message"} and item.get("text"):
                    texts.append(item["text"])
        if seen_usage:
            report.input_tokens, report.output_tokens = inputs, outputs
        last = workdir / "last-message.txt"
        report.summary = (last.read_text(encoding="utf-8", errors="replace") if last.exists()
                          else (texts[-1] if texts else _tail_text(stderr)))[-2000:]
        return report


class CommandAdapter(Adapter):
    """Any CLI agent declared in adapters.json."""
    name = "command"

    def available(self):
        argv = self.config.get("argv")
        if not argv:
            return "adapter has no argv"
        return None if shutil.which(argv[0]) or Path(argv[0]).is_file() else f"{argv[0]} not found"

    def build(self, prompt, project, workdir, goal, session, mcp_args=None, readonly=False):
        prompt_file = workdir / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        mcp = workdir / "mcp.json"
        spec = mcp_server_spec(project, extra=mcp_args)
        write_json(mcp, {"mcpServers": {"loop": spec}})
        values = {"prompt": prompt, "prompt_file": str(prompt_file), "project": str(project),
                  "mcp_config": str(mcp), "mcp_command": " ".join([spec["command"], *spec["args"]]),
                  "model": goal["agent"].get("model") or ""}
        raw = self.config["argv"]
        uses_prompt = any("{prompt}" in a or "{prompt_file}" in a for a in raw)
        argv = [a.format(**values) for a in raw] + list(goal["agent"].get("extra_args") or [])
        stdin = None if uses_prompt or self.config.get("stdin") is False else prompt
        env = {**{k: str(v).format(**values) for k, v in (self.config.get("env") or {}).items()},
               "PYTHONPATH": spec["env"]["PYTHONPATH"]}
        return Turn(argv=argv, stdin=stdin, env=env)


BUILTIN = {"claude": ClaudeAdapter, "codex": CodexAdapter}


def configured(project: Path) -> dict:
    merged = {}
    for path in (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "loop" / "adapters.json",
                 project / ".loop" / "adapters.json"):
        if path.is_file():
            data = read_json(path)
            if not isinstance(data, dict):
                raise LoopError(f"{path} must map adapter names to settings")
            merged.update(data)
    return merged


def get(name: str, project: Path) -> Adapter:
    settings = configured(project)
    config = settings.get(name, {})
    if name in BUILTIN:
        return BUILTIN[name](config)
    if config.get("argv"):
        return CommandAdapter(config)
    raise LoopError(f"Unknown adapter {name!r}. Built in: {sorted(BUILTIN)}; add others to .loop/adapters.json")


def _lines(path: Path):
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    with handle:
        for raw in handle:
            raw = raw.strip()
            if raw.startswith("{"):
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value


def _int(*values) -> int | None:
    numbers = [v for v in values if isinstance(v, int)]
    return sum(numbers) if numbers else None


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {_toml(v)}" for k, v in value.items()) + "}"
    raise TypeError(type(value))

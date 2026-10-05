"""One-time project-scoped host setup; preserve unrelated host preferences."""

import json
import hashlib
from pathlib import Path
import sys
import tomllib

from reference.core import strict_json_loads
from .contracts import ContractError, ROOT
from .workspace import atomic_write, safe_path

HOSTS = ("codex", "claude-code", "claude-desktop")
SKILL = ROOT / "templates/native-host/loop-engineering"
# Exact published instructions can be upgraded; edited project instructions
# retain the ordinary collision refusal and are never silently replaced.
PREVIOUS_SKILLS = {"18225575a5a3a5ef2d80342a046def37dcc1fe96a6dbf0c0bf19208c0643b1d7",
                   "08f81818db5a083acc3a01e8d3fef214885ca69a60ae276870eef997adaf78f2",
                   "d9d15c143a00b465f3d45862fca50303e6ff214cdf9054fa18a548363e0b0616",
                   "eb912e90c0a9500a6d23ed802240cf738352e20ee79eaca8e5d52f04b38be4c7",
                   "af986ce583cd21a725ba33803c7486861b49032ec9eb03beb4378467a3af905b",
                   "2a46ff2a611f1f96c98de292c512cb5adac8886f0b65c07d2c08a9b6b1d6b2d9",
                   "09256c6240b750ff761b0f6f5a1f59395a769ad9bd01e70dca4bf16be36bfd74",
                   "a42b122eca95f749a00e7c08900ce314c5cffa081ce997780cd23e97c70fd362"}


def configuration(project, state_dir, host="codex"):
    project, state_dir = Path(project).resolve(), Path(state_dir).resolve()
    if host not in HOSTS or not project.is_dir() or state_dir.is_relative_to(project):
        raise ContractError("Host setup requires a supported host, existing project and external private state")
    entry = {"command": str(Path(sys.executable).absolute()),
        "args": [str(ROOT / "scripts/native_mcp.py"), "--project", str(project), "--state-dir", str(state_dir)]}
    if host == "codex":
        configuration_text = ("[mcp_servers.loop-native]\ncommand = " + json.dumps(entry["command"], ensure_ascii=False) +
                              "\nargs = " + json.dumps(entry["args"], ensure_ascii=False) + "\n")
        target = ".codex/config.toml"
    else:
        if host == "claude-code":
            entry = {"type": "stdio", **entry}
        configuration_text = json.dumps({"mcpServers": {"loop-native": entry}}, indent=2, ensure_ascii=False) + "\n"
        target = ".mcp.json" if host == "claude-code" else ".loop/claude-desktop-native-mcp.json"
    return {"host": host, "project": str(project), "state_dir": str(state_dir), "server_name": "loop-native",
        "server_entry": entry, "configuration_text": configuration_text, "configuration_path": str(project / target),
        "skill_source": str(SKILL / "SKILL.md"), "automatic_dispatch": False}


def install(project, state_dir, host="codex"):
    config = configuration(project, state_dir, host)
    project = Path(config["project"])
    targets = {}
    registry_originals = {}
    if host == "claude-desktop":
        targets[".loop/native-host-instructions.md"] = (SKILL / "SKILL.md").read_bytes()
        targets[".loop/claude-desktop-native-mcp.json"] = config["configuration_text"].encode()
    else:
        prefix = ".agents/skills/loop-engineering" if host == "codex" else ".claude/skills/loop-engineering"
        targets[prefix + "/SKILL.md"] = (SKILL / "SKILL.md").read_bytes()
        if host == "codex":
            targets[prefix + "/agents/openai.yaml"] = (SKILL / "agents/openai.yaml").read_bytes()
        name = ".codex/config.toml" if host == "codex" else ".mcp.json"
        path = safe_path(project, name)
        original = _read(path)
        registry_originals[name] = original
        targets[name] = _merge_config(original, config)

    # Validate all paths and collisions before writing any file. A rerun is
    # harmless; a different/customized skill or server entry needs review.
    originals = {}
    upgraded = []
    for name, content in targets.items():
        path = safe_path(project, name)
        originals[name] = _read(path)
        if name in registry_originals and originals[name] != registry_originals[name]:
            raise ContractError("Host registry changed while preparing installation: " + str(path))
        is_registry = name in {".codex/config.toml", ".mcp.json"}
        if originals[name] not in {None, content} and not is_registry:
            is_skill = name.endswith("/SKILL.md") or name == ".loop/native-host-instructions.md"
            if not is_skill or hashlib.sha256(originals[name]).hexdigest() not in PREVIOUS_SKILLS:
                raise ContractError("Preserve the existing customized host file: " + str(path))
            upgraded.append(name)
    written = []
    try:
        for name, content in targets.items():
            path = safe_path(project, name)
            if _read(path) != originals[name]:
                raise ContractError("Host file changed during installation: " + str(path))
            if originals[name] == content:
                continue
            atomic_write(path, content)
            written.append(name)
    except BaseException:
        for name in reversed(written):
            path = safe_path(project, name)
            if _read(path) == targets[name]:
                if originals[name] is None:
                    path.unlink()
                else:
                    atomic_write(path, originals[name])
        raise
    return {"host": host, "project": str(project), "server_name": "loop-native",
        "files": [str(project / name) for name in targets], "files_written": len(written),
        "files_upgraded": [str(project / name) for name in upgraded],
        "host_settings_modified": host != "claude-desktop", "global_settings_modified": False,
        "automatic_dispatch": False, "native_app_connection_verified": False,
        "next": ("Merge the exported loop-native entry into Claude Desktop's local MCP settings, reconnect, and provide native-host-instructions.md as project instructions."
                 if host == "claude-desktop" else "Trust/open this project and reconnect its MCP tools in the selected host; invoke the loop-engineering skill. Inspect existing teams before starting a new mode.")}


def _read(path):
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 1048576:
        raise ContractError("Host configuration must be a regular file within 1 MiB: " + str(path))
    return path.read_bytes()


def _merge_config(original, config):
    raw = original or b""
    try:
        parsed = tomllib.loads(raw.decode()) if config["host"] == "codex" else strict_json_loads(raw.decode()) if raw else {}
    except (UnicodeError, ValueError) as exc:
        raise ContractError("Existing host configuration must be valid before installation") from exc
    if type(parsed) is not dict:
        raise ContractError("Host configuration must be an object/table")
    key = "mcp_servers" if config["host"] == "codex" else "mcpServers"
    servers = parsed.get(key, {})
    if type(servers) is not dict:
        raise ContractError("Host MCP registry must be an object/table")
    existing = servers.get("loop-native")
    if "loop-native" in servers:
        if (type(existing) is not dict or existing.get("type", "stdio") != "stdio"
                or any(existing.get(name) != config["server_entry"][name] for name in ("command", "args"))):
            raise ContractError("An existing loop-native server targets different settings; preserve it for explicit review")
        return raw
    if config["host"] == "codex":
        combined = raw + (b"\n" if raw else b"") + config["configuration_text"].encode()
        tomllib.loads(combined.decode())
        return combined
    parsed.setdefault(key, {})["loop-native"] = config["server_entry"]
    return (json.dumps(parsed, indent=2, ensure_ascii=False) + "\n").encode()

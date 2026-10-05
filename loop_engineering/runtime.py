"""Supervised local fallback and fail-closed, named Docker execution.

Docker is a deployment dependency, never pulled or installed by this module.
Only a fresh read-only snapshot is mounted. Host state, keys, sockets, credentials,
and model API access are absent from the container.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

from reference.core import canonical_digest
from .contracts import ContractError, relative_path
from .processes import ProcessResult, execute
from .project import local_capabilities


class LocalRuntime:
    backend = "local"

    def probe(self, directory: Path) -> dict:
        return {"backend": self.backend, "verified": False, "capabilities": local_capabilities(),
                "reason": "Supervised processes share host authority", "identity": "local-supervised"}


class DockerRuntime:
    backend = "docker"

    def __init__(self, config: dict, *, executable: str = "docker", control=None):
        self.config = config
        self.executable = executable
        self.control = control or self._control
        self.last_probe = None

    def _control(self, argv: list[str], timeout: float = 5) -> str:
        result = subprocess.run([self.executable] + argv, capture_output=True, text=True,
                                timeout=max(0.01, timeout), check=False)
        if result.returncode:
            # Never include arbitrary daemon error bodies or inherited secrets.
            raise ContractError("Docker control failed; inspect daemon/image availability and permissions")
        return result.stdout

    def name(self) -> str:
        return "loop-" + uuid4().hex

    def tmpfs_options(self) -> str:
        return f"rw,noexec,nosuid,nodev,size={self.config['tmp_mb']}m,mode=1777"

    def create_command(self, workspace: Path, cwd: str, argv: list[str], name: str, owner: str) -> list[str]:
        if not name.startswith("loop-") or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
            raise ContractError("Invalid container name")
        relative_path(cwd, allow_dot=True)
        root = str(workspace.resolve())
        if any(c in root for c in (",", "\n", "\0")):
            raise ContractError("Docker bind path cannot contain CSV delimiters or NUL")
        if not argv or any(type(item) is not str or not item or "\0" in item for item in argv):
            raise ContractError("Container execution requires a bounded argv array")
        if not self.config["image"]:
            raise ContractError("A pinned local image is required")
        cpu = self.config["cpu_millis"]
        cpus = f"{cpu // 1000}.{cpu % 1000:03d}"
        return ["create", "--name", name, "--label", "loop.owner=" + owner, "--pull=never",
                "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                "--memory", str(self.config["memory_mb"]) + "m", "--memory-swap", str(self.config["memory_mb"]) + "m",
                "--cpus", cpus, "--pids-limit", str(self.config["pids_limit"]), "--ipc=private",
                "--user", f"{self.config['uid']}:{self.config['gid']}", "--init",
                "--tmpfs", "/tmp:" + self.tmpfs_options(),
                "--mount", f"type=bind,src={root},dst=/workspace,readonly",
                "--workdir", "/workspace" + ("/" + cwd if cwd != "." else ""),
                "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint", argv[0], self.config["image"]] + argv[1:]

    def inspect(self, name: str, timeout: float = 5) -> dict | None:
        # `container ls` gives an unambiguous absence without treating a failed
        # daemon connection or permission denial as 'not executed'.
        listing = self.control(["container", "ls", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}"], timeout)
        if name not in listing.splitlines():
            return None
        values = json.loads(self.control(["container", "inspect", name], timeout))
        if type(values) is not list or len(values) != 1:
            raise ContractError("Ambiguous container identity")
        return values[0]

    def verify_container(self, value: dict, workspace: Path, owner: str) -> None:
        host, config = value["HostConfig"], value["Config"]
        mounts = value["Mounts"]
        if type(mounts) is not list or any(type(item) is not dict for item in mounts):
            raise ContractError("Unsupported container mount representation")
        binds = [item for item in mounts if item.get("Type") == "bind"]
        temporary = [item for item in mounts if item.get("Type") == "tmpfs"]
        expected = self.config
        if (config.get("Labels", {}).get("loop.owner") != owner
                or config.get("User") != f"{expected['uid']}:{expected['gid']}"
                or host.get("NetworkMode") != "none" or host.get("ReadonlyRootfs") is not True
                or host.get("Privileged") is not False or host.get("CapDrop") != ["ALL"]
                or host.get("CapAdd") or host.get("SecurityOpt") != ["no-new-privileges:true"]
                or host.get("Memory") != expected["memory_mb"] * 1048576
                or host.get("MemorySwap") != expected["memory_mb"] * 1048576
                or host.get("NanoCpus") != expected["cpu_millis"] * 1000000
                or host.get("PidsLimit") != expected["pids_limit"]
                or host.get("PidMode") not in {"", "private"} or host.get("IpcMode") != "private"
                or host.get("UtsMode", "") not in {"", "private"} or host.get("UsernsMode", "") not in {"", "private"}
                or host.get("Devices") or host.get("DeviceRequests") or host.get("Binds") or host.get("VolumesFrom")
                or host.get("Tmpfs") != {"/tmp": self.tmpfs_options()}
                or len(binds) != 1 or len(temporary) > 1 or len(mounts) != len(binds) + len(temporary)
                or Path(binds[0]["Source"]).resolve() != workspace.resolve()
                or binds[0].get("Destination") != "/workspace" or binds[0].get("RW") is not False
                or any(item.get("Source") or item.get("Destination") != "/tmp" or item.get("RW") is not True for item in temporary)):
            raise ContractError("Actual container policy differs from authorized containment")
        images = json.loads(self.control(["image", "inspect", expected["image"]], 5))
        if len(images) != 1 or images[0]["Id"] != value["Image"] or images[0].get("Config", {}).get("Volumes"):
            raise ContractError("Container image identity or implicit volumes differ from policy")

    def reconcile(self, name: str, owner: str) -> None:
        value = self.inspect(name)
        if value is None:
            return
        if value.get("Config", {}).get("Labels", {}).get("loop.owner") != owner:
            raise ContractError("Container identity is now owned by someone else; no removal")
        self.control(["container", "rm", "--force", name], 5)
        if self.inspect(name) is not None:
            raise ContractError("Owned container remains after cancellation")

    def execute(self, argv: list[str], workspace: Path, cwd: str, logs: Path, *, timeout: float,
                name: str, owner: str, cancelled=lambda: False, on_started=lambda pid: None) -> ProcessResult:
        started = time.monotonic()
        try:
            if cancelled():
                raise ContractError("Execution cancelled before container creation")
            self.control(self.create_command(workspace, cwd, argv, name, owner), min(5, timeout))
            value = self.inspect(name, min(5, timeout))
            if value is None:
                raise ContractError("Container creation result is missing")
            self.verify_container(value, workspace, owner)
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise ContractError("Containment setup exhausted the execution deadline")
            result = execute([self.executable, "start", "--attach", name], workspace, logs,
                             timeout=remaining, cancelled=cancelled, on_started=on_started)
            if result.outcome == "completed":
                completed = self.inspect(name)
                if not completed or completed.get("State", {}).get("Running") is not False:
                    raise ContractError("Container execution result cannot be reconciled")
                # The daemon's real container exit code is authoritative.
                result = ProcessResult(completed["State"]["ExitCode"], result.outcome, result.elapsed_ms,
                                       result.stdout, result.stderr)
            return result
        finally:
            self.reconcile(name, owner)

    def probe(self, directory: Path) -> dict:
        result = {"backend": "docker", "verified": False, "capabilities": {}, "identity": None}
        try:
            info = json.loads(self.control(["info", "--format", "{{json .}}"], 5))
            if info.get("OSType") != "linux":
                raise ContractError("Protected backend requires Linux containers")
            if not any(str(option).startswith("name=seccomp") for option in info.get("SecurityOptions", [])):
                raise ContractError("Protected backend requires the Docker seccomp boundary")
            workspace = directory / ("probe-" + uuid4().hex)
            workspace.mkdir(parents=True)
            sentinel = directory / ("outside-" + uuid4().hex)
            sentinel.write_text("host-only")
            script = ("import json,os,socket; "
                      "r={'nonroot':os.getuid()!=0,'host_hidden':not os.path.exists(" + repr(str(sentinel)) + ")}; "
                      "\ntry: open('/loop-write-probe','w').close(); r['readonly']=False"
                      "\nexcept OSError: r['readonly']=True"
                      "\ns=socket.socket(); s.settimeout(.2)"
                      "\ntry: s.connect(('1.1.1.1',443)); r['network_denied']=False"
                      "\nexcept OSError: r['network_denied']=True"
                      "\nprint(json.dumps(r))")
            name = self.name()
            observed = self.execute(["python3", "-c", script], workspace, ".", workspace.parent / (name + "-logs"),
                                    timeout=10, name=name, owner="probe")
            facts = json.loads(observed.stdout.read_text()) if observed.exit_code == 0 else {}
            if facts != {"nonroot": True, "host_hidden": True, "readonly": True, "network_denied": True}:
                raise ContractError("Filesystem, identity, or network probe did not pass")
            name = self.name()
            cancelled = self.execute(["python3", "-c", "import time; time.sleep(30)"], workspace, ".",
                                     workspace.parent / (name + "-logs"), timeout=2, name=name, owner="probe")
            if cancelled.outcome != "timeout" or self.inspect(name) is not None:
                raise ContractError("Bounded container cancellation probe did not pass")
            result.update(verified=True, identity=canonical_digest({"server": info.get("ID"),
                          "version": info.get("ServerVersion"), "policy": self.config}),
                          capabilities={name: True for name in ("snapshot_capture", "trusted_evidence", "bounded_execution",
                          "isolated_workspace", "policy_enforcement", "durable_state", "run_cancellation")})
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            result["reason"] = str(exc)
        self.last_probe = result
        return result


def runtime_for(config: dict):
    return LocalRuntime() if config["backend"] == "local" else DockerRuntime(config)

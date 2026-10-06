"""Host-owned HMAC authorities, purpose separation, and explicit revocation.

Keys must never be mounted into an execution workspace. Cryptographic integrity
does not protect against an administrator who owns both the store and its keys.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from uuid import uuid4

from reference.core import canonical_digest, strict_json_loads
from .contracts import ContractError
from .native_contracts import validate_native
from .workspace import atomic_write

PURPOSES = {"controller": {"event", "evaluation_request"}, "collector": {"evidence"},
            "reviewer": {"evidence"}, "human": {"evidence"},
            "interaction": {"evidence"}, "artifact": {"evidence"}}


class Authorities:
    def __init__(self, directory: Path):
        if directory.is_symlink():
            raise ContractError("Authority directory cannot be a symlink")
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)

    def path(self, key_id: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", key_id):
            raise ContractError("Invalid authority key ID")
        path = self.directory / (key_id + ".json")
        if path.is_symlink():
            raise ContractError("Authority key cannot be a symlink")
        return path

    def create(self, key_id: str, role: str) -> None:
        if role not in PURPOSES:
            raise ContractError("Unknown authority role")
        value = {"role": role, "enabled": True, "secret": secrets.token_hex(32)}
        descriptor = os.open(self.path(key_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
            handle.flush(); os.fsync(handle.fileno())

    def key(self, key_id: str) -> dict:
        path = self.path(key_id)
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise ContractError("Authority key permissions must be private")
        value = strict_json_loads(path.read_text())
        if (set(value) != {"role", "enabled", "secret"} or value["role"] not in PURPOSES
                or value["enabled"] is not True or not re.fullmatch(r"[a-f0-9]{64}", value["secret"])):
            raise ContractError("Authority key is revoked or invalid")
        return value

    def revoke(self, key_id: str) -> None:
        value = self.key(key_id)
        value["enabled"] = False
        atomic_write(self.path(key_id), (json.dumps(value) + "\n").encode(), 0o600)

    def sign(self, key_id: str, purpose: str, payload: dict) -> dict:
        key = self.key(key_id)
        if purpose not in PURPOSES[key["role"]]:
            raise ContractError("Authority cannot sign this purpose")
        body = {"schema_version": "1.0", "key_id": key_id, "role": key["role"],
                "purpose": purpose, "nonce": uuid4().hex, "payload": payload}
        body["mac"] = hmac.new(bytes.fromhex(key["secret"]), canonical_digest(body).encode(), hashlib.sha256).hexdigest()
        validate_native("attestation", body)
        return body

    def verify(self, envelope: dict, purpose: str, *, key_id: str | None = None, role: str | None = None) -> dict:
        validate_native("attestation", envelope)
        key = self.key(envelope["key_id"])
        if (envelope["purpose"] != purpose or purpose not in PURPOSES[key["role"]]
                or envelope["role"] != key["role"] or (key_id and envelope["key_id"] != key_id)
                or (role and key["role"] != role)):
            raise ContractError("Evidence authority or signing purpose does not match")
        body = {name: value for name, value in envelope.items() if name != "mac"}
        expected = hmac.new(bytes.fromhex(key["secret"]), canonical_digest(body).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, envelope["mac"]):
            raise ContractError("Authenticated record failed signature verification")
        return envelope["payload"]

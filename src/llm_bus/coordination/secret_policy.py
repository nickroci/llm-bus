"""Independent machine-owner anchors for exact development secret consent."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import cast

from llm_bus.errors import BusError
from llm_bus.ssh_signatures import verification_command

POLICY_PATH = Path("/etc/llm-bus/development-secrets.json")
NAMESPACE = "llm-bus-dev-secret-v1"
DENIED = "Development secret consent denied"
MAX_BYTES = 32768
KEY_FIELDS = 2
MAX_SIGNATURE = 8192


def _protected_bytes() -> bytes:
    """Resolve macOS /etc, then reject writable files, directories and ACL grants."""
    try:
        if os.geteuid() == 0:
            raise BusError(DENIED)
        path = POLICY_PATH.resolve(strict=True)
        for entry in (path, *path.parents):
            info = entry.stat()
            if info.st_uid != 0 or info.st_mode & 0o022 or os.access(entry, os.W_OK):
                raise BusError(DENIED)
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise BusError(DENIED)
            return stream.read(MAX_BYTES + 1)
    except OSError as exc:
        raise BusError(DENIED) from exc


def load() -> dict[str, object]:
    """There is deliberately no caller-supplied path, environment override or key pin."""
    try:
        value = cast("object", json.loads(_protected_bytes()))
    except (ValueError, UnicodeError) as exc:
        raise BusError(DENIED) from exc
    if not isinstance(value, dict):
        raise BusError(DENIED)
    policy = cast("dict[str, object]", value)
    if set(policy) != {"version", "machine", "owners", "targets", "revoked"}:
        raise BusError(DENIED)
    # pylint: disable=unidiomatic-typecheck
    if (
        type(policy["version"]) is not int
        or policy["version"] != 1
        or not isinstance(policy["machine"], str)
    ):
        raise BusError(DENIED)
    if not all(isinstance(policy[name], list) for name in ("targets", "revoked")):
        raise BusError(DENIED)
    if not isinstance(policy["owners"], dict):
        raise BusError(DENIED)
    return policy


def verify(payload: dict[str, str], data: bytes, signature: str) -> None:
    """Verify only a host-owner configured signer and exact development target tuple."""
    policy = load()
    owners = cast("dict[str, object]", policy["owners"])
    key = owners.get(payload["owner"])
    target = {name: payload[name] for name in ("provider", "account", "provider_project", "target")}
    target["environment"] = "development"
    if (
        policy["machine"] != payload["actor_machine"]
        or payload["receipt_id"] in cast("list[object]", policy["revoked"])
        or target not in cast("list[object]", policy["targets"])
    ):
        raise BusError(DENIED)
    if (
        not isinstance(key, str)
        or not key.startswith("ssh-ed25519 ")
        or len(key.split()) != KEY_FIELDS
        or not signature.startswith("-----BEGIN SSH SIGNATURE-----")
        or len(signature) > MAX_SIGNATURE
    ):
        raise BusError(DENIED)
    try:
        with tempfile.TemporaryDirectory(prefix="llm-bus-dev-consent-") as directory:
            allowed = Path(directory) / "allowed"
            signed = Path(directory) / "signature"
            allowed.write_text(f'owner namespaces="{NAMESPACE}" {key}\n', encoding="ascii")
            signed.write_text(signature, encoding="ascii")
            result = subprocess.run(  # noqa: S603 - fixed verifier argv, no shell
                verification_command("/usr/bin/ssh-keygen", allowed, "owner", NAMESPACE, signed),
                input=data,
                capture_output=True,
                check=False,
                timeout=15,
            )
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise BusError(DENIED) from exc
    if result.returncode != 0 or load() != policy:
        raise BusError(DENIED)

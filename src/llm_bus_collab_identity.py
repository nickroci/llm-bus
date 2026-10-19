"""GitHub-bound identity: SSH signing keys published on a GitHub account."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

from llm_bus_collab_state import CollabError, github_user

if TYPE_CHECKING:
    from http.client import HTTPResponse

NAMESPACE = "llm-bus-collab"
KEYS_URL = "https://api.github.com/users/{user}/ssh_signing_keys"
TEST_KEYS_ENV = "LLM_BUS_COLLAB_TEST_KEYS_DIR"
TIMEOUT_SECONDS = 15
MAX_KEYS_BYTES = 256 * 1024
MAX_SIGNATURE_CHARS = 8192
HTTP_NOT_FOUND = 404
PUBLIC_KEY_FIELDS = 2
LOOKUP_FAILED = "GitHub key lookup failed"
SIGNATURE_HEADER = "-----BEGIN SSH SIGNATURE-----"


def _ssh_keygen() -> str:
    binary = shutil.which("ssh-keygen")
    if binary is None:
        raise CollabError("ssh-keygen is required for collaboration identity")
    return binary


def test_keys_override() -> Path | None:
    """Local key directory standing in for GitHub; for tests only, shown in the owner UI."""
    configured = os.environ.get(TEST_KEYS_ENV)
    return Path(configured) if configured else None


def published_keys(user: str) -> list[str]:
    """Public SSH signing keys GitHub lists for a user."""
    github_user(user)
    override = test_keys_override()
    if override is not None:
        path = override / f"{user.lower()}.keys"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return [line.strip() for line in text.splitlines() if line.strip()]
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        KEYS_URL.format(user=user),
        headers={"Accept": "application/vnd.github+json", "User-Agent": "llm-bus"},
    )
    try:
        with cast(
            "HTTPResponse",
            urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS),  # noqa: S310 - https URL
        ) as response:
            data = cast("object", json.loads(response.read(MAX_KEYS_BYTES)))
    except urllib.error.HTTPError as exc:
        if exc.code == HTTP_NOT_FOUND:
            return []
        raise CollabError(f"{LOOKUP_FAILED} with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CollabError(LOOKUP_FAILED) from exc
    if not isinstance(data, list):
        raise CollabError("GitHub key lookup returned an unexpected response")
    keys: list[str] = []
    for item in cast("list[object]", data):
        key = cast("dict[str, object]", item).get("key") if isinstance(item, dict) else None
        if isinstance(key, str) and key.startswith(("ssh-", "sk-")):
            keys.append(key.strip())
    return keys


def fingerprint(key_line: str) -> str:
    """OpenSSH SHA256 fingerprint of a public key line."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [_ssh_keygen(), "-lf", "-"],
        input=key_line,
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise CollabError("Unreadable SSH public key")
    return result.stdout.split()[1]


def public_key(signing_key: Path) -> str:
    """Public half for a private key path, or the key itself for an agent-held .pub path."""
    pub = (
        signing_key
        if signing_key.suffix == ".pub"
        else signing_key.with_name(signing_key.name + ".pub")
    )
    if not pub.exists():
        raise CollabError(f"Missing public key {pub}")
    fields = pub.read_text(encoding="utf-8").split()
    if len(fields) < PUBLIC_KEY_FIELDS:
        raise CollabError(f"Unreadable public key {pub}")
    return f"{fields[0]} {fields[1]}"


def is_published(user: str, signing_key: Path) -> bool:
    local = public_key(signing_key)
    return any(
        local.split()[:PUBLIC_KEY_FIELDS] == key.split()[:PUBLIC_KEY_FIELDS]
        for key in published_keys(user)
    )


def generate_key(path: Path, user: str) -> Path:
    """Create an ed25519 signing key; never overwrite an existing file."""
    if path.exists() or path.with_name(path.name + ".pub").exists():
        raise CollabError(f"{path} already exists; choose it with --key or another path")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [_ssh_keygen(), "-q", "-t", "ed25519", "-N", "", "-C", f"llm-bus {user}", "-f", str(path)],
        check=True,
        timeout=TIMEOUT_SECONDS,
    )
    return path


class Handshake(NamedTuple):
    """One pairing's facts; both endpoint IDs are authenticated by the transport."""

    host_id: str
    joiner_id: str
    nonce: str
    host_user: str
    joiner_user: str


def transcript(role: str, handshake: Handshake) -> bytes:
    """Bytes each side signs: both authenticated endpoint IDs, the invite nonce, both users."""
    fields = {
        "v": 0,
        "role": role,
        "host_id": handshake.host_id,
        "joiner_id": handshake.joiner_id,
        "nonce": handshake.nonce,
        "host_user": handshake.host_user.lower(),
        "joiner_user": handshake.joiner_user.lower(),
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def sign(signing_key: Path, data: bytes) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [_ssh_keygen(), "-Y", "sign", "-q", "-f", str(signing_key), "-n", NAMESPACE],
        input=data,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise CollabError("SSH signing failed; check the key path or unlock it in ssh-agent")
    return result.stdout.decode("ascii")


def verify(user: str, data: bytes, signature: str) -> str | None:
    """Return the matching key fingerprint when a published key of `user` signed `data`."""
    if not signature.startswith(SIGNATURE_HEADER) or len(signature) > MAX_SIGNATURE_CHARS:
        return None
    for key in published_keys(user):
        with tempfile.TemporaryDirectory(prefix="llm-bus-verify-") as temporary:
            allowed = Path(temporary) / "allowed_signers"
            allowed.write_text(f'{user.lower()} namespaces="{NAMESPACE}" {key}\n', encoding="utf-8")
            signature_file = Path(temporary) / "signature"
            signature_file.write_text(signature, encoding="ascii")
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    _ssh_keygen(),
                    "-Y",
                    "verify",
                    "-f",
                    str(allowed),
                    "-I",
                    user.lower(),
                    "-n",
                    NAMESPACE,
                    "-s",
                    str(signature_file),
                ],
                input=data,
                capture_output=True,
                check=False,
                timeout=TIMEOUT_SECONDS,
            )
        if result.returncode == 0:
            return fingerprint(key)
    return None


def confirm_code(handshake: Handshake) -> str:
    """Short code both people can compare; derived from both endpoints and the nonce."""
    material = f"llm-bus-collab-code|{handshake.host_id}|{handshake.joiner_id}|{handshake.nonce}"
    digest = hashlib.sha256(material.encode()).digest()
    groups = (int.from_bytes(digest[index : index + 2]) % 10000 for index in (0, 2, 4))
    return "-".join(f"{group:04d}" for group in groups)

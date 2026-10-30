"""Machine-owned, fail-closed remote capability policy."""

from __future__ import annotations

import os
import re
import stat
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

import llm_bus_store

ADDRESS_PARTS = 3

CONFIG_ENV = "LLM_BUS_CONFIG"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,128}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
NATIVE = re.compile(r"(claude|codex):[A-Za-z0-9_-]{1,128}")
POLICY_VERSION = 2
REPORT_REASONS: dict[str, tuple[str | None, ...]] = {
    "received": (None,),
    "proceeding": ("started", "resumed"),
    "not_proceeding": (
        "needs_approval",
        "missing_information",
        "dependency_blocked",
        "out_of_scope",
        "declined",
    ),
    "completed": (None,),
    "failed": ("validation_failed", "execution_failed", "permission_denied", "unavailable"),
}
REPORT_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
MAX_REVISION = 2**53 - 1


class Report(TypedDict):
    message_id: str
    revision: int
    status: str | None
    reason: str | None


def validate_status(status: object, reason: object) -> tuple[str, str | None]:
    if (
        not isinstance(status, str)
        or status not in REPORT_REASONS
        or (reason is not None and not isinstance(reason, str))
        or reason not in REPORT_REASONS[status]
    ):
        raise llm_bus_store.BusError("Invalid built-in report status/reason")
    return status, reason


def parse_report(value: object, message_id: str) -> Report:
    data = object_fields(value)
    revision = data.get("revision")
    if (
        set(data) != {"message_id", "revision", "status", "reason"}
        or not REPORT_UUID.fullmatch(message_id)
        or data.get("message_id") != message_id
    ):
        raise llm_bus_store.BusError("Invalid work report response")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 0 <= revision <= MAX_REVISION
    ):
        raise llm_bus_store.BusError("Invalid work report revision")
    if revision == 0:
        if data["status"] is not None or data["reason"] is not None:
            raise llm_bus_store.BusError("Invalid initial work report")
        return Report(message_id=message_id, revision=0, status=None, reason=None)
    status, reason = validate_status(data["status"], data["reason"])
    return Report(message_id=message_id, revision=revision, status=status, reason=reason)


def object_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise llm_bus_store.BusError("Remote data must be an object")
    return cast("dict[str, object]", value)


def required_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise llm_bus_store.BusError("Remote field must be a nonempty string")
    return value


def identifier(value: object) -> str:
    result = required_text(value)
    if not IDENTIFIER.fullmatch(result):
        raise llm_bus_store.BusError("Invalid remote machine/repository ID")
    return result


def array_values(value: object) -> list[object]:
    if not isinstance(value, list):
        raise llm_bus_store.BusError("Remote field must be an array")
    return cast("list[object]", value)


def _keys(data: dict[str, object], allowed: set[str]) -> None:
    if set(data) - allowed:
        raise llm_bus_store.BusError(
            "Unknown or legacy local policy keys; migrate to policy_version = 2"
        )


@dataclass(frozen=True)
class PeerPolicy:
    machine_id: str
    send_messages: bool = False
    receive_messages: str = "deny"
    send_reports: tuple[str, ...] = ()
    receive_reports: tuple[str, ...] = ()


@dataclass(frozen=True)
class Repository:
    scope: str
    repo_id: str
    peers: tuple[PeerPolicy, ...] = ()


@dataclass(frozen=True)
class Config:
    url: str
    machine_id: str
    credential_file: Path
    repositories: tuple[Repository, ...]

    @property
    def connection(self) -> str:
        return f"{self.url}|{self.machine_id}"

    def repository(self, project: str) -> Repository | None:
        scope = llm_bus_store.routing_scope(project)
        return self.repository_scope(scope)

    def repository_scope(self, scope: str) -> Repository | None:
        """Match stored canonical scope without resolving a historical folder."""
        return next((repo for repo in self.repositories if repo.scope == scope), None)

    def peer(self, repo_id: str, machine_id: str) -> PeerPolicy | None:
        return next(
            (
                peer
                for repo in self.repositories
                if repo.repo_id == repo_id
                for peer in repo.peers
                if peer.machine_id == machine_id
            ),
            None,
        )


def _reports(value: object) -> tuple[str, ...]:
    values = tuple(required_text(item) for item in array_values(value))
    if len(set(values)) != len(values) or any(item not in REPORT_REASONS for item in values):
        raise llm_bus_store.BusError("Report permissions require unique built-in statuses")
    return values


def _peer(value: object, machine: str) -> PeerPolicy:
    data = object_fields(value)
    _keys(
        data, {"machine_id", "send_messages", "receive_messages", "send_reports", "receive_reports"}
    )
    peer = identifier(data.get("machine_id"))
    if peer == machine:
        raise llm_bus_store.BusError("Self-machine peer policy is invalid")
    # A missing capability denies access; this boolean is data, not a mode selector.
    send = data.get("send_messages", False)  # pylint: disable=clean-code-boolean-flag-argument
    receive = data.get("receive_messages", "deny")
    if not isinstance(send, bool) or receive not in ("deny", "monitor", "inbox"):
        raise llm_bus_store.BusError("Invalid message capability policy")
    return PeerPolicy(
        peer,
        send,
        cast("str", receive),
        _reports(data.get("send_reports", [])),
        _reports(data.get("receive_reports", [])),
    )


def _repository(value: object, base: Path, machine: str) -> Repository:
    data = object_fields(value)
    _keys(data, {"path", "repo_id", "peers"})
    repo_id = identifier(data.get("repo_id"))
    path = Path(required_text(data.get("path"))).expanduser()
    if not path.is_absolute():
        path = base / path
    peers = tuple(_peer(item, machine) for item in array_values(data.get("peers", [])))
    if len({peer.machine_id for peer in peers}) != len(peers):
        raise llm_bus_store.BusError("Duplicate peer policy")
    return Repository(llm_bus_store.routing_scope(str(path.resolve())), repo_id, peers)


def _load_config() -> Config | None:
    configured = os.environ.get(CONFIG_ENV)
    path = (
        Path(configured).expanduser() if configured else Path.home() / ".config/llm-bus/config.toml"
    )
    if not path.exists() and configured is None:
        return None
    try:
        data = cast("dict[str, object]", tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise llm_bus_store.BusError("Invalid or unavailable remote configuration TOML") from error
    _keys(data, {"policy_version", "remote", "repositories"})
    version = data.get("policy_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != POLICY_VERSION:
        raise llm_bus_store.BusError(
            "Local policy requires policy_version = 2; migrate legacy configuration"
        )
    remote = object_fields(data.get("remote"))
    _keys(remote, {"url", "machine_id", "credential_file"})
    url = required_text(remote.get("url")).rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    # Origins cannot carry credentials, paths, queries, or fragments.
    if (  # pylint: disable=too-many-boolean-expressions
        (parsed.scheme != "https" and not local_http)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise llm_bus_store.BusError(
            "Relay URL must be an HTTPS origin (HTTP allowed only on loopback)"
        )
    credential = Path(required_text(remote.get("credential_file"))).expanduser()
    if not credential.is_absolute():
        credential = path.parent / credential
    machine = identifier(remote.get("machine_id"))
    repositories = tuple(
        _repository(item, path.parent, machine) for item in array_values(data.get("repositories"))
    )
    if len({repo.scope for repo in repositories}) != len(repositories) or len(
        {repo.repo_id for repo in repositories}
    ) != len(repositories):
        raise llm_bus_store.BusError(
            "Duplicate remote repository mappings; map linked worktrees once"
        )
    return Config(url, machine, credential, repositories)


def load_config() -> Config | None:
    try:
        return _load_config()
    except llm_bus_store.BusError:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise llm_bus_store.BusError("Invalid or unavailable remote configuration") from error


def optional_config() -> Config | None:
    """Local-only operations survive missing or invalid remote configuration."""
    try:
        return load_config()
    except llm_bus_store.BusError:
        return None


def require_config() -> Config:
    config = load_config()
    if config is None:
        raise llm_bus_store.BusError("Remote command requires a remote configuration file")
    return config


class MessageIdentity(TypedDict):
    sender: str
    recipient: str
    repo_id: str


def receive_mode(item: MessageIdentity, scope: str, current_scope: str, connection: str) -> str:
    """Check original qualified identities and both stored/current repository scopes."""
    config = optional_config()
    if config is None or config.connection != connection or scope != current_scope:
        return "deny"
    repo_id = item["repo_id"]
    source = item["sender"].split("/")
    target = item["recipient"].split("/")
    if len(source) != ADDRESS_PARTS or len(target) != ADDRESS_PARTS:
        return "deny"
    if (
        source[1] != repo_id
        or target[1] != repo_id
        or target[0] != config.machine_id
        or not NATIVE.fullmatch(source[2])
        or not NATIVE.fullmatch(target[2])
    ):
        return "deny"
    repo = next((item for item in config.repositories if item.repo_id == repo_id), None)
    peer = config.peer(repo_id, source[0])
    if repo is None or repo.scope != scope or peer is None:
        return "deny"
    return peer.receive_messages


def permits_inbox(item: MessageIdentity, scope: str, current_scope: str, connection: str) -> bool:
    return receive_mode(item, scope, current_scope, connection) == "inbox"


def read_token(path: Path) -> str:
    if path.is_symlink():
        raise llm_bus_store.BusError("Credential file must not be a symlink")
    # Platform flag names are fixed OS APIs, not configurable policy.
    # pylint: disable-next=clean-code-business-policy-literal
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise llm_bus_store.BusError("Credential must be a regular file")
        if os.name == "posix" and (metadata.st_mode & 0o077 or metadata.st_uid != os.getuid()):
            raise llm_bus_store.BusError("Credential file must be owned by this user with mode 600")
        token = stream.read(129).strip()
    try:
        result = token.decode("ascii")
    except UnicodeDecodeError as error:
        raise llm_bus_store.BusError("Invalid machine credential format") from error
    if not TOKEN_PATTERN.fullmatch(result):
        raise llm_bus_store.BusError("Invalid machine credential format")
    return result

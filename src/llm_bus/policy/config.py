"""Machine-owned, fail-closed remote capability policy."""

from __future__ import annotations

import os
import re
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from llm_bus.coordination.models import GATE_OPERATIONS
from llm_bus.errors import BusError
from llm_bus.hosts.kinds import KIND_PATTERN
from llm_bus.policy.fields import array_values, identifier, object_fields, required_text
from llm_bus.reports.protocol import REPORT_REASONS
from llm_bus.routing import routing_scope

CONFIG_ENV = "LLM_BUS_CONFIG"
NATIVE = re.compile(rf"({KIND_PATTERN}):[A-Za-z0-9_-]{{1,128}}")
POLICY_VERSION = 2


def _keys(data: dict[str, object], allowed: set[str]) -> None:
    if set(data) - allowed:
        raise BusError("Unknown or legacy local policy keys; migrate to policy_version = 2")


@dataclass(frozen=True)
class PeerPolicy:
    machine_id: str
    send_messages: bool = False
    receive_messages: str = "deny"
    send_reports: tuple[str, ...] = ()
    receive_reports: tuple[str, ...] = ()
    accept_delegated_gates: tuple[str, ...] = ()


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
        scope = routing_scope(project)
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
        raise BusError("Report permissions require unique built-in statuses")
    return values


def _gates(value: object) -> tuple[str, ...]:
    values = tuple(required_text(item) for item in array_values(value))
    if len(set(values)) != len(values) or any(
        item not in GATE_OPERATIONS.values() for item in values
    ):
        raise BusError("Delegated gate permissions require unique built-in gates")
    return values


def _peer(value: object, machine: str) -> PeerPolicy:
    data = object_fields(value)
    _keys(
        data,
        {
            "machine_id",
            "send_messages",
            "receive_messages",
            "send_reports",
            "receive_reports",
            "accept_delegated_gates",
        },
    )
    peer = identifier(data.get("machine_id"))
    if peer == machine:
        raise BusError("Self-machine peer policy is invalid")
    # A missing capability denies access; this boolean is data, not a mode selector.
    send = data.get("send_messages", False)  # pylint: disable=clean-code-boolean-flag-argument
    receive = data.get("receive_messages", "deny")
    if not isinstance(send, bool) or receive not in ("deny", "monitor", "inbox"):
        raise BusError("Invalid message capability policy")
    gates = _gates(data.get("accept_delegated_gates", []))
    if gates and receive != "inbox":
        raise BusError('accept_delegated_gates needs receive_messages = "inbox"')
    return PeerPolicy(
        peer,
        send,
        cast("str", receive),
        _reports(data.get("send_reports", [])),
        _reports(data.get("receive_reports", [])),
        gates,
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
        raise BusError("Duplicate peer policy")
    return Repository(routing_scope(str(path.resolve())), repo_id, peers)


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
        raise BusError("Invalid or unavailable remote configuration TOML") from error
    _keys(data, {"policy_version", "remote", "repositories"})
    version = data.get("policy_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != POLICY_VERSION:
        raise BusError("Local policy requires policy_version = 2; migrate legacy configuration")
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
        raise BusError("Relay URL must be an HTTPS origin (HTTP allowed only on loopback)")
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
        raise BusError("Duplicate remote repository mappings; map linked worktrees once")
    return Config(url, machine, credential, repositories)


def load_config() -> Config | None:
    try:
        return _load_config()
    except BusError:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise BusError("Invalid or unavailable remote configuration") from error


def optional_config() -> Config | None:
    """Local-only operations survive missing or invalid remote configuration."""
    try:
        return load_config()
    except BusError:
        return None


def require_config() -> Config:
    config = load_config()
    if config is None:
        raise BusError("Remote command requires a remote configuration file")
    return config

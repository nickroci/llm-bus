"""Resolve display references once, before exact-identity command processing."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from llm_bus.errors import BusError
from llm_bus.hosts.kinds import KIND_PATTERN
from llm_bus.mailbox.labels import validate_label
from llm_bus.policy.config import optional_config
from llm_bus.remote.discovery import remote_peers
from llm_bus.routing import routing_scope

if TYPE_CHECKING:
    from llm_bus.mailbox.store import Store

QUALIFIED_PARTS = 3


def resolve_reference(store: Store, value: str, project: str) -> str:
    """Return one native identity; labels convey no authorization."""
    if re.fullmatch(rf"(?:[^/:\s]+/[^/:\s]+/)?(?:{KIND_PATTERN}):[^/:\s]+", value):
        return value
    if "/" not in value:
        return store.resolve_label(value, project)
    parts = value.split("/")
    if len(parts) != QUALIFIED_PARTS or any(
        not part or ":" in part or any(char.isspace() for char in part) for part in parts
    ):
        raise BusError("Expected machine/repository/label reference")
    machine, repository, label = parts
    _, key = validate_label(label)
    config = optional_config()
    if config is None:
        raise BusError("Qualified labels require remote configuration")
    if machine == config.machine_id:
        repo = next((repo for repo in config.repositories if repo.repo_id == repository), None)
        if repo is None:
            raise BusError("Unknown local repository")
        for agent in store.agents():
            if routing_scope(agent["project"]) == repo.scope:
                return store.resolve_label(label, agent["project"])
        raise BusError("Unknown label in local repository")
    matches = [
        peer["address"]
        for peer in remote_peers(store, config, None)
        if peer["machine_id"] == machine
        and peer["repo_id"] == repository
        and str(peer.get("label", "")).lower() == key
    ]
    if len(matches) != 1:
        raise BusError("Unknown or ambiguous remote label")
    return matches[0]

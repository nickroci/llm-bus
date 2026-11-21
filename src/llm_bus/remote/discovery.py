"""Discovery ownership."""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from llm_bus.errors import BusError
from llm_bus.hosts.sessions import is_subagent
from llm_bus.policy.fields import array_values
from llm_bus.remote.http import HTTP_GET, HTTP_POST
from llm_bus.remote.protocol import parse_peer
from llm_bus.remote.store import RemoteStore

if TYPE_CHECKING:
    from llm_bus.mailbox.store import Store
    from llm_bus.policy.config import Config, Repository
    from llm_bus.remote.models import Envelope, Peer

if TYPE_CHECKING:
    from llm_bus.remote.bridge import Bridge


def remote_peers(store: Store, config: Config, project: str | None) -> list[Peer]:
    selected = config.repository(project) if project is not None else None
    allowed = {
        repo.repo_id for repo in config.repositories if (project is None or repo == selected)
    }
    remote = RemoteStore(store, config.connection)
    peers = remote.peers()
    for peer in peers:
        peer["synchronization"] = remote.peer_snapshot(peer["repo_id"], datetime.now(UTC))
    return [
        peer
        for peer in peers
        if peer["repo_id"] in allowed
        and not peer["ended"]
        and (policy := config.peer(peer["repo_id"], peer["machine_id"])) is not None
        and policy.send_messages
    ]


def publish_sessions(self: Bridge) -> int:
    failures = 0
    published = self.remote.publication_payloads()
    for session in self.remote.sessions():
        repo = self.config.repository_scope(cast("str", session["scope"]))
        if repo is not None:
            payload = {
                "native_address": session["native_address"],
                "repo_id": repo.repo_id,
                "last_seen": session["last_seen"],
                "ended": session["ended_at"] is not None
                or is_subagent(str(session["native_address"])),
                "label": session["label"],
                "revision": session["revision"],
            }
            key = f"{repo.repo_id}/{session['native_address']}"
            serialized = json.dumps(payload, sort_keys=True)
            if published.get(key) == serialized:
                continue
            try:
                self.relay.request(HTTP_POST, "/v1/sessions", payload)
                self.remote.record_publication(key, serialized)
            except BusError:
                failures += 1
    return failures


def cache_peers(self: Bridge, repo: Repository) -> None:
    peers = [
        parse_peer(item, repo.repo_id, self.config.machine_id)
        for item in array_values(
            self.relay.request(
                HTTP_GET, "/v1/sessions?" + urllib.parse.urlencode({"repo_id": repo.repo_id})
            )
        )
    ]
    self.remote.cache(repo.repo_id, peers)


def publish_original_sender(self: Bridge, envelope: Envelope) -> None:
    key = f"{envelope['repo_id']}/{envelope['sender']}"
    if self.remote.published(key):
        return
    # A queued sender may have moved or ended before its first bridge pass.
    payload = {
        "native_address": envelope["sender"],
        "repo_id": envelope["repo_id"],
        "last_seen": self.remote.source_activity(envelope["id"]),
        "ended": True,
    }
    self.relay.request(HTTP_POST, "/v1/sessions", payload)
    self.remote.record_publication(key, json.dumps(payload, sort_keys=True))

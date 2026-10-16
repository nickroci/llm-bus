"""Opt-in remote policy, authenticated HTTPS transport, and polling bridge."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from typing import IO, TYPE_CHECKING, cast, override

from llm_bus_policy import (
    IDENTIFIER,
    NATIVE,
    Config,
    Repository,
    array_values,
    identifier,
    load_config,
    object_fields,
    read_token,
    required_text,
)
from llm_bus_remote_store import Delivery, Envelope, Peer, RemoteStore
from llm_bus_reports import Reports
from llm_bus_store import MAX_MESSAGE_BYTES, BusError, Store
from llm_bus_wake import wake

if TYPE_CHECKING:
    from http.client import HTTPMessage, HTTPResponse

    from llm_bus_store import SenderContext

CLIENT_USER_AGENT = "llm-bus/0.1"
HTTP_GET = "GET"
HTTP_POST = "POST"
WAKE_FAILED = "failed"
SYNC_INCOMPLETE = "Remote synchronization incomplete; pending state retained"
WAKE_ERROR_REASON = "wake adapter failed; message remains pending"
POLL_SECONDS = 5
ADDRESS_PARTS = 3
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib calls this exact public signature.
    # pylint: disable=unused-argument,too-many-arguments,too-many-positional-arguments
    """Never forward bearer credentials to a redirect target."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


class Relay:
    config: Config
    token: str

    def __init__(self, config: Config) -> None:
        self.config = config
        self.token = read_token(config.credential_file)

    def request(self, method: str, path: str, data: object = None) -> object:
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        request = urllib.request.Request(  # noqa: S310 - validated origin; no redirects
            self.config.url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": CLIENT_USER_AGENT,
            },
        )
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with cast("HTTPResponse", opener.open(request, timeout=10)) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise BusError(f"Relay rejected request (HTTP {error.code})") from None  # pylint: disable=clean-code-business-policy-literal
        except (OSError, urllib.error.URLError, HTTPException) as error:
            raise BusError("Relay unavailable; local transport state retained") from error
        if len(payload) > MAX_RESPONSE_BYTES:
            raise BusError("Relay response exceeds size limit")
        try:
            return cast("object", json.loads(payload))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BusError("Invalid relay response") from error


def parse_peer(value: object, repo_id: str, machine_id: str) -> Peer:
    data = object_fields(value)
    machine = identifier(data.get("machine_id"))
    native = required_text(data.get("native_address"))
    peer_address = required_text(data.get("address"))
    if (
        data.get("repo_id") != repo_id
        or machine == machine_id
        or not NATIVE.fullmatch(native)
        or peer_address != f"{machine}/{repo_id}/{native}"
    ):
        raise BusError("Invalid remote session identity")
    return Peer(
        address=peer_address,
        machine_id=machine,
        repo_id=repo_id,
        native_address=native,
        last_seen=required_text(data.get("last_seen")),
        ended=0,
    )


def remote_peers(store: Store, config: Config, project: str | None) -> list[Peer]:
    selected = config.repository(project) if project is not None else None
    allowed = {
        repo.repo_id for repo in config.repositories if (project is None or repo == selected)
    }
    peers = RemoteStore(store, config.connection).peers()
    return [
        peer
        for peer in peers
        if peer["repo_id"] in allowed
        and not peer["ended"]
        and (policy := config.peer(peer["repo_id"], peer["machine_id"])) is not None
        and policy.send_messages
    ]


def enqueue_remote(
    store: Store, config: Config, sender: SenderContext, recipient: str, body: str
) -> dict[str, object]:
    repo = config.repository(sender.project)
    if repo is None:
        raise BusError("Remote sending is disabled for this repository")
    parts = recipient.split("/")
    if (
        len(parts) != ADDRESS_PARTS
        or not IDENTIFIER.fullmatch(parts[0])
        or parts[0] == config.machine_id
        or parts[1] != repo.repo_id
        or not NATIVE.fullmatch(parts[2])
    ):
        raise BusError("Remote recipient must belong to another machine in the same repository")
    peer = config.peer(repo.repo_id, parts[0])
    if peer is None or not peer.send_messages:
        raise BusError("Local capability policy: message sending is disabled for this peer")
    return RemoteStore(store, config.connection).enqueue(sender, recipient, repo.repo_id, body)


def parse_delivery(value: object, repo: Repository, machine: str) -> Delivery:
    data = object_fields(value)
    recipient = required_text(data.get("recipient"))
    sender = required_text(data.get("sender"))
    for qualified in (sender, recipient):
        parts = qualified.split("/")
        if (
            len(parts) != ADDRESS_PARTS
            or not IDENTIFIER.fullmatch(parts[0])
            or parts[1] != repo.repo_id
            or not NATIVE.fullmatch(parts[2])
        ):
            raise BusError("Invalid remote message identity")
    if (
        not recipient.startswith(f"{machine}/")
        or sender.startswith(f"{machine}/")
        or data.get("repo_id") != repo.repo_id
    ):
        raise BusError("Invalid remote message routing")
    sequence = data.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise BusError("Invalid remote mailbox cursor")
    body = required_text(data.get("body"))
    if len(body.encode()) > MAX_MESSAGE_BYTES:
        raise BusError("Remote message exceeds 64 KiB")
    return Delivery(
        id=required_text(data.get("id")),
        sender=sender,
        recipient=recipient,
        repo_id=repo.repo_id,
        body=body,
        sequence=sequence,
        sent_at=required_text(data.get("sent_at")),
    )


class Bridge:
    config: Config
    store: Store
    remote: RemoteStore
    relay: Relay

    def __init__(self, store: Store, config: Config) -> None:
        self.config = config
        self.store = store
        self.remote = RemoteStore(store, config.connection)
        self.relay = Relay(config)

    def _current_config(self) -> Config | None:
        config = load_config()
        if config is None or config.connection != self.config.connection:
            return None
        return config

    def sync(self) -> dict[str, int]:
        current = self._current_config()
        if current is None:
            raise BusError("Remote policy unavailable or connection changed")
        self.config = current
        counts = {"uploaded": 0, "imported": 0, "acknowledged": 0, "errors": 0}
        counts["errors"] += self._publish_sessions()
        for repo in self.config.repositories:
            try:
                if any(peer.send_messages for peer in repo.peers):
                    self._cache_peers(repo)
            except BusError:
                counts["errors"] += 1
        counts["uploaded"], failures = self._sync_outgoing()
        counts["errors"] += failures
        for repo in self.config.repositories:
            try:
                if any(peer.receive_messages in {"inbox", "monitor"} for peer in repo.peers):
                    counts["imported"] += self._receive(repo)
            except BusError:
                counts["errors"] += 1
        counts["acknowledged"], failures = self._sync_acknowledgements()
        counts["errors"] += failures
        counts["errors"] += self._submit_reports()
        counts["errors"] += self._poll_reports()
        return counts

    def _submit_reports(self) -> int:
        reports = Reports(self.store, self.config.connection)
        failures = 0
        for local_id, submission in reports.pending():
            try:
                item = reports.imported(local_id)
                status = cast("str", submission["status"])
                if not reports.permitted(item, status):
                    continue
                # Retry durable delivery independently of current text reception policy.
                self.relay.request(HTTP_POST, "/v1/receipts", {"id": item["id"]})
                if reports.permitted(item, status):
                    reports.accepted(
                        submission, self.relay.request(HTTP_POST, "/v1/reports", submission)
                    )
            except BusError:
                failures += 1
        return failures

    def _poll_reports(self) -> int:
        reports = Reports(self.store, self.config.connection)
        failures = 0
        for envelope in reports.pollable():
            try:
                current = self._current_config()
                if current is None:
                    continue
                peer = current.peer(envelope["repo_id"], envelope["recipient"].split("/", 1)[0])
                repo = next(
                    (r for r in current.repositories if r.repo_id == envelope["repo_id"]), None
                )
                if (
                    peer is None
                    or not peer.receive_reports
                    or repo is None
                    or repo.scope != self.remote.source_scope(envelope["id"])
                ):
                    continue
                value = self.relay.request(
                    HTTP_GET,
                    "/v1/reports?" + urllib.parse.urlencode({"message_id": envelope["id"]}),
                )
                current = self._current_config()
                if current is not None:
                    reports.receive(envelope, value, current)
            except BusError:
                failures += 1
        return failures

    def _publish_sessions(self) -> int:
        failures = 0
        for session in self.remote.sessions():
            repo = self.config.repository(cast("str", session["project"]))
            if repo is not None:
                payload = {
                    "native_address": session["native_address"],
                    "repo_id": repo.repo_id,
                    "last_seen": session["last_seen"],
                    "ended": session["ended_at"] is not None,
                }
                key = f"{repo.repo_id}/{session['native_address']}"
                serialized = json.dumps(payload, sort_keys=True)
                if self.remote.published(key, serialized):
                    continue
                try:
                    self.relay.request(HTTP_POST, "/v1/sessions", payload)
                    self.remote.record_publication(key, serialized)
                except BusError:
                    failures += 1
        return failures

    def _sync_acknowledgements(self) -> tuple[int, int]:
        acknowledged = failures = 0
        for remote_id, _repo_id in self.remote.acknowledgements():
            if self.remote.permits_ack(remote_id):
                try:
                    self.relay.request(HTTP_POST, "/v1/ack", {"id": remote_id})
                    self.remote.mark_acknowledged(remote_id)
                    acknowledged += 1
                except BusError:
                    failures += 1
        return acknowledged, failures

    def _cache_peers(self, repo: Repository) -> None:
        peers = [
            parse_peer(item, repo.repo_id, self.config.machine_id)
            for item in array_values(
                self.relay.request(
                    HTTP_GET, "/v1/sessions?" + urllib.parse.urlencode({"repo_id": repo.repo_id})
                )
            )
        ]
        self.remote.cache(repo.repo_id, peers)

    def _sync_outgoing(self) -> tuple[int, int]:
        uploaded = failures = 0
        for envelope, state in self.remote.outgoing():
            if self._permits_outgoing(envelope):
                try:
                    if self._upload(envelope, state):
                        uploaded += 1
                except BusError:
                    failures += 1
        return uploaded, failures

    def _permits_outgoing(self, envelope: Envelope) -> bool:
        try:
            current = self._current_config()
        except BusError:
            return False
        if current is None:
            return False
        peer = current.peer(envelope["repo_id"], envelope["recipient"].split("/", 1)[0])
        repo = next(
            (item for item in current.repositories if item.repo_id == envelope["repo_id"]),
            None,
        )
        return (
            peer is not None
            and peer.send_messages
            and repo is not None
            and repo.scope == self.remote.source_scope(envelope["id"])
        )

    def _upload(self, envelope: Envelope, state: str) -> bool:
        if state == "queued_local":
            self._publish_original_sender(envelope)
            if not self._permits_outgoing(envelope):
                return False
            result = self.relay.request(HTTP_POST, "/v1/messages", envelope)
        else:
            result = self.relay.request(
                HTTP_GET, "/v1/status?" + urllib.parse.urlencode({"id": envelope["id"]})
            )
        status = object_fields(result).get("status")
        if status not in {"accepted_remote", "delivered_local", "acknowledged", "expired"}:
            raise BusError("Invalid remote message status")
        data = object_fields(result)
        if "report_expires_at" in data:
            Reports(self.store, self.config.connection).deadline(
                envelope["id"], data["report_expires_at"]
            )
        self.remote.set_state(envelope["id"], cast("str", status))
        return True

    def _publish_original_sender(self, envelope: Envelope) -> None:
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

    def _receive(self, repo: Repository) -> int:
        # The cursor is scoped to one scan. Persisting it could skip late Queue jobs.
        after = 0
        imported = 0
        while True:
            values = array_values(
                self.relay.request(
                    HTTP_GET,
                    "/v1/inbox?"
                    + urllib.parse.urlencode({"repo_id": repo.repo_id, "after": after}),
                )
            )
            if not values:
                return imported
            for value in values:
                item = parse_delivery(value, repo, self.config.machine_id)
                if item["sequence"] <= after:
                    raise BusError("Remote mailbox cursor did not advance")
                after = item["sequence"]
                admitted = self.remote.admit_message(item, repo.scope)
                if admitted is not None:
                    mode, local_id = admitted
                    if (
                        mode == "inbox"
                        and local_id is not None
                        and self.store.pending_allowed(local_id)
                        and self.remote.claim_wake(item["id"])
                    ):
                        self._wake(item, local_id)
                        imported += 1
                    self.relay.request(HTTP_POST, "/v1/receipts", {"id": item["id"]})

    def _wake(self, item: Delivery, local_id: int) -> None:
        native = item["recipient"].split("/", 2)[2]
        if not self.store.pending_allowed(local_id):
            return
        if self.store.is_ended(native):
            self.store.record_wake(
                local_id, WAKE_FAILED, "remote bridge", "recipient session ended"
            )
            return
        try:
            result = wake(
                native, local_id, item["sender"], claude_socket=self.store.messaging_socket(native)
            )
            self.store.record_wake(
                local_id, result["status"], result.get("via", "remote bridge"), result.get("reason")
            )
        except Exception:  # pylint: disable=broad-exception-caught
            self.store.record_wake(local_id, "unknown", "remote bridge", WAKE_ERROR_REASON)


def run_bridge(store: Store, *, once: bool) -> None:
    while True:
        config = load_config()
        if config is None:
            raise BusError("Remote bridge requires a remote configuration file")
        try:
            result: object = Bridge(store, config).sync()
        except BusError as error:
            if once:
                raise
            result = {"error": str(error)}
        print(json.dumps(result), flush=True)
        if once and result.get("errors"):
            raise BusError(SYNC_INCOMPLETE)
        if once:
            return
        time.sleep(POLL_SECONDS)

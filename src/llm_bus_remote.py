"""Opt-in remote policy, authenticated HTTPS transport, and polling bridge."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from http.client import HTTPException
from typing import IO, TYPE_CHECKING, cast, override

from llm_bus_dispatch import dispatch_due
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
from llm_bus_remote_store import Delivery, Envelope, Peer, RemoteStore, validate_reply_uuid
from llm_bus_reports import Reports, parse_report, report_deadline
from llm_bus_store import MAX_MESSAGE_BYTES, BusError, Store

if TYPE_CHECKING:
    from collections.abc import Callable
    from http.client import HTTPMessage, HTTPResponse

    from llm_bus_store import SenderContext

CLIENT_USER_AGENT = "llm-bus/0.1"
HTTP_GET = "GET"
HTTP_POST = "POST"
SYNC_INCOMPLETE = "Remote synchronization incomplete; pending state retained"
ADDRESS_PARTS = 3
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
UNSUPPORTED_BATCH = frozenset({404, 405, 501})
AUTHORIZATION_FAILURES = frozenset({401, 403})
BATCH_CAPABILITY_SECONDS = 300
TRANSPORT_STATES = frozenset({"accepted_remote", "delivered_local", "acknowledged", "expired"})


class RelayError(BusError):
    """Sanitized transport failure with machine-readable retry classification."""

    code: str
    http_status: int | None

    def __init__(self, code: str, *, http_status: int | None = None) -> None:
        self.code = code
        self.http_status = http_status
        if http_status is not None:
            message = f"Relay rejected request (HTTP {http_status})"
        elif code == "protocol_error":
            message = "Invalid relay response"
        elif code == "unsupported_reply_link":
            message = "Relay does not support reply links; linked message remains queued"
        else:
            message = "Relay unavailable; local transport state retained"
        super().__init__(message)


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
            code = "not_allowed" if error.code in AUTHORIZATION_FAILURES else "rejected"
            raise RelayError(code, http_status=error.code) from None
        except (OSError, urllib.error.URLError, HTTPException) as error:
            raise RelayError("unavailable") from error
        if len(payload) > MAX_RESPONSE_BYTES:
            raise RelayError("protocol_error")
        try:
            return cast("object", json.loads(payload))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RelayError("protocol_error") from error


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


def enqueue_remote(  # noqa: PLR0913 - additive reply keyword preserves callers
    store: Store,
    config: Config,
    sender: SenderContext,
    recipient: str,
    body: str,
    *,
    reply_to: int | None = None,
) -> dict[str, object]:
    # pylint: disable=too-many-arguments
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
    return RemoteStore(store, config.connection).enqueue(
        sender, recipient, repo.repo_id, body, scope=repo.scope, reply_to=reply_to
    )


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
    result = Delivery(
        id=required_text(data.get("id")),
        sender=sender,
        recipient=recipient,
        repo_id=repo.repo_id,
        body=body,
        sequence=sequence,
        sent_at=required_text(data.get("sent_at")),
    )
    if "reply_to" in data:
        parent = required_text(data["reply_to"])
        validate_reply_uuid(parent)
        result["reply_to"] = parent
    return result


class Bridge:
    # pylint: disable=too-many-instance-attributes
    config: Config
    store: Store
    remote: RemoteStore
    relay: Relay
    clock: Callable[[], datetime]
    refresh_status: bool

    def __init__(
        self,
        store: Store,
        config: Config,
        *,
        clock: Callable[[], datetime] | None = None,
        refresh_status: bool = False,
    ) -> None:
        self.config = config
        self.store = store
        self.remote = RemoteStore(store, config.connection)
        self.relay = Relay(config)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.refresh_status = refresh_status
        self.unsupported_until: dict[str, datetime] = {}
        self.reply_capability: tuple[datetime, bool] | None = None

    def _record(self, kind: str, key: str, error: BusError | None = None) -> None:
        code = (
            error.code if isinstance(error, RelayError) else ("protocol_error" if error else None)
        )
        self.remote.record_sync_result(kind, key, self.clock(), error_code=code)

    def _read_batch(self, kind: str, ids: list[str]) -> dict[str, tuple[object, str | None]]:
        path = "/v1/status" if kind == "status" else "/v1/reports"
        field = "ids" if kind == "status" else "message_ids"
        until = self.unsupported_until.get(kind)
        if until is None or self.clock() >= until:
            try:
                value = self.relay.request(HTTP_POST, path + "/batch", {field: ids})
            except RelayError as error:
                if error.http_status not in UNSUPPORTED_BATCH:
                    raise
                self.unsupported_until[kind] = self.clock() + timedelta(
                    seconds=BATCH_CAPABILITY_SECONDS
                )
            else:
                return self._validate_batch(kind, ids, value)
        result: dict[str, tuple[object, str | None]] = {}
        query_field = "id" if kind == "status" else "message_id"
        for key in ids:
            try:
                result[key] = (
                    self.relay.request(
                        HTTP_GET, path + "?" + urllib.parse.urlencode({query_field: key})
                    ),
                    None,
                )
            except BusError as error:
                code = error.code if isinstance(error, RelayError) else "unavailable"
                result[key] = (None, code)
        return result

    def _validate_batch(
        self, kind: str, ids: list[str], value: object
    ) -> dict[str, tuple[object, str | None]]:
        wrapper = object_fields(value)
        if set(wrapper) != {"results"}:
            raise RelayError("protocol_error")
        result: dict[str, tuple[object, str | None]] = {}
        for item in array_values(wrapper["results"]):
            data = object_fields(item)
            key = required_text(data.get("id"))
            if set(data) != {"id", "value", "error"} or key not in ids or key in result:
                raise RelayError("protocol_error")
            error = data["error"]
            if error is not None:
                details = object_fields(error)
                code = details.get("code")
                if (
                    set(details) != {"code"}
                    or not isinstance(code, str)
                    or code not in {"not_allowed", "unavailable"}
                    or data["value"] is not None
                ):
                    raise RelayError("protocol_error")
                result[key] = (None, cast("str", details["code"]))
            else:
                if kind == "status":
                    self._validate_status(key, data["value"], require_id=True)
                else:
                    parse_report(data["value"], key)
                result[key] = (data["value"], None)
        if set(result) != set(ids):
            raise RelayError("protocol_error")
        return result

    @staticmethod
    def _validate_status(key: str, value: object, *, require_id: bool = False) -> dict[str, object]:
        data = object_fields(value)
        status = data.get("status")
        if not isinstance(status, str) or status not in TRANSPORT_STATES:
            raise BusError("Invalid remote message status")
        if (require_id or "id" in data) and data.get("id") != key:
            raise BusError("Remote status message ID conflicts")
        if "report_expires_at" in data:
            report_deadline(data["report_expires_at"])
        return data

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
        self.relay.config = current
        self.relay.token = read_token(current.credential_file)
        counts = {"uploaded": 0, "imported": 0, "acknowledged": 0, "errors": 0}
        counts["errors"] += self._publish_sessions()
        for repo in self.config.repositories:
            snapshot = self.remote.peer_snapshot(repo.repo_id, self.clock())
            due = cast("str | None", snapshot["next_attempt_at"])
            if due is not None and self.clock() < datetime.fromisoformat(due):
                continue
            try:
                if any(peer.send_messages for peer in repo.peers):
                    self._cache_peers(repo)
                    self.remote.record_peer_result(repo.repo_id, self.clock(), error_code=None)
            except BusError as error:
                code = error.code if isinstance(error, RelayError) else "protocol_error"
                self.remote.record_peer_result(repo.repo_id, self.clock(), error_code=code)
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
        dispatch_due(self.store)
        return counts

    def _submit_reports(self) -> int:
        reports = Reports(self.store, self.config.connection)
        failures = 0
        for local_id, submission in reports.pending_items():
            key = f"{submission['message_id']}:{submission['expected_revision']}"
            if not self.remote.work_due("report_submit", key, self.clock()):
                continue
            try:
                item = reports.imported(local_id)
                status = cast("str", submission["status"])
                if not reports.permitted(item, status):
                    continue
                # Retry durable delivery independently of current text reception policy.
                if not self._receipt(item["id"]):
                    continue
                if reports.permitted(item, status):
                    reports.accepted(
                        submission, self.relay.request(HTTP_POST, "/v1/reports", submission)
                    )
                    self._record("report_submit", key)
            except BusError as error:
                self._record("report_submit", key, error)
                failures += 1
        return failures

    def _poll_reports(self) -> int:
        reports = Reports(self.store, self.config.connection)
        failures = 0
        for page in reports.pollable_pages(now=self.clock()):
            due = [
                envelope
                for envelope in page
                if self._permits_report(envelope)
                and self.remote.work_due(
                    "report_read", envelope["id"], self.clock(), force=self.refresh_status
                )
            ]
            failures += self._poll_page("report_read", due)
        return failures

    def _permits_report(self, envelope: Envelope) -> bool:
        current = self._current_config()
        if current is None:
            return False
        peer = current.peer(envelope["repo_id"], envelope["recipient"].split("/", 1)[0])
        repo = next((r for r in current.repositories if r.repo_id == envelope["repo_id"]), None)
        return (
            peer is not None
            and bool(peer.receive_reports)
            and repo is not None
            and repo.scope == self.remote.source_scope(envelope["id"])
        )

    def _poll_page(self, kind: str, envelopes: list[Envelope]) -> int:
        if not envelopes:
            return 0
        try:
            values = self._read_batch(
                "status" if kind == "status" else "report", [e["id"] for e in envelopes]
            )
        except BusError as error:
            for envelope in envelopes:
                self._record(kind, envelope["id"], error)
            return len(envelopes)
        failures = 0
        for envelope in envelopes:
            key = envelope["id"]
            value, code = values[key]
            try:
                if code:
                    raise RelayError(code)
                if kind == "status":
                    if self._permits_outgoing(envelope):
                        self._apply_status(envelope, value)
                else:
                    current = self._current_config()
                    if current is not None:
                        Reports(self.store, self.config.connection).receive(
                            envelope, value, current
                        )
                self._record(kind, key)
            except BusError as error:
                self._record(kind, key, error)
                failures += 1
        return failures

    def _publish_sessions(self) -> int:
        failures = 0
        for session in self.remote.sessions():
            repo = self.config.repository_scope(cast("str", session["scope"]))
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
            if self.remote.work_due("ack", remote_id, self.clock()) and self.remote.permits_ack(
                remote_id
            ):
                try:
                    self.relay.request(HTTP_POST, "/v1/ack", {"id": remote_id})
                    self.remote.mark_acknowledged(remote_id)
                    self._record("ack", remote_id)
                    acknowledged += 1
                except BusError as error:
                    self._record("ack", remote_id, error)
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
        for page in self.remote.outgoing_pages():
            status_due: list[Envelope] = []
            for envelope, state in page:
                if not self._permits_outgoing(envelope):
                    continue
                kind = "upload" if state == "queued_local" else "status"
                if not self.remote.work_due(
                    kind,
                    envelope["id"],
                    self.clock(),
                    force=self.refresh_status and kind == "status",
                ):
                    continue
                if kind == "status":
                    status_due.append(envelope)
                    continue
                try:
                    if self._upload(envelope):
                        uploaded += 1
                        self._record(kind, envelope["id"])
                        self._record("status", envelope["id"])
                except BusError as error:
                    self._record(kind, envelope["id"], error)
                    failures += 1
            status_failures = self._poll_page("status", status_due)
            failures += status_failures
            uploaded += len(status_due) - status_failures
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

    def _upload(self, envelope: Envelope) -> bool:
        if "reply_to" in envelope and not self._supports_reply():
            raise RelayError("unsupported_reply_link")
        self._publish_original_sender(envelope)
        if not self._permits_outgoing(envelope):
            return False
        result = self.relay.request(HTTP_POST, "/v1/messages", envelope)
        self._apply_status(envelope, result)
        return True

    def _supports_reply(self) -> bool:
        cached = self.reply_capability
        if cached is not None and self.clock() < cached[0]:
            return cached[1]
        try:
            data = object_fields(self.relay.request(HTTP_GET, "/v1/capabilities"))
        except RelayError as error:
            if error.http_status not in UNSUPPORTED_BATCH:
                raise
            supported = False
        else:
            if set(data) - {"reply_to", "batch_status", "batch_reports"} or any(
                not isinstance(value, bool) for value in data.values()
            ):
                raise RelayError("protocol_error")
            supported = data.get("reply_to") is True
        self.reply_capability = (
            self.clock() + timedelta(seconds=BATCH_CAPABILITY_SECONDS),
            supported,
        )
        return supported

    def _apply_status(self, envelope: Envelope, value: object) -> None:
        data = self._validate_status(envelope["id"], value)
        if "report_expires_at" in data:
            Reports(self.store, self.config.connection).deadline(
                envelope["id"], data["report_expires_at"]
            )
        self.remote.set_state(envelope["id"], cast("str", data["status"]))
        self.remote.observe_acceptance(envelope["id"], self.clock())

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
                    mode, local_id, created = admitted
                    if (
                        mode == "inbox"
                        and created
                        and local_id is not None
                        and self.store.pending_allowed(local_id)
                    ):
                        imported += 1
                    self._receipt(item["id"])

    def _receipt(self, key: str) -> bool:
        if not self.remote.work_due("receipt", key, self.clock()):
            return False
        try:
            self.relay.request(HTTP_POST, "/v1/receipts", {"id": key})
        except BusError as error:
            self._record("receipt", key, error)
            raise
        self._record("receipt", key)
        return True


def run_bridge(
    store: Store,
    *,
    once: bool,
    refresh_status: bool = False,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    if refresh_status and not once:
        raise BusError("--refresh-status requires --once")
    bridge: Bridge | None = None
    pause = sleep or time.sleep
    failures = 0
    while True:
        config = load_config()
        if config is None:
            raise BusError("Remote bridge requires a remote configuration file")
        try:
            if bridge is None or bridge.config.connection != config.connection:
                bridge = Bridge(store, config, refresh_status=refresh_status, clock=clock)
            result = bridge.sync()
            failures = 0
        except BusError as error:
            if once:
                raise
            failures += 1
            print(json.dumps({"error": str(error)}), flush=True)
            pause((5, 10, 20, 30)[min(failures - 1, 3)])
            continue
        print(json.dumps(result), flush=True)
        if once and result.get("errors"):
            raise BusError(SYNC_INCOMPLETE)
        if once:
            return
        pause(bridge.remote.sleep_seconds(bridge.clock()))

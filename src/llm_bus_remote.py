"""Opt-in remote policy, authenticated HTTPS transport, and polling bridge."""

from __future__ import annotations

import json
import os
import re
import stat
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast, override

from llm_bus_remote_store import Delivery, Envelope, Peer, RemoteStore
from llm_bus_store import MAX_MESSAGE_BYTES, BusError, Store, routing_scope
from llm_bus_wake import wake

if TYPE_CHECKING:
    from http.client import HTTPMessage, HTTPResponse

    from llm_bus_store import SenderContext

CONFIG_ENV = "LLM_BUS_CONFIG"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,128}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
NATIVE = re.compile(r"(claude|codex):[A-Za-z0-9_-]{1,128}")
HTTP_GET = "GET"
HTTP_POST = "POST"
WAKE_FAILED = "failed"
SYNC_INCOMPLETE = "Remote synchronization incomplete; pending state retained"
WAKE_ERROR_REASON = "wake adapter failed; message remains pending"
POLL_SECONDS = 5
ADDRESS_PARTS = 3
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BusError("Remote data must be an object")
    return cast("dict[str, object]", value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise BusError("Remote field must be a nonempty string")
    return value


def _identifier(value: object) -> str:
    result = _text(value)
    if not IDENTIFIER.fullmatch(result):
        raise BusError("Invalid remote machine/repository ID")
    return result


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise BusError("Remote field must be an array")
    return cast("list[object]", value)


@dataclass(frozen=True)
class Repository:
    scope: str
    repo_id: str
    send: bool
    receive: bool


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
        return next((repo for repo in self.repositories if repo.scope == scope), None)


def _repository(value: object, base: Path) -> Repository:
    data = _mapping(value)
    repo_id = _identifier(data.get("repo_id"))
    path = Path(_text(data.get("path"))).expanduser()
    if not path.is_absolute():
        path = base / path
    permissions: list[bool] = []
    for key in ("send_to", "receive_from"):
        allowed = [_identifier(item) for item in _array(data.get(key, [repo_id]))]
        if any(item != repo_id for item in allowed):
            raise BusError("Remote v1 permits same-repository routes only")
        permissions.append(repo_id in allowed)
    return Repository(
        routing_scope(str(path.resolve())), repo_id, send=permissions[0], receive=permissions[1]
    )


def load_config() -> Config | None:
    configured = os.environ.get(CONFIG_ENV)
    path = (
        Path(configured).expanduser() if configured else Path.home() / ".config/llm-bus/config.toml"
    )
    if not path.exists() and configured is None:
        return None
    try:
        data = cast("dict[str, object]", tomllib.loads(path.read_text(encoding="utf-8")))
    except tomllib.TOMLDecodeError as error:
        raise BusError("Invalid remote configuration TOML") from error
    remote = _mapping(data.get("remote"))
    url = _text(remote.get("url")).rstrip("/")
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
    credential = Path(_text(remote.get("credential_file"))).expanduser()
    if not credential.is_absolute():
        credential = path.parent / credential
    repositories = tuple(
        _repository(item, path.parent) for item in _array(data.get("repositories"))
    )
    if len({repo.scope for repo in repositories}) != len(repositories) or len(
        {repo.repo_id for repo in repositories}
    ) != len(repositories):
        raise BusError("Duplicate remote repository mappings; map linked worktrees once")
    return Config(url, _identifier(remote.get("machine_id")), credential, repositories)


def require_config() -> Config:
    config = load_config()
    if config is None:
        raise BusError("Remote command requires a remote configuration file")
    return config


def read_token(path: Path) -> str:
    if path.is_symlink():
        raise BusError("Credential file must not be a symlink")
    # Platform flag names are fixed OS APIs, not configurable policy.
    # pylint: disable-next=clean-code-business-policy-literal
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise BusError("Credential must be a regular file")
        if os.name == "posix" and (metadata.st_mode & 0o077 or metadata.st_uid != os.getuid()):
            raise BusError("Credential file must be owned by this user with mode 600")
        token = stream.read(129).strip()
    try:
        result = token.decode("ascii")
    except UnicodeDecodeError as error:
        raise BusError("Invalid machine credential format") from error
    if not TOKEN_PATTERN.fullmatch(result):
        raise BusError("Invalid machine credential format")
    return result


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
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with cast("HTTPResponse", opener.open(request, timeout=10)) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise BusError(f"Relay rejected request (HTTP {error.code})") from None  # pylint: disable=clean-code-business-policy-literal
        except (OSError, urllib.error.URLError) as error:
            raise BusError("Relay unavailable; local transport state retained") from error
        if len(payload) > MAX_RESPONSE_BYTES:
            raise BusError("Relay response exceeds size limit")
        try:
            return cast("object", json.loads(payload))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BusError("Invalid relay response") from error


def parse_peer(value: object, repo_id: str, machine_id: str) -> Peer:
    data = _mapping(value)
    machine = _identifier(data.get("machine_id"))
    native = _text(data.get("native_address"))
    peer_address = _text(data.get("address"))
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
        last_seen=_text(data.get("last_seen")),
        ended=0,
    )


def remote_peers(store: Store, config: Config, project: str | None) -> list[Peer]:
    selected = config.repository(project) if project is not None else None
    allowed = {
        repo.repo_id
        for repo in config.repositories
        if repo.send and (project is None or repo == selected)
    }
    peers = RemoteStore(store, config.connection).peers()
    return [peer for peer in peers if peer["repo_id"] in allowed and not peer["ended"]]


def enqueue_remote(
    store: Store, config: Config, sender: SenderContext, recipient: str, body: str
) -> dict[str, object]:
    repo = config.repository(sender.project)
    if repo is None or not repo.send:
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
    return RemoteStore(store, config.connection).enqueue(sender, recipient, repo.repo_id, body)


def parse_delivery(value: object, repo: Repository, machine: str) -> Delivery:
    data = _mapping(value)
    recipient = _text(data.get("recipient"))
    sender = _text(data.get("sender"))
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
    body = _text(data.get("body"))
    if len(body.encode()) > MAX_MESSAGE_BYTES:
        raise BusError("Remote message exceeds 64 KiB")
    return Delivery(
        id=_text(data.get("id")),
        sender=sender,
        recipient=recipient,
        repo_id=repo.repo_id,
        body=body,
        sequence=sequence,
        sent_at=_text(data.get("sent_at")),
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

    def sync(self) -> dict[str, int]:
        counts = {"uploaded": 0, "imported": 0, "acknowledged": 0, "errors": 0}
        counts["errors"] += self._publish_sessions()
        for repo in self.config.repositories:
            try:
                if repo.send:
                    self._cache_peers(repo)
            except BusError:
                counts["errors"] += 1
        counts["uploaded"], failures = self._sync_outgoing()
        counts["errors"] += failures
        for repo in self.config.repositories:
            try:
                if repo.receive:
                    counts["imported"] += self._receive(repo)
            except BusError:
                counts["errors"] += 1
        counts["acknowledged"], failures = self._sync_acknowledgements()
        counts["errors"] += failures
        return counts

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
        for remote_id, repo_id in self.remote.acknowledgements():
            if any(repo.repo_id == repo_id and repo.receive for repo in self.config.repositories):
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
            for item in _array(
                self.relay.request(
                    HTTP_GET, "/v1/sessions?" + urllib.parse.urlencode({"repo_id": repo.repo_id})
                )
            )
        ]
        self.remote.cache(repo.repo_id, peers)

    def _sync_outgoing(self) -> tuple[int, int]:
        uploaded = failures = 0
        for envelope, state in self.remote.outgoing():
            repo = next(
                (repo for repo in self.config.repositories if repo.repo_id == envelope["repo_id"]),
                None,
            )
            if repo is not None and repo.send:
                try:
                    self._upload(envelope, state)
                    uploaded += 1
                except BusError:
                    failures += 1
        return uploaded, failures

    def _upload(self, envelope: Envelope, state: str) -> None:
        if state == "queued_local":
            self._publish_original_sender(envelope)
            result = self.relay.request(HTTP_POST, "/v1/messages", envelope)
        else:
            result = self.relay.request(
                HTTP_GET, "/v1/status?" + urllib.parse.urlencode({"id": envelope["id"]})
            )
        status = _mapping(result).get("status")
        if status not in {"accepted_remote", "delivered_local", "acknowledged", "expired"}:
            raise BusError("Invalid remote message status")
        self.remote.set_state(envelope["id"], cast("str", status))

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
            values = _array(
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
                local_id = self.remote.import_message(item, repo.scope)
                if local_id is not None:
                    if self.remote.claim_wake(item["id"]):
                        self._wake(item, local_id)
                        imported += 1
                    self.relay.request(HTTP_POST, "/v1/receipts", {"id": item["id"]})

    def _wake(self, item: Delivery, local_id: int) -> None:
        native = item["recipient"].split("/", 2)[2]
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

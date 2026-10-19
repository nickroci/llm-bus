"""Owner-controlled collaboration identity, invites, peers, and message state."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Generator

HOME_ENV = "LLM_BUS_COLLAB_HOME"
MODES = ("monitor", "isolated", "readonly", "readwrite", "full")
RESPONDER_MODES = frozenset({"isolated", "readonly", "readwrite"})
DEFAULT_MODE = "monitor"
FULL_MODE = "full"
INVITE_MINUTES = 60
FULL_MAX_MINUTES = 24 * 60
MAX_BODY_BYTES = 64 * 1024
MAX_INVITE_CHARS = 4096
MAX_REPOSITORY_NAME = 200
AUTO_REPLIES_PER_HOUR = 20
INVITE_PREFIX = "llmbus-invite-v0:"
NOTICE_SENDER = "collab:_daemon"
FORWARD_SENDER = "collab:_owner"
REPLY_SENDER_PREFIX = "collab:reply:"
SENDER_PREFIX = "collab:"
WINDOWS_DATA_ENV = "LOCALAPPDATA"
JOIN_AWAITING = "awaiting_owner"
JOIN_APPROVED = "approved"
JOIN_CONNECTED = "connected"
JOIN_FAILED = "failed"
MESSAGE_FAILED = "failed"
MESSAGE_CANCELLED = "cancelled"
MESSAGE_SENT = "sent"
MESSAGE_REJECTED = "rejected"
GITHUB_USER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
SESSION = re.compile(r"^(?:claude|codex):[A-Za-z0-9_-]{1,128}$")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
SCHEMA = """
    CREATE TABLE IF NOT EXISTS identity (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        github_user TEXT NOT NULL,
        signing_key TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS invites (
        nonce TEXT PRIMARY KEY,
        peer_user TEXT NOT NULL,
        mode TEXT NOT NULL,
        full_until TEXT,
        review_replies INTEGER NOT NULL,
        agent_replies INTEGER NOT NULL DEFAULT 0,
        wake_on_answer INTEGER NOT NULL DEFAULT 0,
        repository TEXT NOT NULL,
        session TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT
    );
    CREATE TABLE IF NOT EXISTS joins (
        id INTEGER PRIMARY KEY,
        invite TEXT NOT NULL,
        repository TEXT NOT NULL,
        session TEXT,
        mode TEXT,
        full_until TEXT,
        review_replies INTEGER NOT NULL DEFAULT 1,
        agent_replies INTEGER NOT NULL DEFAULT 0,
        wake_on_answer INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS peers (
        id INTEGER PRIMARY KEY,
        pair TEXT NOT NULL UNIQUE,
        github_user TEXT NOT NULL,
        repository TEXT NOT NULL,
        endpoint_id TEXT NOT NULL,
        ticket TEXT,
        key_fingerprint TEXT NOT NULL,
        mode TEXT NOT NULL,
        full_until TEXT,
        review_replies INTEGER NOT NULL,
        agent_replies INTEGER NOT NULL DEFAULT 0,
        wake_on_answer INTEGER NOT NULL DEFAULT 0,
        their_grant TEXT,
        session TEXT,
        confirm_code TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        peer_id INTEGER NOT NULL REFERENCES peers(id),
        direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
        kind TEXT NOT NULL CHECK (kind IN ('request', 'reply')),
        uid TEXT NOT NULL,
        in_reply_to TEXT,
        body TEXT NOT NULL,
        state TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (peer_id, direction, uid)
    );
"""
# Columns added after the first release; existing databases gain them on open.
ADDED_COLUMNS = [
    (table, f"{name} INTEGER NOT NULL DEFAULT 0")
    for table in ("invites", "joins", "peers")
    for name in ("agent_replies", "wake_on_answer")
]


class CollabError(ValueError):
    """Invalid collaboration input or state."""


class Invite(TypedDict):
    v: int
    ticket: str
    nonce: str
    host_user: str
    joiner_user: str
    repository_name: str
    grant: str
    expires_at: str


class Peer(TypedDict):
    id: int
    pair: str
    github_user: str
    repository: str
    endpoint_id: str
    ticket: str | None
    mode: str
    full_until: str | None
    review_replies: bool
    agent_replies: bool
    wake_on_answer: bool
    their_grant: str | None
    session: str | None
    confirm_code: str
    created_at: str
    revoked_at: str | None


class Message(TypedDict):
    id: int
    peer_id: int
    direction: str
    kind: str
    uid: str
    in_reply_to: str | None
    body: str
    state: str
    detail: str | None
    created_at: str


class Grant(NamedTuple):
    """One owner's choice of what a peer's messages may cause locally."""

    mode: str
    full_until: str | None
    review_replies: bool
    agent_replies: bool = False
    wake_on_answer: bool = False


PEER_COLUMNS = (
    "id, pair, github_user, repository, endpoint_id, ticket, mode, full_until, review_replies, "
    "their_grant, session, confirm_code, created_at, revoked_at, agent_replies, wake_on_answer"
)
MESSAGE_COLUMNS = "id, peer_id, direction, kind, uid, in_reply_to, body, state, detail, created_at"
type PeerRow = tuple[
    int,
    str,
    str,
    str,
    str,
    str | None,
    str,
    str | None,
    int,
    str | None,
    str | None,
    str,
    str,
    str | None,
    int,
    int,
]
type InviteRow = tuple[str, str, str | None, int, str, str | None, str, str | None, int, int]
type MessageRow = tuple[int, int, str, str, str, str | None, str, str, str | None, str]


def now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return moment.strftime(TIME_FORMAT)


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC)


def default_home() -> Path:
    """Keep collaboration identity outside repositories; one directory per identity."""
    configured = os.environ.get(HOME_ENV)
    if configured:
        return Path(configured).expanduser()
    if sys.platform != "win32":
        return Path.home() / ".config" / "llm-bus" / "collab"
    local_data = os.environ.get(WINDOWS_DATA_ENV) or str(Path.home() / "AppData" / "Local")
    return Path(local_data) / "llm-bus" / "collab"


def github_user(value: object) -> str:
    if not isinstance(value, str) or not GITHUB_USER.fullmatch(value) or "--" in value:
        raise CollabError("Invalid GitHub username")
    return value


def grant(
    mode: object,
    full_minutes: object = None,
    *,
    review_replies: bool = True,
    agent_replies: bool = False,
    wake_on_answer: bool = False,
) -> Grant:
    """Validate a human's mode choice; full access always expires."""
    if mode not in MODES:
        raise CollabError(f"Mode must be one of {', '.join(MODES)}")
    full_until = None
    if mode == FULL_MODE:
        if not isinstance(full_minutes, int) or not 1 <= full_minutes <= FULL_MAX_MINUTES:
            raise CollabError(f"Full mode needs an expiry between 1 and {FULL_MAX_MINUTES} minutes")
        full_until = stamp(now() + timedelta(minutes=full_minutes))
    return Grant(cast("str", mode), full_until, review_replies, agent_replies, wake_on_answer)


def effective_mode(peer: Peer) -> str | None:
    """Current local mode, or None when revoked; expired full access falls back to monitor."""
    if peer["revoked_at"] is not None:
        return None
    if peer["mode"] == FULL_MODE and (
        peer["full_until"] is None or parse_stamp(peer["full_until"]) <= now()
    ):
        return DEFAULT_MODE
    return peer["mode"]


def pair_id(nonce: str) -> str:
    return hashlib.sha256(f"llm-bus-collab-pair|{nonce}".encode()).hexdigest()[:32]


def encode_invite(invite: Invite) -> str:
    raw = json.dumps(invite, sort_keys=True, separators=(",", ":")).encode()
    return INVITE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _invite_fields(text: str) -> dict[str, object]:
    text = text.strip()
    if len(text) > MAX_INVITE_CHARS or not text.startswith(INVITE_PREFIX):
        raise CollabError("Not an llm-bus invite")
    payload = text.removeprefix(INVITE_PREFIX)
    try:
        data = cast(
            "object", json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        )
    except (ValueError, binascii.Error) as exc:
        raise CollabError("Invite is malformed") from exc
    if not isinstance(data, dict):
        raise CollabError("Invite is malformed")
    fields = cast("dict[str, object]", data)
    expected = set(Invite.__annotations__)
    if set(fields) != expected or fields.get("v") != 0:
        raise CollabError("Invite has an unsupported format")
    if not all(isinstance(fields[key], str) for key in expected - {"v"}):
        raise CollabError("Invite is malformed")
    return fields


def decode_invite(text: str) -> Invite:
    """Parse an invite as untrusted data; nothing in it grants local permission."""
    invite = cast("Invite", cast("object", _invite_fields(text)))
    github_user(invite["host_user"])
    github_user(invite["joiner_user"])
    if (
        not TOKEN.fullmatch(invite["nonce"])
        or invite["grant"] not in MODES
        or len(invite["repository_name"]) > MAX_REPOSITORY_NAME
    ):
        raise CollabError("Invite is malformed")
    try:
        expires = parse_stamp(invite["expires_at"])
    except ValueError as exc:
        raise CollabError("Invite is malformed") from exc
    if expires <= now():
        raise CollabError("Invite has expired")
    return invite


def _peer(row: PeerRow) -> Peer:
    return Peer(
        id=row[0],
        pair=row[1],
        github_user=row[2],
        repository=row[3],
        endpoint_id=row[4],
        ticket=row[5],
        mode=row[6],
        full_until=row[7],
        review_replies=bool(row[8]),
        their_grant=row[9],
        session=row[10],
        confirm_code=row[11],
        created_at=row[12],
        revoked_at=row[13],
        agent_replies=bool(row[14]),
        wake_on_answer=bool(row[15]),
    )


def _message(row: MessageRow) -> Message:
    return Message(
        id=row[0],
        peer_id=row[1],
        direction=row[2],
        kind=row[3],
        uid=row[4],
        in_reply_to=row[5],
        body=row[6],
        state=row[7],
        detail=row[8],
        created_at=row[9],
    )


class ApprovedJoin(NamedTuple):
    """A join the owner approved; the daemon performs its handshake."""

    id: int
    invite: str
    repository: str
    session: str | None
    choice: Grant


class NewMessage(NamedTuple):
    """Fields for one stored collaboration message."""

    direction: str
    kind: str
    uid: str
    body: str
    state: str
    in_reply_to: str | None = None
    detail: str | None = None


class NewPeer(NamedTuple):
    """Verified peer facts recorded after a successful handshake."""

    pair: str
    github_user: str
    repository: str
    endpoint_id: str
    ticket: str | None
    key_fingerprint: str
    their_grant: str
    session: str | None
    confirm_code: str


class CollabState:  # pylint: disable=too-many-public-methods
    """SQLite state for one collaboration identity; only the owner edits grants."""

    home: Path
    path: Path

    def __init__(self, home: Path | None = None) -> None:
        self.home = home or default_home()
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.home / "collab.sqlite3"
        with closing(self._connect()) as db:
            db.executescript(SCHEMA)
            for table, column in ADDED_COLUMNS:
                info = cast(
                    "list[tuple[int, str]]", db.execute(f"PRAGMA table_info({table})").fetchall()
                )
                names = {row[1] for row in info}
                if column.split()[0] not in names:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            db.commit()
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            db.commit()

    def identity(self) -> tuple[str, str] | None:
        with closing(self._connect()) as db:
            return cast(
                "tuple[str, str] | None",
                db.execute("SELECT github_user, signing_key FROM identity").fetchone(),
            )

    def set_identity(self, user: str, signing_key: Path) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO identity(id, github_user, signing_key) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET github_user = excluded.github_user, "
                "signing_key = excluded.signing_key",
                (github_user(user), str(signing_key)),
            )

    def create_invite(
        self, peer_user: str, choice: Grant, repository: str, session: str | None
    ) -> tuple[str, str]:
        """Record a one-time invite bound to one GitHub user; return (nonce, expires_at)."""
        nonce = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
        created = now()
        expires = stamp(created + timedelta(minutes=INVITE_MINUTES))
        with self.transaction() as db:
            db.execute(
                "INSERT INTO invites(nonce, peer_user, mode, full_until, review_replies, "
                "agent_replies, wake_on_answer, repository, session, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    nonce,
                    github_user(peer_user),
                    choice.mode,
                    choice.full_until,
                    int(choice.review_replies),
                    int(choice.agent_replies),
                    int(choice.wake_on_answer),
                    repository,
                    session,
                    stamp(created),
                    expires,
                ),
            )
        return nonce, expires

    def claim_invite(self, nonce: str, peer_user: str) -> tuple[Grant, str, str | None]:
        """Consume an unexpired invite for exactly its GitHub user; single use."""
        with self.transaction() as db:
            row = cast(
                "InviteRow | None",
                db.execute(
                    "SELECT peer_user, mode, full_until, review_replies, repository, session, "
                    "expires_at, used_at, agent_replies, wake_on_answer FROM invites "
                    "WHERE nonce = ?",
                    (nonce,),
                ).fetchone(),
            )
            if row is None:
                raise CollabError("Invite is unknown, already used, or for another user")
            invited, mode, full_until, review, repository, session, expires_at, used_at = row[:8]
            if used_at is not None or invited != peer_user:
                raise CollabError("Invite is unknown, already used, or for another user")
            if parse_stamp(expires_at) <= now():
                raise CollabError("Invite has expired")
            db.execute("UPDATE invites SET used_at = ? WHERE nonce = ?", (stamp(now()), nonce))
        choice = Grant(mode, full_until, bool(review), bool(row[8]), bool(row[9]))
        return choice, repository, session

    def invite_is_open(self, nonce: str, peer_user: str) -> bool:
        with closing(self._connect()) as db:
            row = cast(
                "tuple[str, str] | None",
                db.execute(
                    "SELECT peer_user, expires_at FROM invites WHERE nonce = ? AND used_at IS NULL",
                    (nonce,),
                ).fetchone(),
            )
        return row is not None and row[0] == peer_user and parse_stamp(row[1]) > now()

    def add_join(self, invite: str, repository: str, session: str | None) -> int:
        decode_invite(invite)
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT INTO joins(invite, repository, session, status, created_at) "
                "VALUES (?, ?, ?, 'awaiting_owner', ?)",
                (invite.strip(), repository, session, stamp(now())),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a join ID")
        return cursor.lastrowid

    def join(self, join_id: int) -> tuple[str, str, str | None, str, str | None]:
        """Return (invite, repository, session, status, detail)."""
        with closing(self._connect()) as db:
            row = cast(
                "tuple[str, str, str | None, str, str | None] | None",
                db.execute(
                    "SELECT invite, repository, session, status, detail FROM joins WHERE id = ?",
                    (join_id,),
                ).fetchone(),
            )
        if row is None:
            raise CollabError("Unknown join request")
        return row

    def approve_join(self, join_id: int, choice: Grant) -> None:
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE joins SET status = 'approved', mode = ?, full_until = ?, "
                "review_replies = ?, agent_replies = ?, wake_on_answer = ? "
                "WHERE id = ? AND status = 'awaiting_owner'",
                (
                    choice.mode,
                    choice.full_until,
                    int(choice.review_replies),
                    int(choice.agent_replies),
                    int(choice.wake_on_answer),
                    join_id,
                ),
            )
            if cursor.rowcount != 1:
                raise CollabError("Join request is not awaiting approval")

    def approved_joins(self) -> list[ApprovedJoin]:
        with closing(self._connect()) as db:
            rows = cast(
                "list[tuple[int, str, str, str | None, str, str | None, int, int, int]]",
                db.execute(
                    "SELECT id, invite, repository, session, mode, full_until, review_replies, "
                    "agent_replies, wake_on_answer FROM joins WHERE status = 'approved' "
                    "ORDER BY id"
                ).fetchall(),
            )
        return [
            ApprovedJoin(
                r[0], r[1], r[2], r[3], Grant(r[4], r[5], bool(r[6]), bool(r[7]), bool(r[8]))
            )
            for r in rows
        ]

    def finish_join(self, join_id: int, status: str, detail: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE joins SET status = ?, detail = ? WHERE id = ?", (status, detail, join_id)
            )

    def add_peer(self, facts: NewPeer, choice: Grant) -> Peer:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO peers(pair, github_user, repository, endpoint_id, ticket, "
                "key_fingerprint, mode, full_until, review_replies, agent_replies, wake_on_answer, "
                "their_grant, session, confirm_code, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    facts.pair,
                    github_user(facts.github_user),
                    facts.repository,
                    facts.endpoint_id,
                    facts.ticket,
                    facts.key_fingerprint,
                    choice.mode,
                    choice.full_until,
                    int(choice.review_replies),
                    int(choice.agent_replies),
                    int(choice.wake_on_answer),
                    facts.their_grant,
                    facts.session,
                    facts.confirm_code,
                    stamp(now()),
                ),
            )
        peer = self.peer_by_pair(facts.pair)
        if peer is None:
            raise RuntimeError("Inserted peer could not be read")
        return peer

    def peers(self) -> list[Peer]:
        with closing(self._connect()) as db:
            rows = cast(
                "list[PeerRow]",
                db.execute(
                    f"SELECT {PEER_COLUMNS} FROM peers ORDER BY id"  # noqa: S608 - fixed columns
                ).fetchall(),
            )
        return [_peer(row) for row in rows]

    def peer(self, peer_id: int) -> Peer | None:
        return next((p for p in self.peers() if p["id"] == peer_id), None)

    def peer_by_pair(self, pair: str) -> Peer | None:
        return next((p for p in self.peers() if p["pair"] == pair), None)

    def active_peer(self, user: str, repository: str) -> Peer | None:
        """Latest unrevoked pairing with a GitHub user in this repository."""
        matches = [
            p
            for p in self.peers()
            if p["github_user"].lower() == user.lower()
            and p["repository"] == repository
            and p["revoked_at"] is None
        ]
        return matches[-1] if matches else None

    def set_mode(self, peer_id: int, choice: Grant) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE peers SET mode = ?, full_until = ?, review_replies = ?, agent_replies = ?, "
                "wake_on_answer = ? WHERE id = ?",
                (
                    choice.mode,
                    choice.full_until,
                    int(choice.review_replies),
                    int(choice.agent_replies),
                    int(choice.wake_on_answer),
                    peer_id,
                ),
            )

    def set_session(self, peer_id: int, session: str) -> None:
        if not SESSION.fullmatch(session):
            raise CollabError("Invalid session ID")
        with self.transaction() as db:
            db.execute("UPDATE peers SET session = ? WHERE id = ?", (session, peer_id))

    def set_ticket(self, peer_id: int, ticket: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE peers SET ticket = ? WHERE id = ?", (ticket, peer_id))

    def revoke(self, peer_id: int) -> None:
        """Stop future delivery; stored history is retained, exposed content is not recalled."""
        with self.transaction() as db:
            db.execute(
                "UPDATE peers SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (stamp(now()), peer_id),
            )
            db.execute(
                "UPDATE messages SET state = 'cancelled' WHERE peer_id = ? AND direction = 'out' "
                "AND state IN ('queued', 'held')",
                (peer_id,),
            )

    def add_message(self, peer_id: int, new: NewMessage) -> Message | None:
        """Insert a message; return None for a duplicate UID from the same peer."""
        if not new.body.strip():
            raise CollabError("Message body cannot be empty")
        if len(new.body.encode("utf-8")) > MAX_BODY_BYTES:
            raise CollabError("Message body exceeds 64 KiB")
        try:
            with self.transaction() as db:
                cursor = db.execute(
                    "INSERT INTO messages(peer_id, direction, kind, uid, in_reply_to, body, state, "
                    "detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        peer_id,
                        new.direction,
                        new.kind,
                        new.uid,
                        new.in_reply_to,
                        new.body,
                        new.state,
                        new.detail,
                        stamp(now()),
                    ),
                )
        except sqlite3.IntegrityError:
            return None
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a message ID")
        return self.message(cursor.lastrowid)

    def message(self, message_id: int) -> Message:
        with closing(self._connect()) as db:
            row = cast(
                "MessageRow | None",
                db.execute(
                    f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE id = ?",  # noqa: S608
                    (message_id,),
                ).fetchone(),
            )
        if row is None:
            raise CollabError(f"Unknown collaboration message {message_id}")
        return _message(row)

    def messages(self, *, state: str | None = None, limit: int = 200) -> list[Message]:
        query = f"SELECT {MESSAGE_COLUMNS} FROM messages"  # noqa: S608
        parameters: tuple[object, ...] = ()
        if state is not None:
            query += " WHERE state = ?"
            parameters = (state,)
        with closing(self._connect()) as db:
            rows = cast(
                "list[MessageRow]",
                db.execute(f"{query} ORDER BY id DESC LIMIT ?", (*parameters, limit)).fetchall(),
            )
        return [_message(row) for row in rows]

    def set_state(self, message_id: int, state: str, detail: str | None = None) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE messages SET state = ?, detail = ? WHERE id = ?",
                (state, detail, message_id),
            )

    def transition(self, message_id: int, expected: str, state: str) -> bool:
        """Change state only from an expected state, so one human action wins."""
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE messages SET state = ? WHERE id = ? AND state = ?",
                (state, message_id, expected),
            )
        return cursor.rowcount == 1

    def first_answer(self, peer_id: int, message: Message) -> bool:
        """True when an incoming reply is the first one to a question this side sent."""
        with closing(self._connect()) as db:
            asked = cast(
                "tuple[int] | None",
                db.execute(
                    "SELECT 1 FROM messages WHERE peer_id = ? AND direction = 'out' "
                    "AND kind = 'request' AND uid = ?",
                    (peer_id, message["in_reply_to"]),
                ).fetchone(),
            )
            earlier = cast(
                "tuple[int] | None",
                db.execute(
                    "SELECT 1 FROM messages WHERE peer_id = ? AND direction = 'in' "
                    "AND kind = 'reply' AND in_reply_to = ? AND id < ?",
                    (peer_id, message["in_reply_to"], message["id"]),
                ).fetchone(),
            )
        return asked is not None and earlier is None

    def recent_auto_replies(self, peer_id: int) -> int:
        since = stamp(now() - timedelta(hours=1))
        with closing(self._connect()) as db:
            row = cast(
                "tuple[int]",
                db.execute(
                    "SELECT COUNT(*) FROM messages WHERE peer_id = ? AND direction = 'in' "
                    "AND kind = 'request' AND state IN ('responding', 'answered', 'held_reply') "
                    "AND created_at >= ?",
                    (peer_id, since),
                ).fetchone(),
            )
        return row[0]


def exposure_allowed(sender: str, recipient: str, home: Path | None = None) -> bool:
    """Recheck owner policy before a collaboration message is shown to a local agent.

    Daemon notices carry no peer text, and owner forwards are the owner's explicit choice.
    Answers to this side's own questions reach the asking session while the owner allows agent
    replies for that peer. Other peer text reaches an agent inbox only while that peer is in
    unexpired full mode for the same local session.
    """
    if sender in {NOTICE_SENDER, FORWARD_SENDER}:
        return True
    answer = sender.startswith(REPLY_SENDER_PREFIX)
    user = sender.removeprefix(REPLY_SENDER_PREFIX if answer else SENDER_PREFIX)
    state_home = home or default_home()
    if not (state_home / "collab.sqlite3").exists():
        return False
    try:
        peers = CollabState(state_home).peers()
    except (sqlite3.Error, OSError):
        return False
    return any(
        p["github_user"] == user
        and p["session"] == recipient
        and (
            (p["agent_replies"] and effective_mode(p) is not None)
            if answer
            else effective_mode(p) == FULL_MODE
        )
        for p in peers
    )


def daemon_file(state: CollabState) -> Path:
    return state.home / "daemon.json"


def running_daemon(state: CollabState) -> dict[str, object] | None:
    """Return daemon details when its recorded process is still alive."""
    path = daemon_file(state)
    try:
        data = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
        pid = data["pid"]
        if not isinstance(pid, int):
            return None
        os.kill(pid, 0)
    except (OSError, ValueError, KeyError):
        return None
    return data

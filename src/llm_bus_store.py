"""SQLite mailbox shared by local agent sessions."""

# pylint: disable=too-many-lines
# Mailbox writes and retained read-only diagnostics share the storage contract.

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, TypedDict, cast

import llm_bus_policy
from llm_bus_schema import ensure_schema

if TYPE_CHECKING:
    from collections.abc import Generator

    from llm_bus_policy import MessageIdentity

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_MESSAGE_BYTES = 64 * 1024
MAX_BATCH = 100
PENDING_PAGE_ERROR = "Pending page size must be between 1 and 100"
PENDING_COUNT_ERROR = "Pending count limit must be positive"
SESSION_IDLE_HOURS = 24
DB_ENV = "LLM_BUS_DB"
SQLITE_TIMEOUT_SECONDS = 10
SQLITE_BUSY_RETRY_SECONDS = 0.01
WINDOWS_DATA_ENV = "LOCALAPPDATA"
GIT_ENV_PREFIX = "GIT_"  # pylint: disable=clean-code-business-policy-literal
SYNC_READ_SECONDS = 30
SYNC_KINDS = frozenset({"upload", "status", "report_read", "report_submit", "receipt", "ack"})
SYNC_ERRORS = frozenset(
    {"unavailable", "not_allowed", "protocol_error", "rejected", "unsupported_reply_link"}  # pylint: disable=clean-code-business-policy-literal
)
SYNC_COLUMNS = (
    "attempts",
    "next_attempt_at",
    "last_attempt_at",
    "last_success_at",
    "error_code",
    "first_accepted_at",
    "polling_suspended_at",
)
WAKE_STATUSES = frozenset({"requested", "coalesced", "failed", "unknown", "unsupported"})  # pylint: disable=clean-code-business-policy-literal
WAKE_ADAPTERS = {
    "codex queue": "codex_queue",
    "Claude inbox socket": "claude_inbox_socket",
    "host wake": "host_wake",
    "wake coordinator": "wake_coordinator",
    "none": "none",
}
SYNC_SNAPSHOT_QUERY = (
    "SELECT attempts,next_attempt_at,last_attempt_at,last_success_at,error_code,"
    "first_accepted_at,polling_suspended_at FROM remote_sync_work WHERE kind=? AND key=?"
)
READ_TRANSACTION = "BEGIN"
SQLITE_MAX_MESSAGE_ID = 2**63 - 1


def safe_wake_reason(status: str | None, via: str | None, reason: str | None) -> str | None:
    """Diagnostics contain fixed categories, never adapter stderr or exception text."""
    if reason is None:
        return None
    outcome = status if status in WAKE_STATUSES else "unknown"
    return f"wake_{outcome}:{WAKE_ADAPTERS.get(via or '', 'unknown_adapter')}"


def synchronization_snapshot(db: sqlite3.Connection, kind: str, key: str) -> dict[str, object]:
    """Project retained scheduling state without creating it or changing due work."""
    if kind not in SYNC_KINDS:
        raise BusError("Invalid synchronization kind")
    exists = cast(
        "tuple[int] | None",
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='remote_sync_work'"
        ).fetchone(),
    )
    row = cast(
        "tuple[object, ...] | None",
        db.execute(
            SYNC_SNAPSHOT_QUERY,
            (kind, key),
        ).fetchone()
        if exists
        else None,
    )
    result: dict[str, object] = (
        dict(zip(SYNC_COLUMNS, row, strict=True))
        if row
        else dict.fromkeys(SYNC_COLUMNS, cast("object", None))
    )
    error = result["error_code"]
    if error is not None and (not isinstance(error, str) or error not in SYNC_ERRORS):
        result["error_code"] = "protocol_error"
    success = cast("str | None", result["last_success_at"])
    try:
        result["stale"] = (
            success is None
            or result["error_code"] is not None
            or datetime.now(UTC)
            >= datetime.fromisoformat(success) + timedelta(seconds=SYNC_READ_SECONDS)
        )
    except (ValueError, TypeError):
        result["stale"] = True
    return result


class BusError(ValueError):
    """Invalid operation at the local bus boundary."""


class Agent(TypedDict):
    """Registered session address."""

    address: str
    kind: str
    project: str
    last_seen: str
    ended_at: str | None


class Message(TypedDict):
    """Unacknowledged mailbox item."""

    id: int
    sender: str
    recipient: str
    body: str
    sender_cwd: str | None
    recipient_cwd: str | None
    sent_at: str
    reply_to: int | None


class HistoryMessage(Message):
    """Stored message with delivery state for read-only history views."""

    acknowledged_at: str | None
    wake_status: str | None
    wake_via: str | None
    wake_reason: str | None
    wake_checked_at: str | None


class SenderContext(NamedTuple):
    """Invocation identity and folder used together for an atomic send."""

    kind: str
    session_id: str
    project: str


class PendingRow(NamedTuple):
    """One bounded scan row with authenticated remote metadata when available."""

    id: int
    sender: str
    recipient: str
    body: str | None
    sender_cwd: str | None
    recipient_cwd: str | None
    sent_at: str | None
    stored_scope: str | None
    envelope: str | None
    connection: str | None
    reply_to: int | None


type PendingTuple = tuple[
    int,
    str,
    str,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    int | None,
]
type PendingContent = Literal[
    "NULL,NULL,NULL,NULL", "m.body,m.sender_cwd,m.recipient_cwd,m.sent_at"
]


type HistoryRow = tuple[
    int,
    str,
    str,
    str,
    str | None,
    str | None,
    str,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    int | None,
]


def expire_idle_agents(db: sqlite3.Connection) -> None:
    """Close sessions idle for a day so their messages stop waking and their locks lapse."""
    db.execute(
        "UPDATE agents SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE ended_at IS NULL AND last_seen <= "
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
        (f"-{SESSION_IDLE_HOURS} hours",),
    )


def default_path() -> Path:
    """Place user-local bus state outside repositories."""
    configured = os.environ.get(DB_ENV)
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        local_data = os.environ.get(WINDOWS_DATA_ENV)
        base = Path(local_data) if local_data else Path.home() / "AppData" / "Local"
        return base / "llm-bus" / "bus.sqlite3"
    return Path.home() / ".local" / "share" / "llm-bus" / "bus.sqlite3"


def address(kind: str, session_id: str) -> str:
    """Build stable address from host session ID."""
    if kind not in {"claude", "codex"}:
        raise BusError("Agent kind must be claude or codex")
    if not SESSION_ID.fullmatch(session_id):
        raise BusError("Host session ID has invalid characters or length")
    return f"{kind}:{session_id}"


def routing_scope(project: str) -> str:
    """Use one identity for linked Git worktrees, otherwise the folder path."""
    folder = Path(project).resolve()
    git = shutil.which("git")
    if git is None:
        return str(folder)
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(GIT_ENV_PREFIX)
    }
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git, "-C", str(folder), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return str(folder)
    common_dir = result.stdout.strip()
    if result.returncode or not common_dir:
        return str(folder)
    return f"git:{(folder / common_dir).resolve()}"


class Store:
    """Durable messages; inbox reads leave items pending until acknowledged."""

    path: Path

    @classmethod
    def read_only(cls, path: Path) -> Store:
        """Open retained diagnostics without mailbox creation, migration, or chmod."""
        store = cls.__new__(cls)
        store.path = path
        return store

    def message_status(self, message_id: int) -> dict[str, object]:
        """Return body-free local diagnostics, independent of live policy and transport."""
        if isinstance(message_id, bool) or message_id <= 0:
            raise BusError("Message ID must be positive")
        if message_id > SQLITE_MAX_MESSAGE_ID:
            raise BusError("Unknown message ID")
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute(READ_TRANSACTION)
            columns = {
                row[1]
                for row in cast(
                    "list[tuple[object, str]]", db.execute("PRAGMA table_info(messages)").fetchall()
                )
            }
            names = (
                "id",
                "sender",
                "recipient",
                "reply_to",
                "sent_at",
                "acknowledged_at",
                "wake_status",
                "wake_via",
                "wake_reason",
                "wake_checked_at",
            )
            fields = ",".join(name if name in columns else f"NULL AS {name}" for name in names)
            # Only constant projection names enter SQL; the message ID remains a parameter.
            query = f"SELECT {fields} FROM messages WHERE id=?"  # noqa: S608
            row = cast(
                "tuple[object, ...] | None",
                db.execute(query, (message_id,)).fetchone(),
            )
            if row is None:
                raise BusError("Unknown message ID")
            message: dict[str, object] = dict(zip(names, row, strict=True))
            return self._message_status(db, message)

    def _message_status(
        self, db: sqlite3.Connection, message: dict[str, object]
    ) -> dict[str, object]:
        tables = {
            row[0]
            for row in cast(
                "list[tuple[str]]",
                db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
            )
        }
        key = message["id"]
        outgoing = (
            cast(
                "tuple[str, str] | None",
                db.execute(
                    "SELECT id,state FROM remote_outbox WHERE local_id=?", (key,)
                ).fetchone(),
            )
            if "remote_outbox" in tables
            else None
        )
        incoming = (
            cast(
                "tuple[str] | None",
                db.execute("SELECT id FROM remote_imports WHERE local_id=?", (key,)).fetchone(),
            )
            if "remote_imports" in tables
            else None
        )
        remote_id = outgoing[0] if outgoing else incoming[0] if incoming else None
        sync = synchronization_snapshot(db, "status" if outgoing else "receipt", remote_id or "")
        synchronization = {
            name: sync[name]
            for name in (
                "last_attempt_at",
                "last_success_at",
                "next_attempt_at",
                "error_code",
                "polling_suspended_at",
                "stale",
            )
        }
        if remote_id and "remote_sync_work" in tables:
            scheduled = cast(
                "list[tuple[str, str, int | None]]",
                db.execute(
                    "SELECT kind,key,NULL FROM remote_sync_work "
                    "WHERE key=? AND kind<>'report_submit' ORDER BY kind",
                    (remote_id,),
                ).fetchall(),
            )
            if "remote_report_submissions" in tables:
                scheduled.extend(
                    cast(
                        "list[tuple[str, str, int | None]]",
                        db.execute(
                            "SELECT w.kind,w.key,s.expected_revision "
                            "FROM remote_report_submissions s JOIN remote_sync_work w "
                            "ON w.kind='report_submit' AND w.key=s.id||':'||s.expected_revision "
                            "WHERE s.id=? ORDER BY s.expected_revision",
                            (remote_id,),
                        ).fetchall(),
                    )
                )
            operations = [
                {
                    "kind": kind,
                    **({"expected_revision": revision} if revision is not None else {}),
                    **synchronization_snapshot(db, kind, operation_key),
                }
                for kind, operation_key, revision in scheduled
                if kind in SYNC_KINDS
            ]
            if operations:
                synchronization["operations"] = operations
        coverage = (
            cast(
                "tuple[str] | None",
                db.execute(
                    "SELECT status FROM wake_coverage WHERE message_id=?", (key,)
                ).fetchone(),
            )
            if "wake_coverage" in tables
            else None
        )
        retry = (
            cast(
                "tuple[str | None] | None",
                db.execute(
                    "SELECT next_attempt_at FROM wake_dispatch WHERE recipient=?",
                    (message["recipient"],),
                ).fetchone(),
            )
            if "wake_dispatch" in tables
            else None
        )
        return {
            "id": key,
            "remote_id": remote_id,
            "sender": message["sender"],
            "recipient": message["recipient"],
            "reply_to": message["reply_to"],
            "transport": {
                "status": outgoing[1]
                if outgoing
                else "delivered_local"
                if incoming
                else "stored_local",
                "source": "relay_snapshot"
                if outgoing and outgoing[1] != "queued_local"
                else "local",
                "observed_at": sync["last_success_at"]
                if outgoing
                else None
                if incoming
                else message["sent_at"],
            },
            "wake": self._wake_status(message, coverage, retry),
            "acknowledged_at": message["acknowledged_at"],
            "work_report": self._work_report(db, tables, remote_id) if outgoing else None,
            "synchronization": synchronization,
        }

    @staticmethod
    def _wake_status(
        message: dict[str, object],
        coverage: tuple[str] | None,
        retry: tuple[str | None] | None,
    ) -> dict[str, object]:
        status = cast("str | None", message["wake_status"])
        via = cast("str | None", message["wake_via"])
        return {
            "status": status if status in WAKE_STATUSES else None,
            "via": via if via in WAKE_ADAPTERS else None,
            "reason": safe_wake_reason(status, via, cast("str | None", message["wake_reason"])),
            "checked_at": message["wake_checked_at"],
            "coverage_status": coverage[0] if coverage and coverage[0] in WAKE_STATUSES else None,
            "retry_at": retry[0] if retry and message["acknowledged_at"] is None else None,  # pylint: disable=clean-code-business-policy-literal
        }

    @staticmethod
    def _work_report(db: sqlite3.Connection, tables: set[str], remote_id: str | None) -> object:
        row = (
            cast(
                "tuple[str] | None",
                db.execute(
                    "SELECT payload FROM remote_reports WHERE id=?", (remote_id,)
                ).fetchone(),
            )
            if "remote_reports" in tables
            else None
        )
        return (
            llm_bus_policy.parse_report(cast("object", json.loads(row[0])), remote_id or "")
            if row
            else None
        )

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            try:
                ensure_schema(db, routing_scope)
            except ValueError as error:
                raise BusError(str(error)) from error
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 10000")
        deadline = time.monotonic() + SQLITE_TIMEOUT_SECONDS
        while True:
            try:
                db.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError as error:
                # Journal initialization can bypass SQLite's busy handler on a new file.
                if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                    db.close()
                    raise
                time.sleep(SQLITE_BUSY_RETRY_SECONDS)
        return db

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        with closing(self._connect()) as db:
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def register(
        self, kind: str, session_id: str, project: str, *, messaging_socket: str | None = None
    ) -> str:
        """Upsert session address without deleting its pending messages."""
        scope = routing_scope(project)
        with self.transaction() as db:
            agent_address = self._upsert_agent(db, kind, session_id, project, scope)
            db.execute(
                "UPDATE agents SET messaging_socket = ? WHERE address = ?",
                (messaging_socket if kind == "claude" else None, agent_address),
            )
            return agent_address

    def messaging_socket(self, agent_address: str) -> str | None:
        """Read the native endpoint reported by the recipient's own hook."""
        with closing(self._connect()) as db:
            row = cast(
                "tuple[str | None] | None",
                db.execute(
                    "SELECT messaging_socket FROM agents WHERE address = ? AND ended_at IS NULL",
                    (agent_address,),
                ).fetchone(),
            )
        return row[0] if row is not None else None

    @staticmethod
    def _upsert_agent(
        db: sqlite3.Connection, kind: str, session_id: str, project: str, scope: str
    ) -> str:
        agent_address = address(kind, session_id)
        db.execute(
            "INSERT INTO agents(address, kind, project, scope, last_seen, ended_at) "
            "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NULL) "
            "ON CONFLICT(address) DO UPDATE SET project = excluded.project, "
            "scope = excluded.scope, "
            "last_seen = excluded.last_seen, ended_at = NULL",
            (agent_address, kind, project, scope),
        )
        return agent_address

    def end_session(self, kind: str, session_id: str) -> None:
        """Mark a registered session closed without consuming its pending messages."""
        with self.transaction() as db:
            db.execute(
                "UPDATE agents SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
                "messaging_socket = NULL "
                "WHERE address = ?",
                (address(kind, session_id),),
            )

    def agents(self, project: str | None = None) -> list[Agent]:
        """List sessions active within the last day; presence is not guaranteed."""
        clauses = ["ended_at IS NULL"]
        parameters: list[str] = []
        if project is not None:
            clauses.append("scope = ?")
            parameters.append(routing_scope(project))
        return self._read_agents(clauses, parameters)

    def all_agents(self) -> list[Agent]:
        """List all registered sessions, including ended ones."""
        return self._read_agents([], [])

    def _read_agents(self, clauses: list[str], parameters: list[str]) -> list[Agent]:
        with self.transaction() as db:
            expire_idle_agents(db)
            query = "SELECT address, kind, project, last_seen, ended_at FROM agents"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            rows = cast(
                "list[tuple[str, str, str, str, str | None]]",
                db.execute(query + " ORDER BY last_seen DESC", parameters).fetchall(),
            )
        return [
            Agent(address=row[0], kind=row[1], project=row[2], last_seen=row[3], ended_at=row[4])
            for row in rows
        ]

    def is_ended(self, agent_address: str) -> bool:
        """Whether a session ended or has been idle for a day."""
        with self.transaction() as db:
            expire_idle_agents(db)
            row = cast(
                "tuple[str | None] | None",
                db.execute(
                    "SELECT ended_at FROM agents WHERE address = ?", (agent_address,)
                ).fetchone(),
            )
        return row is not None and row[0] is not None

    def send(
        self,
        sender_context: SenderContext,
        recipient: str,
        body: str,
        *,
        cross_folder: bool = False,
        reply_to: int | None = None,
    ) -> Message:
        """Atomically register sender, check folder, and persist message."""
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            sender = self._upsert_agent(
                db,
                sender_context.kind,
                sender_context.session_id,
                sender_context.project,
                routing_scope(sender_context.project),
            )
            return self.send_in_transaction(
                db, sender, recipient, body, cross_folder=cross_folder, reply_to=reply_to
            )

    def send_in_transaction(  # noqa: PLR0913 - same send contract plus caller-owned transaction
        self,
        db: sqlite3.Connection,
        sender: str,
        recipient: str,
        body: str,
        *,
        cross_folder: bool = False,
        reply_to: int | None = None,
    ) -> Message:
        """Persist for an already registered sender without changing its activity or lifecycle."""
        if not body.strip():
            raise BusError("Message body cannot be empty")
        if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise BusError("Message body exceeds 64 KiB")
        sender_row = cast(
            "tuple[str, str] | None",
            db.execute("SELECT project, scope FROM agents WHERE address = ?", (sender,)).fetchone(),
        )
        if sender_row is None:
            raise BusError(f"Unknown sender {sender!r}; register the session first")
        sender_project, sender_scope = sender_row
        recipient_row = cast(
            "tuple[str, str] | None",
            db.execute(
                "SELECT project, scope FROM agents WHERE address = ?", (recipient,)
            ).fetchone(),
        )
        if recipient_row is None:
            raise BusError(f"Unknown recipient {recipient!r}; run `llm-bus list` first")
        if sender_scope != recipient_row[1] and not cross_folder:
            raise BusError("Recipient is in another repository or folder; use --cross-folder")
        self.validate_reply(db, reply_to, (sender, sender_scope), (recipient, recipient_row[1]))
        cursor = db.execute(
            "INSERT INTO messages(sender, recipient, body, sender_cwd, recipient_cwd, "
            "sender_scope, recipient_scope, reply_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sender,
                recipient,
                body,
                sender_project,
                recipient_row[0],
                sender_scope,
                recipient_row[1],
                reply_to,
            ),
        )
        message_id = cursor.lastrowid
        if message_id is None:
            raise RuntimeError("SQLite did not return a message ID")
        self.request_dispatch(db, recipient)
        row = cast(
            "tuple[int, str, str, str, str | None, str | None, str] | None",
            db.execute(
                "SELECT id, sender, recipient, body, sender_cwd, recipient_cwd, sent_at "
                "FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone(),
        )
        if row is None:
            raise RuntimeError("Inserted message could not be read")
        return Message(
            id=row[0],
            sender=row[1],
            recipient=row[2],
            body=row[3],
            sender_cwd=row[4],
            recipient_cwd=row[5],
            sent_at=row[6],
            reply_to=reply_to,
        )

    @staticmethod
    def validate_reply(
        db: sqlite3.Connection,
        parent_id: int | None,
        sender: tuple[str, str],
        recipient: tuple[str, str],
    ) -> None:
        """Bind a retained parent to the same participants and their individual scopes."""
        if parent_id is None:
            return
        if isinstance(parent_id, bool) or parent_id < 1:
            raise BusError("Reply ID must be a positive integer")
        parent = cast(
            "tuple[str, str, str | None, str | None] | None",
            db.execute(
                "SELECT sender,recipient,sender_scope,recipient_scope FROM messages WHERE id=?",
                (parent_id,),
            ).fetchone(),
        )
        if parent is None:
            raise BusError("Unknown reply parent")
        parent_sender, parent_recipient, parent_sender_scope, parent_recipient_scope = parent
        if sorted((parent_sender, parent_recipient)) != sorted((sender[0], recipient[0])):
            raise BusError("Reply participants do not match parent")
        if (
            parent_sender_scope is None
            or parent_recipient_scope is None
            or sorted(
                ((parent_sender, parent_sender_scope), (parent_recipient, parent_recipient_scope))
            )
            != sorted((sender, recipient))
        ):
            raise BusError("Reply participant scopes do not match parent")

    @staticmethod
    def request_dispatch(db: sqlite3.Connection, recipient: str) -> None:
        """Count a new native inbox member in the same insertion transaction."""
        db.execute(
            "INSERT INTO wake_dispatch(recipient,requested_generation) VALUES (?,1) "
            "ON CONFLICT(recipient) DO UPDATE SET "
            "requested_generation=requested_generation+1,next_attempt_at=NULL",
            (recipient,),
        )

    def pending_members(self, recipient: str) -> list[tuple[int, str]]:
        """Read permitted pending identities across bounded pages, without message bodies."""
        members: list[tuple[int, str]] = []
        with closing(self._connect()) as db, closing(self._pending_pages(recipient, db)) as pages:
            for page in pages:
                members.extend(
                    (row.id, row.sender) for row in page if self._pending_permitted(row, db)
                )
        return members

    def guidance_needed(self, recipient: str, version: int, *, reset: bool) -> bool:
        """Remember only guidance format, never permissions or peer policy."""
        with self.transaction() as db:
            row = cast(
                "tuple[int] | None",
                db.execute(
                    "SELECT version FROM hook_guidance WHERE address=?", (recipient,)
                ).fetchone(),
            )
            db.execute(
                "INSERT INTO hook_guidance(address,version) VALUES (?,?) "
                "ON CONFLICT(address) DO UPDATE SET version=excluded.version",
                (recipient, version),
            )
        return reset or row is None or row[0] != version

    def message_acknowledged(self, recipient: str, message_id: int, sender: str) -> bool:
        """Verify a wake's full identity against durable acknowledgement state."""
        with closing(self._connect()) as db:
            return (
                db.execute(
                    "SELECT 1 FROM messages WHERE id=? AND recipient=? AND sender=? "
                    "AND acknowledged_at IS NOT NULL",
                    (message_id, recipient, sender),
                ).fetchone()
                is not None
            )

    @staticmethod
    def _pending_permitted(row: PendingRow, db: sqlite3.Connection) -> bool:
        if "/" not in row.sender:
            return True
        # Registration can move between rows already fetched in the same page.
        recipient = cast(
            "tuple[str | None] | None",
            db.execute("SELECT scope FROM agents WHERE address=?", (row.recipient,)).fetchone(),
        )
        if (
            row.envelope is None
            or row.connection is None
            or row.stored_scope is None
            or recipient is None
            or recipient[0] is None
        ):
            return False
        try:
            value = cast("object", json.loads(row.envelope))
            data = llm_bus_policy.object_fields(value)
            if any(
                not isinstance(data.get(key), str) for key in ("sender", "recipient", "repo_id")
            ):
                return False
            envelope = cast("MessageIdentity", cast("object", data))
            return (
                row.sender == envelope["sender"]
                and row.recipient == envelope["recipient"].rsplit("/", 1)[-1]
                and llm_bus_policy.permits_inbox(
                    envelope, row.stored_scope, recipient[0], row.connection
                )
            )
        except (ValueError, KeyError, TypeError):
            return False

    @staticmethod
    def _pending_query(content: PendingContent) -> str:
        return (
            "SELECT m.id,m.sender,m.recipient,"  # noqa: S608 - projection is internal SQL only
            + content
            + ",m.recipient_scope,CASE WHEN json_valid(r.envelope) THEN "
            "json_object('sender',json_extract(r.envelope,'$.sender'),"
            "'recipient',json_extract(r.envelope,'$.recipient'),"
            "'repo_id',json_extract(r.envelope,'$.repo_id')) END,b.connection,m.reply_to "
            "FROM messages m JOIN agents a ON a.address=m.recipient "
            "LEFT JOIN remote_imports r ON r.local_id=m.id AND "
            "(SELECT COUNT(*) FROM remote_imports i WHERE i.local_id=m.id)=1 "
            "LEFT JOIN remote_binding b ON b.id=1 "
        )

    def _pending_pages(
        self,
        recipient: str,
        db: sqlite3.Connection,
        *,
        page_size: int = MAX_BATCH,
        content: PendingContent = "NULL,NULL,NULL,NULL",
    ) -> Generator[list[PendingRow]]:
        """Bound each read and restart from the oldest pending row on every invocation."""
        if not 1 <= page_size <= MAX_BATCH:
            raise BusError(PENDING_PAGE_ERROR)
        after = 0
        while True:
            rows = [
                PendingRow(*row)
                for row in cast(
                    "list[PendingTuple]",
                    db.execute(
                        self._pending_query(content)
                        + "WHERE m.recipient=? AND m.acknowledged_at IS NULL "
                        "AND m.id>? ORDER BY m.id LIMIT ?",
                        (recipient, after, page_size),
                    ).fetchall(),
                )
            ]
            if not rows:
                return
            yield rows
            after = rows[-1].id

    def pending_allowed(self, message_id: int) -> bool:
        """Recheck remote pending exposure against fresh machine-owned policy."""
        with closing(self._connect()) as db:
            result = cast(
                "PendingTuple | None",
                db.execute(
                    self._pending_query("NULL,NULL,NULL,NULL") + "WHERE m.id=?",
                    (message_id,),
                ).fetchone(),
            )
            return result is not None and self._pending_permitted(PendingRow(*result), db)

    def inbox(self, recipient: str, limit: int = MAX_BATCH) -> list[Message]:
        """Read pending messages without consuming them."""
        if not 1 <= limit <= MAX_BATCH:
            raise BusError("Inbox limit must be between 1 and 100")
        messages: list[Message] = []
        with (
            closing(self._connect()) as db,
            closing(
                self._pending_pages(
                    recipient, db, content="m.body,m.sender_cwd,m.recipient_cwd,m.sent_at"
                )
            ) as pages,
        ):
            for page in pages:
                for row in page:
                    if self._pending_permitted(row, db):
                        messages.append(
                            Message(
                                id=row.id,
                                sender=row.sender,
                                recipient=row.recipient,
                                body=cast("str", row.body),
                                sender_cwd=row.sender_cwd,
                                recipient_cwd=row.recipient_cwd,
                                sent_at=cast("str", row.sent_at),
                                reply_to=row.reply_to,
                            )
                        )
                        if len(messages) == limit:
                            return messages
        return messages

    def history(  # noqa: C901, PLR0913 - independent public history filters compose here
        self,
        *,
        folder: str | None,
        repository: str | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int = MAX_BATCH,
        conversation: int | None = None,
        peer: str | None = None,
    ) -> list[HistoryMessage]:
        """Read stored messages, newest page first or new items after a cursor."""
        # Optional public filters preserve the existing keyword API and compose one SQL query.
        # pylint: disable=too-many-arguments,too-many-locals
        if not 1 <= limit <= MAX_BATCH:
            raise BusError("History limit must be between 1 and 100")
        if (before is not None and before < 1) or (after is not None and after < 0):
            raise BusError("History cursors must be nonnegative message IDs")
        if before is not None and after is not None:
            raise BusError("Use either --before or --after, not both")
        if folder is not None and repository is not None:
            raise BusError("Use either folder or repository scope, not both")
        clauses: list[str] = []
        parameters: list[str | int] = []
        if peer is not None:
            clauses.append("(sender = ? OR recipient = ?)")
            parameters.extend((peer, peer))
        prefix = ""
        if conversation is not None:
            if isinstance(conversation, bool) or conversation < 1:
                raise BusError("Conversation ID must be a positive integer")
            clauses.append("id IN (SELECT id FROM conversation)")
        if folder is not None:
            clauses.append("(sender_cwd = ? OR recipient_cwd = ?)")
            parameters.extend((folder, folder))
        if repository is not None:
            scope = routing_scope(repository)
            clauses.append(
                "(sender_scope = ? OR recipient_scope = ? OR "
                "(sender_scope IS NULL AND sender_cwd = ?) OR "
                "(recipient_scope IS NULL AND recipient_cwd = ?))"
            )
            parameters.extend((scope, scope, repository, repository))
        if before is not None:
            clauses.append("id < ?")
            parameters.append(before)
        if after is not None:
            clauses.append("id > ?")
            parameters.append(after)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order = "ASC" if after is not None else "DESC"
        parameters.append(limit)
        with closing(self._connect()) as db:
            if conversation is not None:
                root = self._conversation_root(db, conversation)
                prefix = (
                    "WITH RECURSIVE conversation(id) AS (SELECT ? UNION "
                    "SELECT m.id FROM messages m JOIN conversation c ON m.reply_to=c.id) "
                )
                parameters.insert(0, root)
            rows = cast(
                "list[HistoryRow]",
                db.execute(
                    prefix + "SELECT id,sender,recipient,body,"  # noqa: S608
                    "sender_cwd,recipient_cwd,sent_at,acknowledged_at,"
                    "wake_status,wake_via,wake_reason,"
                    f"wake_checked_at, reply_to FROM messages{where} ORDER BY id {order} LIMIT ?",
                    parameters,
                ).fetchall(),
            )
        return [
            HistoryMessage(
                id=row[0],
                sender=row[1],
                recipient=row[2],
                body=row[3],
                sender_cwd=row[4],
                recipient_cwd=row[5],
                sent_at=row[6],
                acknowledged_at=row[7],
                wake_status=row[8],
                wake_via=row[9],
                wake_reason=row[10],
                wake_checked_at=row[11],
                reply_to=row[12],
            )
            for row in rows
        ]

    @staticmethod
    def _conversation_root(db: sqlite3.Connection, message_id: int) -> int:
        seen: set[int] = set()
        while True:
            if message_id in seen:
                raise BusError("Corrupt conversation cycle")
            seen.add(message_id)
            row = cast(
                "tuple[int | None] | None",
                db.execute("SELECT reply_to FROM messages WHERE id=?", (message_id,)).fetchone(),
            )
            if row is None:
                raise BusError("Unknown conversation message")
            if row[0] is None:
                return message_id
            message_id = row[0]

    def pending_count(self, recipient: str, *, limit: int | None = None) -> int:
        """Count permitted pending metadata, optionally stopping at a saturation limit."""
        if limit is not None and limit < 1:
            raise BusError(PENDING_COUNT_ERROR)
        count = 0
        with (
            closing(self._connect()) as db,
            closing(self._pending_pages(recipient, db)) as pages,
        ):
            for page in pages:
                for row in page:
                    if self._pending_permitted(row, db):
                        count += 1
                        if count == limit:
                            return count
        return count

    def acknowledge(self, recipient: str, ids: list[int]) -> int:
        """Acknowledge only own messages; reject mixed or unknown batches atomically."""
        if (
            not ids
            or len(ids) > MAX_BATCH
            or any(isinstance(item, bool) or item < 1 for item in ids)
        ):
            raise BusError("Provide 1-100 positive integer message IDs")
        unique_ids = list(dict.fromkeys(ids))
        with self.transaction() as db:
            acknowledged = 0
            for message_id in unique_ids:
                exists = cast(
                    "tuple[int] | None",
                    db.execute(
                        "SELECT 1 FROM messages WHERE recipient = ? AND id = ?",
                        (recipient, message_id),
                    ).fetchone(),
                )
                if exists is None:
                    raise BusError("One or more message IDs do not belong to this agent")
                cursor = db.execute(
                    "UPDATE messages SET acknowledged_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE recipient = ? AND acknowledged_at IS NULL AND id = ?",
                    (recipient, message_id),
                )
                acknowledged += cursor.rowcount
            return acknowledged

"""SQLite mailbox shared by local agent sessions."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypedDict, cast

import llm_bus_collab_state
import llm_bus_policy

if TYPE_CHECKING:
    from collections.abc import Generator

    from llm_bus_policy import MessageIdentity

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_MESSAGE_BYTES = 64 * 1024
MAX_BATCH = 100
SESSION_IDLE_HOURS = 24
DB_ENV = "LLM_BUS_DB"
WINDOWS_DATA_ENV = "LOCALAPPDATA"
GIT_ENV_PREFIX = "GIT_"  # pylint: disable=clean-code-business-policy-literal
SCHEMA = """
    CREATE TABLE IF NOT EXISTS agents (
        address TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        project TEXT NOT NULL,
        scope TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        ended_at TEXT,
        messaging_socket TEXT
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        sender TEXT NOT NULL REFERENCES agents(address),
        recipient TEXT NOT NULL REFERENCES agents(address),
        body TEXT NOT NULL,
        sender_cwd TEXT,
        recipient_cwd TEXT,
        sender_scope TEXT,
        recipient_scope TEXT,
        sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        acknowledged_at TEXT,
        wake_status TEXT,
        wake_via TEXT,
        wake_reason TEXT,
        wake_checked_at TEXT
    );
    CREATE INDEX IF NOT EXISTS messages_pending
        ON messages(recipient, acknowledged_at, id);
"""


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
]


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

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            agent_columns = {
                row[1]
                for row in cast(
                    "list[tuple[int, str, str, int, object, int]]",
                    db.execute("PRAGMA table_info(agents)").fetchall(),
                )
            }
            if "ended_at" not in agent_columns:
                db.execute("ALTER TABLE agents ADD COLUMN ended_at TEXT")
            if "messaging_socket" not in agent_columns:
                db.execute("ALTER TABLE agents ADD COLUMN messaging_socket TEXT")
            if "scope" not in agent_columns:
                db.execute("ALTER TABLE agents ADD COLUMN scope TEXT")
                projects = cast(
                    "list[tuple[str]]",
                    db.execute("SELECT DISTINCT project FROM agents").fetchall(),
                )
                for (project,) in projects:
                    db.execute(
                        "UPDATE agents SET scope = ? WHERE project = ?",
                        (routing_scope(project), project),
                    )
            columns = {
                row[1]
                for row in cast(
                    "list[tuple[int, str, str, int, object, int]]",
                    db.execute("PRAGMA table_info(messages)").fetchall(),
                )
            }
            if "sender_cwd" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN sender_cwd TEXT")
            if "recipient_cwd" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN recipient_cwd TEXT")
            for column in (
                "sender_scope",
                "recipient_scope",
                "wake_status",
                "wake_via",
                "wake_reason",
                "wake_checked_at",
            ):
                if column not in columns:
                    db.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
            if "sender_scope" not in columns or "recipient_scope" not in columns:
                self._backfill_message_scopes(db)
            db.execute(
                "CREATE INDEX IF NOT EXISTS messages_sender_folder ON messages(sender_cwd, id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS msg_recipient_folder ON messages(recipient_cwd, id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS messages_sender_scope ON messages(sender_scope, id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS messages_recipient_scope "
                "ON messages(recipient_scope, id)"
            )
            db.commit()
        self.path.chmod(0o600)

    @staticmethod
    def _backfill_message_scopes(db: sqlite3.Connection) -> None:
        for select_query, update_query in (
            (
                "SELECT DISTINCT sender_cwd FROM messages WHERE sender_cwd IS NOT NULL",
                "UPDATE messages SET sender_scope = ? WHERE sender_cwd = ?",
            ),
            (
                "SELECT DISTINCT recipient_cwd FROM messages WHERE recipient_cwd IS NOT NULL",
                "UPDATE messages SET recipient_scope = ? WHERE recipient_cwd = ?",
            ),
        ):
            folders = cast(
                "list[tuple[str]]",
                db.execute(select_query).fetchall(),
            )
            for (folder,) in folders:
                db.execute(update_query, (routing_scope(folder), folder))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 10000")
        db.execute("PRAGMA journal_mode = WAL")
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
            self._expire_idle_agents(db)
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

    @staticmethod
    def _expire_idle_agents(db: sqlite3.Connection) -> None:
        db.execute(
            "UPDATE agents SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE ended_at IS NULL AND last_seen <= "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (f"-{SESSION_IDLE_HOURS} hours",),
        )

    def is_ended(self, agent_address: str) -> bool:
        """Whether a session ended or has been idle for a day."""
        with self.transaction() as db:
            self._expire_idle_agents(db)
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
    ) -> Message:
        """Atomically register sender, check folder, and persist message."""
        if not body.strip():
            raise BusError("Message body cannot be empty")
        if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise BusError("Message body exceeds 64 KiB")
        sender_scope = routing_scope(sender_context.project)
        with self.transaction() as db:
            sender = self._upsert_agent(
                db,
                sender_context.kind,
                sender_context.session_id,
                sender_context.project,
                sender_scope,
            )
            recipient_row = cast(
                "tuple[str, str] | None",
                db.execute(
                    "SELECT project, scope FROM agents WHERE address = ?", (recipient,)
                ).fetchone(),
            )
            if recipient_row is None or recipient.startswith(llm_bus_collab_state.SENDER_PREFIX):
                raise BusError(f"Unknown recipient {recipient!r}; run `llm-bus list` first")
            if sender_scope != recipient_row[1] and not cross_folder:
                raise BusError("Recipient is in another repository or folder; use --cross-folder")
            cursor = db.execute(
                "INSERT INTO messages(sender, recipient, body, sender_cwd, recipient_cwd, "
                "sender_scope, recipient_scope) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sender,
                    recipient,
                    body,
                    sender_context.project,
                    recipient_row[0],
                    sender_scope,
                    recipient_row[1],
                ),
            )
            message_id = cursor.lastrowid
            if message_id is None:
                raise RuntimeError("SQLite did not return a message ID")
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
        )

    def record_wake(self, message_id: int, status: str, via: str, reason: str | None) -> None:
        """Save the outcome after delivery without changing acknowledgement state."""
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE messages SET wake_status = ?, wake_via = ?, wake_reason = ?, "
                "wake_checked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (status, via, reason, message_id),
            )
            if cursor.rowcount != 1:
                raise BusError(f"Unknown message ID {message_id}")

    def pending_allowed(self, message_id: int) -> bool:
        """Recheck remote pending exposure against fresh machine-owned policy."""
        with closing(self._connect()) as db:
            row = cast(
                "tuple[str, str, str | None, str | None] | None",
                db.execute(
                    "SELECT m.sender,m.recipient,m.recipient_scope,a.scope FROM messages m "
                    "JOIN agents a ON a.address=m.recipient WHERE m.id=?",
                    (message_id,),
                ).fetchone(),
            )
            if row is None:
                return False
            if "/" not in row[0]:
                return True
            if (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='remote_imports'"
                ).fetchone()
                is None
            ):
                return False
            imported = cast(
                "tuple[str, str] | None",
                db.execute(
                    "SELECT r.envelope,b.connection FROM remote_imports r "
                    "CROSS JOIN remote_binding b "
                    "WHERE r.local_id=? AND b.id=1",
                    (message_id,),
                ).fetchone(),
            )
        sender, recipient, stored_scope, current_scope = row
        if imported is None or stored_scope is None or current_scope is None:
            return False
        envelope = cast("MessageIdentity", json.loads(imported[0]))
        return (
            sender == envelope["sender"]
            and recipient == envelope["recipient"].rsplit("/", 1)[-1]
            and llm_bus_policy.permits_inbox(envelope, stored_scope, current_scope, imported[1])
        )

    def _exposable(self, message_id: int, sender: str, recipient: str) -> bool:
        """Apply the owner's current remote or collaboration policy before showing text."""
        if sender.startswith(llm_bus_collab_state.SENDER_PREFIX):
            return llm_bus_collab_state.exposure_allowed(sender, recipient)
        return "/" not in sender or self.pending_allowed(message_id)

    def deliver_collab(self, sender: str, recipient: str, body: str) -> int:
        """Store daemon-delivered collaboration text for a registered local session.

        The sender row is marked ended so it never appears in `list` and cannot be addressed.
        """
        if not sender.startswith(llm_bus_collab_state.SENDER_PREFIX):
            raise BusError("Collaboration sender must use the collab: prefix")
        if not body.strip() or len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise BusError("Collaboration message is empty or exceeds 64 KiB")
        with self.transaction() as db:
            recipient_row = cast(
                "tuple[str, str] | None",
                db.execute(
                    "SELECT project, scope FROM agents WHERE address = ? AND kind != 'collab'",
                    (recipient,),
                ).fetchone(),
            )
            if recipient_row is None:
                raise BusError(f"Unknown recipient {recipient!r}")
            db.execute(
                "INSERT INTO agents(address, kind, project, scope, last_seen, ended_at) "
                "VALUES (?, 'collab', '', 'collab', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) "
                "ON CONFLICT(address) DO UPDATE SET last_seen = excluded.last_seen",
                (sender,),
            )
            cursor = db.execute(
                "INSERT INTO messages(sender, recipient, body, recipient_cwd, recipient_scope) "
                "VALUES (?, ?, ?, ?, ?)",
                (sender, recipient, body, recipient_row[0], recipient_row[1]),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a message ID")
            return cursor.lastrowid

    def inbox(self, recipient: str, limit: int = MAX_BATCH) -> list[Message]:
        """Read pending messages without consuming them."""
        if not 1 <= limit <= MAX_BATCH:
            raise BusError("Inbox limit must be between 1 and 100")
        with closing(self._connect()) as db:
            rows = cast(
                "list[tuple[int, str, str, str, str | None, str | None, str]]",
                db.execute(
                    "SELECT id, sender, recipient, body, sender_cwd, recipient_cwd, sent_at "
                    "FROM messages "
                    "WHERE recipient = ? AND acknowledged_at IS NULL ORDER BY id",
                    (recipient,),
                ).fetchall(),
            )
        return [
            Message(
                id=row[0],
                sender=row[1],
                recipient=row[2],
                body=row[3],
                sender_cwd=row[4],
                recipient_cwd=row[5],
                sent_at=row[6],
            )
            for row in rows
            if self._exposable(row[0], row[1], row[2])
        ][:limit]

    def history(
        self,
        *,
        folder: str | None,
        repository: str | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int = MAX_BATCH,
    ) -> list[HistoryMessage]:
        """Read stored messages, newest page first or new items after a cursor."""
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
            rows = cast(
                "list[HistoryRow]",
                db.execute(
                    "SELECT id, sender, recipient, body, sender_cwd, recipient_cwd, "  # noqa: S608
                    "sent_at, acknowledged_at, wake_status, wake_via, wake_reason, "
                    f"wake_checked_at FROM messages{where} ORDER BY id {order} LIMIT ?",
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
            )
            for row in rows
        ]

    def pending_count(self, recipient: str) -> int:
        """Count unread messages for low-context hook notification."""
        with closing(self._connect()) as db:
            rows = cast(
                "list[tuple[int, str]]",
                db.execute(
                    "SELECT id,sender FROM messages WHERE recipient=? AND acknowledged_at IS NULL",
                    (recipient,),
                ).fetchall(),
            )
        return sum(self._exposable(message_id, sender, recipient) for message_id, sender in rows)

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

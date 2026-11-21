"""Single transactional owner of the additive local mailbox schema."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from llm_bus.coordination import decisions, runs, stalls, work_state
from llm_bus.coordination import store as coordination_store
from llm_bus.mailbox import migrations

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection
SCHEMA_VERSION = 18
SYNC_SCHEMA_VERSION = 2
WAKE_SCHEMA_VERSION = 3
LOCK_SCHEMA_VERSION = 5
LOCK_CATALOG_SCHEMA_VERSION = 6
LOCK_SEEN_SCHEMA_VERSION = 7
REPLY_SCHEMA_VERSION = 4
COORDINATION_SCHEMA_VERSION = 12
LABEL_SCHEMA_VERSION = 9
PUBLICATION_SCHEMA_VERSION = 10
DELEGATION_SCHEMA_VERSION = 13
HOST_PROCESS_SCHEMA_VERSION = 14
WORK_STATE_SCHEMA_VERSION = 15
STALL_SCHEMA_VERSION = 16
RUN_SCHEMA_VERSION = 17
DECISION_RUN_SCHEMA_VERSION = 18
SYNC_WORK_TABLE = (
    "CREATE TABLE IF NOT EXISTS remote_sync_work (kind TEXT NOT NULL, key TEXT NOT NULL, "
    "attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_attempt_at TEXT, "
    "last_success_at TEXT, error_code TEXT, first_accepted_at TEXT, polling_suspended_at TEXT, "
    "PRIMARY KEY(kind,key))"
)
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
MONITOR_TABLE = (
    "CREATE TABLE IF NOT EXISTS remote_monitor "
    "(monitor_id INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, envelope TEXT NOT NULL, "
    "scope TEXT NOT NULL, connection TEXT NOT NULL)"
)


TABLES = (
    MONITOR_TABLE,
    (
        "CREATE TABLE IF NOT EXISTS remote_publications "
        "(address TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS remote_binding (id INTEGER PRIMARY KEY CHECK(id=1), "
        "connection TEXT NOT NULL)"
    ),
    ("CREATE TABLE IF NOT EXISTS remote_proxies (address TEXT PRIMARY KEY)"),
    (
        "CREATE TABLE IF NOT EXISTS remote_peers (address TEXT PRIMARY KEY, repo_id TEXT NOT "
        "NULL, payload TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS remote_outbox (id TEXT PRIMARY KEY, local_id INTEGER NOT "
        "NULL REFERENCES messages(id), envelope TEXT NOT NULL, state TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS remote_imports (id TEXT PRIMARY KEY, local_id INTEGER NOT "
        "NULL REFERENCES messages(id), envelope TEXT NOT NULL, ack_synced INTEGER NOT NULL "
        "DEFAULT 0, wake_claimed INTEGER NOT NULL DEFAULT 0)"
    ),
)


def create_tables(db: Connection) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    for statement in TABLES:
        db.execute(statement)
    db.execute(
        "CREATE TABLE IF NOT EXISTS remote_report_submissions "
        "(id TEXT NOT NULL REFERENCES remote_imports(id), "
        "expected_revision INTEGER NOT NULL, payload TEXT NOT NULL, "
        "synced INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(id,expected_revision))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS remote_reports "
        "(id TEXT PRIMARY KEY REFERENCES remote_outbox(id), payload TEXT NOT NULL)"
    )

    db.execute(
        "CREATE TABLE IF NOT EXISTS remote_report_windows "
        "(id TEXT PRIMARY KEY REFERENCES remote_outbox(id), expires_at TEXT NOT NULL)"
    )


def _version(db: Connection) -> int:
    version = cast("tuple[int]", db.execute("PRAGMA user_version").fetchone())[0]
    if version > SCHEMA_VERSION:
        raise ValueError("Mailbox schema is newer than this llm-bus version")
    return version


def _sync_tables(db: Connection) -> None:
    db.execute(SYNC_WORK_TABLE)
    db.execute(
        "CREATE TABLE IF NOT EXISTS remote_peer_sync (repo_id TEXT PRIMARY KEY, "
        "last_attempt_at TEXT, last_success_at TEXT, next_attempt_at TEXT, error_code TEXT)"
    )


def _wake_tables(db: Connection) -> None:
    db.execute(
        "CREATE TABLE wake_dispatch (recipient TEXT PRIMARY KEY, token TEXT, lease_until TEXT, "
        "requested_generation INTEGER NOT NULL DEFAULT 0, "
        "dispatched_generation INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, "
        "attempts INTEGER NOT NULL DEFAULT 0, capability TEXT)"
    )
    db.execute(
        "CREATE TABLE wake_coverage (message_id INTEGER PRIMARY KEY REFERENCES messages(id), "
        "attempt_token TEXT NOT NULL, status TEXT NOT NULL, checked_at TEXT NOT NULL)"
    )
    db.execute("CREATE TABLE hook_guidance (address TEXT PRIMARY KEY, version INTEGER)")
    # Legacy permanent claims cannot establish membership of a successful native request.
    db.execute(
        "INSERT INTO wake_dispatch(recipient,requested_generation) "
        "SELECT recipient,COUNT(*) FROM messages WHERE acknowledged_at IS NULL "
        "AND recipient NOT IN (SELECT address FROM remote_proxies) GROUP BY recipient"
    )


def create_lock_tables(db: Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS resource_locks (scope TEXT NOT NULL, resource TEXT NOT NULL, "
        "holder TEXT NOT NULL REFERENCES agents(address), reason TEXT, acquired_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL, check_at TEXT NOT NULL, checked_at TEXT, "
        "PRIMARY KEY(scope,resource))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS resource_lock_waiters (scope TEXT NOT NULL, "
        "resource TEXT NOT NULL, waiter TEXT NOT NULL REFERENCES agents(address), reason TEXT, "
        "requested_at TEXT NOT NULL, nudged_at TEXT, PRIMARY KEY(scope,resource,waiter))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS resource_locks_holder ON resource_locks(holder,expires_at)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS resource_locks_check ON resource_locks(check_at)")


def _lock_migrations(db: Connection, version: int) -> None:
    if version < LOCK_SCHEMA_VERSION:
        create_lock_tables(db)
    if version < LOCK_CATALOG_SCHEMA_VERSION:
        migrations.create_lock_catalog(db)
    db.execute(
        "CREATE TABLE IF NOT EXISTS lock_catalog_seen "
        "(address TEXT PRIMARY KEY, signature TEXT NOT NULL)"
    )


def ensure_schema(  # noqa: C901, PLR0912
    db: Connection, resolve_scope: Callable[[str], str]
) -> None:
    """Serialize migrations; reopening the current version performs only a version read."""
    # pylint: disable=too-many-branches
    version = _version(db)
    if version == SCHEMA_VERSION:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        version = _version(db)
        if version < 1:
            create_tables(db)
            migrations.migrate(db, resolve_scope)
        if version < SYNC_SCHEMA_VERSION:
            _sync_tables(db)
        if version < WAKE_SCHEMA_VERSION:
            _wake_tables(db)
        if version < REPLY_SCHEMA_VERSION:
            columns = migrations.message_columns(db)
            if "reply_to" not in columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN reply_to INTEGER REFERENCES messages(id)"
                )
            db.execute("CREATE INDEX IF NOT EXISTS messages_reply_to ON messages(reply_to,id)")
        if version < LOCK_SEEN_SCHEMA_VERSION:
            _lock_migrations(db, version)
        if version < COORDINATION_SCHEMA_VERSION:
            coordination_store.create_tables(db)
        if version < LABEL_SCHEMA_VERSION:
            db.execute(
                "CREATE TABLE IF NOT EXISTS agent_labels (scope TEXT NOT NULL, "
                "label_key TEXT NOT NULL, label TEXT NOT NULL, "
                "address TEXT NOT NULL UNIQUE REFERENCES agents(address), "
                "PRIMARY KEY(scope,label_key))"
            )
        if version < PUBLICATION_SCHEMA_VERSION:
            db.execute(
                "CREATE TABLE IF NOT EXISTS session_publications (address TEXT PRIMARY KEY "
                "REFERENCES agents(address), revision INTEGER NOT NULL, payload TEXT NOT NULL)"
            )
        if version < DELEGATION_SCHEMA_VERSION:
            coordination_store.create_delegation_tables(db)
        if version < HOST_PROCESS_SCHEMA_VERSION:
            migrations.add_host_process(db)
        if version < WORK_STATE_SCHEMA_VERSION:
            work_state.create_tables(db)
        if version < STALL_SCHEMA_VERSION:
            stalls.create_tables(db)
        if version < RUN_SCHEMA_VERSION:
            runs.create_tables(db)
        if version < DECISION_RUN_SCHEMA_VERSION:
            decisions.create_tables(db)
        if version < SCHEMA_VERSION:
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.commit()
    except BaseException:
        db.rollback()
        raise

"""Single transactional owner of the additive local mailbox schema."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection
SCHEMA_VERSION = 7
SYNC_SCHEMA_VERSION = 2
WAKE_SCHEMA_VERSION = 3
LOCK_SCHEMA_VERSION = 5
LOCK_CATALOG_SCHEMA_VERSION = 6
LOCK_SEEN_SCHEMA_VERSION = 7
REPLY_SCHEMA_VERSION = 4
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


def _backfill_message_scopes(db: Connection, resolve_scope: Callable[[str], str]) -> None:
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
            db.execute(update_query, (resolve_scope(folder), folder))


def _create_tables(db: Connection) -> None:
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


def _migrate_agents(db: Connection, resolve_scope: Callable[[str], str]) -> None:
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
                (resolve_scope(project), project),
            )


def _migrate(db: Connection, resolve_scope: Callable[[str], str]) -> None:
    _create_tables(db)
    _migrate_agents(db, resolve_scope)
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
        _backfill_message_scopes(db, resolve_scope)
    db.execute("CREATE INDEX IF NOT EXISTS messages_sender_folder ON messages(sender_cwd, id)")
    db.execute("CREATE INDEX IF NOT EXISTS msg_recipient_folder ON messages(recipient_cwd, id)")
    db.execute("CREATE INDEX IF NOT EXISTS messages_sender_scope ON messages(sender_scope, id)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS messages_recipient_scope ON messages(recipient_scope, id)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS remote_imports_local_id ON remote_imports(local_id)")


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


def _lock_tables(db: Connection) -> None:
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


def _lock_catalog(db: Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS resource_catalog (scope TEXT NOT NULL, resource TEXT NOT NULL, "
        "description TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL, "
        "last_used_at TEXT NOT NULL, PRIMARY KEY(scope,resource))"
    )
    db.execute(
        "INSERT OR IGNORE INTO resource_catalog"
        "(scope,resource,description,created_by,created_at,last_used_at) "
        "SELECT scope,resource,COALESCE(reason,'(no description)'),holder,acquired_at,acquired_at "
        "FROM resource_locks"
    )


def _lock_migrations(db: Connection, version: int) -> None:
    if version < LOCK_SCHEMA_VERSION:
        _lock_tables(db)
    if version < LOCK_CATALOG_SCHEMA_VERSION:
        _lock_catalog(db)
    db.execute(
        "CREATE TABLE IF NOT EXISTS lock_catalog_seen "
        "(address TEXT PRIMARY KEY, signature TEXT NOT NULL)"
    )


def ensure_schema(db: Connection, resolve_scope: Callable[[str], str]) -> None:
    """Serialize migrations; reopening the current version performs only a version read."""
    version = _version(db)
    if version == SCHEMA_VERSION:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        version = _version(db)
        if version < 1:
            _migrate(db, resolve_scope)
        if version < SYNC_SCHEMA_VERSION:
            _sync_tables(db)
        if version < WAKE_SCHEMA_VERSION:
            _wake_tables(db)
        if version < REPLY_SCHEMA_VERSION:
            columns = {
                row[1]
                for row in cast(
                    "list[tuple[int, str, str, int, object, int]]",
                    db.execute("PRAGMA table_info(messages)").fetchall(),
                )
            }
            if "reply_to" not in columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN reply_to INTEGER REFERENCES messages(id)"
                )
            db.execute("CREATE INDEX IF NOT EXISTS messages_reply_to ON messages(reply_to,id)")
        if version < LOCK_SEEN_SCHEMA_VERSION:
            _lock_migrations(db, version)
        if version < SCHEMA_VERSION:
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.commit()
    except BaseException:
        db.rollback()
        raise

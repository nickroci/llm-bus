"""Strict typed work reports, separate from transport and native inboxes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict, cast

from llm_bus_policy import REPORT_REASONS, Config, load_config, object_fields
from llm_bus_remote_store import RemoteStore
from llm_bus_store import BusError, Store

if TYPE_CHECKING:
    from llm_bus_remote_store import Delivery, Envelope

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
MAX_REVISION = 2**53 - 1
TERMINAL = ("completed", "failed")
TRANSITION_CONFLICT = "Report pending or lifecycle transition conflicts"


class Report(TypedDict):
    message_id: str
    revision: int
    status: str | None
    reason: str | None


def validate_status(status: object, reason: object) -> tuple[str, str | None]:
    if (
        not isinstance(status, str)
        or status not in REPORT_REASONS
        or (reason is not None and not isinstance(reason, str))
        or reason not in REPORT_REASONS[status]
    ):
        raise BusError("Invalid built-in report status/reason")
    return status, reason


def parse_report(value: object, message_id: str) -> Report:
    data = object_fields(value)
    revision = data.get("revision")
    if (
        set(data) != {"message_id", "revision", "status", "reason"}
        or not UUID.fullmatch(message_id)
        or data.get("message_id") != message_id
    ):
        raise BusError("Invalid work report response")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 0 <= revision <= MAX_REVISION
    ):
        raise BusError("Invalid work report revision")
    if revision == 0:
        if data["status"] is not None or data["reason"] is not None:
            raise BusError("Invalid initial work report")
        return Report(message_id=message_id, revision=0, status=None, reason=None)
    status, reason = validate_status(data["status"], data["reason"])
    return Report(message_id=message_id, revision=revision, status=status, reason=reason)


def report_deadline(value: object) -> str:
    if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
        raise BusError("Invalid relay report deadline")
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise BusError("Invalid relay report deadline") from error
    return value


class Reports:
    """Durable submissions and validated snapshots; no wake or inbox operations."""

    remote: RemoteStore

    def __init__(self, store: Store, connection: str) -> None:
        self.remote = RemoteStore(store, connection)
        with store.transaction() as db:
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

    def deadline(self, remote_id: str, value: object) -> None:
        expires = report_deadline(value)
        with self.remote.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = cast(
                "tuple[str] | None",
                db.execute(
                    "SELECT expires_at FROM remote_report_windows WHERE id=?", (remote_id,)
                ).fetchone(),
            )
            if prior and prior[0] != expires:
                raise BusError("Relay report deadline changed")
            db.execute(
                "INSERT OR IGNORE INTO remote_report_windows VALUES (?,?)", (remote_id, expires)
            )

    def pollable(self) -> list[Envelope]:
        with self.remote.store.transaction() as db:
            rows = cast(
                "list[tuple[str, str]]",
                db.execute(
                    "SELECT o.envelope,w.expires_at FROM remote_outbox o "
                    "JOIN remote_report_windows w ON w.id=o.id "
                    "WHERE o.state NOT IN ('queued_local','expired')"
                ).fetchall(),
            )
        result: list[Envelope] = []
        for payload, expires in rows:
            envelope = cast("Envelope", json.loads(payload))
            snapshot = self.snapshot(envelope["id"])
            if datetime.fromisoformat(report_deadline(expires)) > datetime.now(UTC) and (
                snapshot is None or snapshot["status"] not in TERMINAL
            ):
                result.append(envelope)
        return result

    def statuses(self) -> list[dict[str, object]]:
        rows = self.remote.statuses()
        for row in rows:
            row["work_report"] = self.snapshot(cast("str", row["remote_id"]))
        return rows

    def imported(self, local_id: int, native: str | None = None) -> Delivery:
        with self.remote.store.transaction() as db:
            row = cast(
                "tuple[str, str, str] | None",
                db.execute(
                    "SELECT r.envelope,m.recipient,m.recipient_scope FROM remote_imports r "
                    "JOIN messages m ON m.id=r.local_id JOIN agents a ON a.address=m.recipient "
                    "WHERE r.local_id=? AND a.scope=m.recipient_scope",
                    (local_id,),
                ).fetchone(),
            )
        if row is None or (native is not None and native != row[1]):
            raise BusError("Report requires an imported message for this native recipient")
        item = cast("Delivery", json.loads(row[0]))
        config = load_config()
        repo = (
            next((r for r in config.repositories if r.repo_id == item["repo_id"]), None)
            if config
            else None
        )
        if (
            config is None
            or config.connection != self.remote.connection
            or repo is None
            or repo.scope != row[-1]
        ):
            raise BusError("Report repository binding is unavailable")
        return item

    def permitted(self, item: Delivery, status: str) -> bool:
        config = load_config()
        if config is None or config.connection != self.remote.connection:
            return False
        peer = config.peer(item["repo_id"], item["sender"].split("/", 1)[0])
        repo = next((r for r in config.repositories if r.repo_id == item["repo_id"]), None)
        with self.remote.store.transaction() as db:
            row = cast(
                "tuple[str, str] | None",
                db.execute(
                    "SELECT i.envelope,m.recipient_scope FROM remote_imports i "
                    "JOIN messages m ON m.id=i.local_id JOIN agents a ON a.address=m.recipient "
                    "WHERE i.id=? AND a.scope=m.recipient_scope",
                    (item["id"],),
                ).fetchone(),
            )
        return (
            peer is not None
            and status in peer.send_reports
            and repo is not None
            and row is not None
            and repo.scope == row[1]
            and json.loads(row[0]) == item
        )

    def enqueue(
        self, local_id: int, native: str, status: object, reason: object
    ) -> dict[str, object]:
        selected, why = validate_status(status, reason)
        item = self.imported(local_id, native)
        if not self.permitted(item, selected):
            raise BusError("Local capability policy: report sending is disabled")
        with self.remote.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row = cast(
                "tuple[str, int] | None",
                db.execute(
                    "SELECT payload,synced FROM remote_report_submissions WHERE id=? "
                    "ORDER BY expected_revision DESC LIMIT 1",
                    (item["id"],),
                ).fetchone(),
            )
            prior = cast("dict[str, object]", json.loads(row[0])) if row else None
            if prior and prior["status"] == selected and prior["reason"] == why:
                return prior
            if prior and (prior["status"] in TERMINAL or selected == "received"):
                raise BusError(TRANSITION_CONFLICT)
            expected = cast("int", prior["expected_revision"]) + 1 if prior else 0
            if expected >= MAX_REVISION:
                raise BusError("Work report revision limit reached")
            payload: dict[str, object] = {
                "message_id": item["id"],
                "expected_revision": expected,
                "status": selected,
                "reason": why,
            }
            db.execute(
                "INSERT INTO remote_report_submissions VALUES (?,?,?,0)",
                (item["id"], expected, json.dumps(payload)),
            )
        return payload

    def pending(self) -> list[tuple[int, dict[str, object]]]:
        with self.remote.store.transaction() as db:
            rows = cast(
                "list[tuple[int, str]]",
                db.execute(
                    "SELECT i.local_id,s.payload FROM remote_report_submissions s "
                    "JOIN remote_imports i ON i.id=s.id WHERE s.synced=0 AND s.expected_revision="
                    "(SELECT MIN(p.expected_revision) FROM remote_report_submissions p "
                    "WHERE p.id=s.id AND p.synced=0) ORDER BY i.local_id"
                ).fetchall(),
            )
        return [
            (local_id, cast("dict[str, object]", json.loads(payload))) for local_id, payload in rows
        ]

    def accepted(self, submission: dict[str, object], value: object) -> None:
        report = parse_report(value, cast("str", submission["message_id"]))
        if (
            report["revision"] != cast("int", submission["expected_revision"]) + 1
            or report["status"] != submission["status"]
            or report["reason"] != submission["reason"]
        ):
            raise BusError("Report acceptance does not match durable submission")
        with self.remote.store.transaction() as db:
            db.execute(
                "UPDATE remote_report_submissions SET synced=1 WHERE id=? AND payload=?",
                (report["message_id"], json.dumps(submission)),
            )

    def snapshot(self, remote_id: str) -> Report | None:
        with self.remote.store.transaction() as db:
            row = cast(
                "tuple[str] | None",
                db.execute(
                    "SELECT payload FROM remote_reports WHERE id=?", (remote_id,)
                ).fetchone(),
            )
        return parse_report(cast("object", json.loads(row[0])), remote_id) if row else None

    def receive(self, envelope: Envelope, value: object, config: Config) -> None:
        report = parse_report(value, envelope["id"])
        with self.remote.store.transaction() as db:
            binding = cast(
                "tuple[str, str | None] | None",
                db.execute(
                    "SELECT o.envelope,m.sender_scope FROM remote_outbox o "
                    "JOIN messages m ON m.id=o.local_id WHERE o.id=?",
                    (envelope["id"],),
                ).fetchone(),
            )
        repo = next((r for r in config.repositories if r.repo_id == envelope["repo_id"]), None)
        if (
            binding is None
            or json.loads(binding[0]) != envelope
            or repo is None
            or binding[1] != repo.scope
            or config.connection != self.remote.connection
        ):
            raise BusError("Work report message binding conflicts")
        peer = config.peer(envelope["repo_id"], envelope["recipient"].split("/", 1)[0])
        if (
            peer is None
            or not peer.receive_reports
            or (report["status"] is not None and report["status"] not in peer.receive_reports)
        ):
            raise BusError("Local capability policy: report receiving is disabled")
        with self.remote.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row = cast(
                "tuple[str] | None",
                db.execute(
                    "SELECT payload FROM remote_reports WHERE id=?", (envelope["id"],)
                ).fetchone(),
            )
            previous = (
                parse_report(cast("object", json.loads(row[0])), envelope["id"]) if row else None
            )
            if previous and report != previous:
                stale = report["revision"] <= previous["revision"]
                invalid_transition = previous["status"] in TERMINAL or (
                    report["status"] == "received" and previous["status"] not in (None, "received")
                )
                if stale or invalid_transition:
                    raise BusError("Work report revision conflict")
            db.execute(
                "INSERT INTO remote_reports VALUES (?,?) ON CONFLICT(id) "
                "DO UPDATE SET payload=excluded.payload",
                (envelope["id"], json.dumps(report)),
            )

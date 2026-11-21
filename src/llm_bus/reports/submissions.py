"""Submissions ownership."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from llm_bus.errors import BusError
from llm_bus.hosts.sessions import is_subagent
from llm_bus.mailbox.eligibility import cutoff, report_open
from llm_bus.policy.config import load_config
from llm_bus.reports.lifecycle import TRANSITION_CONFLICT, transition_conflicts
from llm_bus.reports.protocol import MAX_REVISION, parse_report, validate_status

if TYPE_CHECKING:
    from collections.abc import Iterator

    from llm_bus.remote.models import Delivery


if TYPE_CHECKING:
    from llm_bus.reports.store import Reports


def imported(self: Reports, local_id: int, native: str | None = None) -> Delivery:
    with self.remote.store.transaction() as db:
        row = cast(
            "tuple[str, str, str] | None",
            db.execute(
                "SELECT r.envelope,m.recipient,m.recipient_scope FROM remote_imports r "
                "JOIN messages m ON m.id=r.local_id JOIN agents a ON a.address=m.recipient "
                "WHERE r.local_id=? AND a.scope=m.recipient_scope AND m.retention_at>?",
                (local_id, cutoff()),
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


def permitted(self: Reports, item: Delivery, status: str) -> bool:
    if is_subagent(item["recipient"].rsplit("/", 1)[-1]):
        return False
    config = load_config()
    if config is None or config.connection != self.remote.connection:
        return False
    peer = config.peer(item["repo_id"], item["sender"].split("/", 1)[0])
    repo = next((r for r in config.repositories if r.repo_id == item["repo_id"]), None)
    with self.remote.store.transaction() as db:
        if not report_open(db, item["id"]):
            return False
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
    self: Reports, local_id: int, native: str, status: object, reason: object
) -> dict[str, object]:
    selected, why = validate_status(status, reason)
    item = self.imported(local_id, native)
    if not self.permitted(item, selected):
        raise BusError("Local capability policy: report sending is disabled")
    with self.remote.store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        if not report_open(db, item["id"]):
            raise BusError("Work report message is unavailable")
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
        if transition_conflicts(cast("str | None", prior["status"]) if prior else None, selected):
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


def pending(self: Reports, *, after: int = 0) -> list[tuple[int, dict[str, object]]]:
    with self.remote.store.transaction() as db:
        rows = cast(
            "list[tuple[int, str]]",
            db.execute(
                "SELECT i.local_id,s.payload FROM remote_report_submissions s "
                "JOIN remote_imports i ON i.id=s.id JOIN messages m ON m.id=i.local_id "
                "WHERE s.synced=0 AND i.local_id>? AND m.retention_at>? "
                "AND s.expected_revision="
                "(SELECT MIN(p.expected_revision) FROM remote_report_submissions p "
                "WHERE p.id=s.id AND p.synced=0) ORDER BY i.local_id LIMIT 100",
                (after, cutoff()),
            ).fetchall(),
        )
    return [
        (local_id, cast("dict[str, object]", json.loads(payload))) for local_id, payload in rows
    ]


def pending_items(self: Reports) -> Iterator[tuple[int, dict[str, object]]]:
    after = 0
    while page := self.pending(after=after):
        after = page[-1][0]
        yield from page


def accepted(self: Reports, submission: dict[str, object], value: object) -> None:
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

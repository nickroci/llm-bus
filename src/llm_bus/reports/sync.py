"""Sync ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from llm_bus.errors import BusError
from llm_bus.mailbox.eligibility import report_open
from llm_bus.remote import batches, incoming
from llm_bus.remote.http import HTTP_POST
from llm_bus.reports.store import Reports

if TYPE_CHECKING:
    from llm_bus.remote.bridge import Bridge
    from llm_bus.remote.models import Envelope


def submit_reports(self: Bridge) -> int:
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
            if not incoming.receipt(self, item["id"]):
                continue
            if reports.permitted(item, status):
                reports.accepted(
                    submission, self.relay.request(HTTP_POST, "/v1/reports", submission)
                )
                self.record("report_submit", key)
        except BusError as error:
            self.record("report_submit", key, error)
            failures += 1
    return failures


def poll_reports(self: Bridge) -> int:
    reports = Reports(self.store, self.config.connection)
    failures = 0
    for page in reports.pollable_pages(now=self.clock()):
        due = [
            envelope
            for envelope in page
            if permits_report(self, envelope)
            and self.remote.work_due(
                "report_read", envelope["id"], self.clock(), force=self.refresh_status
            )
        ]
        failures += batches.poll_page(self, "report_read", due)
    return failures


def permits_report(self: Bridge, envelope: Envelope) -> bool:
    with self.store.transaction() as db:
        if not report_open(db, envelope["id"], now=self.clock()):
            return False
    current = self.current_config()
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

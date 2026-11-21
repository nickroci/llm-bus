"""Incoming ownership."""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING

from llm_bus.coordination.models import Availability, Direction
from llm_bus.errors import BusError
from llm_bus.mailbox.eligibility import remote_retained
from llm_bus.policy.fields import array_values, object_fields
from llm_bus.remote.http import HTTP_GET, HTTP_POST
from llm_bus.remote.outgoing import supports_availability, supports_coordination
from llm_bus.remote.protocol import parse_approval, parse_delivery

if TYPE_CHECKING:
    from llm_bus.policy.config import Repository
    from llm_bus.remote.bridge import Bridge


def sync_acknowledgements(self: Bridge) -> tuple[int, int]:
    acknowledged = failures = 0
    for remote_id, _repo_id in self.remote.acknowledgements():
        if self.remote.work_due("ack", remote_id, self.clock()) and self.remote.permits_ack(
            remote_id
        ):
            try:
                self.relay.request(HTTP_POST, "/v1/ack", {"id": remote_id})
                self.remote.mark_acknowledged(remote_id)
                self.record("ack", remote_id)
                acknowledged += 1
            except BusError as error:
                self.record("ack", remote_id, error)
                failures += 1
    return acknowledged, failures


def receive(self: Bridge, repo: Repository) -> int:
    imported = receive_route(self, repo, "/v1/inbox", coordination=False)
    if supports_coordination(self):
        imported += receive_route(self, repo, "/v2/coordination/inbox", coordination=True)
    if supports_availability(self):
        imported += receive_route(
            self, repo, "/v2/coordination/availability/inbox", coordination=False, availability=True
        )
    return imported


def well_formed(data: dict[str, object], *, availability: bool) -> bool:
    """A typed row's metadata, and any approval it carries, must parse."""
    try:
        if availability:
            Availability.parse(data.get("availability"))
        else:
            direction = Direction.parse(data.get("coordination"))
            if "approval" in data:
                parse_approval(data["approval"], direction.operation)
    except BusError:
        return False
    return True


def receive_route(
    self: Bridge, repo: Repository, route: str, *, coordination: bool, availability: bool = False
) -> int:
    # pylint: disable=too-many-locals
    # The cursor is scoped to one scan. Persisting it could skip late Queue jobs.
    after = 0
    imported = 0
    while True:
        values = array_values(
            self.relay.request(
                HTTP_GET,
                route + "?" + urllib.parse.urlencode({"repo_id": repo.repo_id, "after": after}),
            )
        )
        if not values:
            return imported
        for value in values:
            data = object_fields(value)
            sequence = data.get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= after:
                raise BusError("Remote mailbox cursor did not advance")
            after = sequence
            if (
                ("coordination" in data) != coordination
                or ("availability" in data) != availability
                or ("approval" in data and not coordination)
            ):
                raise BusError("Remote mailbox coordination class conflicts")
            # Invalid versions stay unreceipted, but cannot hide later rows.
            if (coordination or availability) and not well_formed(data, availability=availability):
                continue
            item = parse_delivery(value, repo, self.config.machine_id)
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
                receipt(self, item["id"])


def receipt(self: Bridge, key: str) -> bool:
    with self.store.transaction() as db:
        if not remote_retained(db, key):
            return False
    if not self.remote.work_due("receipt", key, self.clock()):
        return False
    try:
        self.relay.request(HTTP_POST, "/v1/receipts", {"id": key})
    except BusError as error:
        self.record("receipt", key, error)
        raise
    self.record("receipt", key)
    return True

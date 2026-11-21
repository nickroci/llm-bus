"""Protocol ownership."""

from __future__ import annotations

from dataclasses import asdict

from llm_bus.coordination.models import Approval, Availability, Direction
from llm_bus.errors import BusError
from llm_bus.mailbox.labels import validate_label
from llm_bus.mailbox.models import MAX_MESSAGE_BYTES
from llm_bus.policy.config import NATIVE, Repository
from llm_bus.policy.fields import IDENTIFIER, identifier, object_fields, required_text
from llm_bus.remote.models import Delivery, Peer, validate_reply_uuid
from llm_bus.reports.protocol import report_deadline

ADDRESS_PARTS = 3


TRANSPORT_STATES = frozenset({"accepted_remote", "delivered_local", "acknowledged", "expired"})


def parse_peer(value: object, repo_id: str, machine_id: str) -> Peer:
    data = object_fields(value)
    machine = identifier(data.get("machine_id"))
    native = required_text(data.get("native_address"))
    peer_address = required_text(data.get("address"))
    if (
        data.get("repo_id") != repo_id
        or machine == machine_id
        or not NATIVE.fullmatch(native)
        or peer_address != f"{machine}/{repo_id}/{native}"
    ):
        raise BusError("Invalid remote session identity")
    label = data.get("label")
    if label is not None:
        label, _ = validate_label(required_text(label))
    revision = data.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise BusError("Invalid remote session revision")
    return Peer(
        label=label,
        address=peer_address,
        machine_id=machine,
        repo_id=repo_id,
        native_address=native,
        last_seen=required_text(data.get("last_seen")),
        ended=0,
    )


def parse_delivery(value: object, repo: Repository, machine: str) -> Delivery:
    data = object_fields(value)
    recipient = required_text(data.get("recipient"))
    sender = required_text(data.get("sender"))
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
    body = required_text(data.get("body"))
    if len(body.encode()) > MAX_MESSAGE_BYTES:
        raise BusError("Remote message exceeds 64 KiB")
    result = Delivery(
        id=required_text(data.get("id")),
        sender=sender,
        recipient=recipient,
        repo_id=repo.repo_id,
        body=body,
        sequence=sequence,
        sent_at=required_text(data.get("sent_at")),
    )
    if "reply_to" in data:
        parent = required_text(data["reply_to"])
        validate_reply_uuid(parent)
        result["reply_to"] = parent
    _typed_classes(result, data)
    return result


def _typed_classes(result: Delivery, data: dict[str, object]) -> None:
    """Copy the one typed metadata class a delivery may carry, validated."""
    # pylint: disable=clean-code-output-argument-mutation
    if "coordination" in data and "availability" in data:
        raise BusError("Conflicting remote message classes")
    if "availability" in data:
        result["availability"] = asdict(Availability.parse(data["availability"]))
    if "coordination" in data:
        result["coordination"] = Direction.parse(data["coordination"]).wire()
    if "approval" in data:
        result["approval"] = parse_approval(
            data["approval"], Direction.parse(data.get("coordination")).operation
        ).wire()


def parse_approval(value: object, operation: str) -> Approval:
    """Approvals travel only with gate operations and never carry a local gate record."""
    approval = Approval.parse(value, operation)
    if approval.record_id is not None:
        raise BusError("Gate records stay local; remote approvals carry no record_id")
    return approval


def validate_status(key: str, value: object, *, require_id: bool = False) -> dict[str, object]:
    data = object_fields(value)
    status = data.get("status")
    if not isinstance(status, str) or status not in TRANSPORT_STATES:
        raise BusError("Invalid remote message status")
    if (require_id or "id" in data) and data.get("id") != key:
        raise BusError("Remote status message ID conflicts")
    if "report_expires_at" in data:
        report_deadline(data["report_expires_at"])
    if "retention_expires_at" in data:
        report_deadline(data["retention_expires_at"])
    return data

"""Coordinator-issued gate approvals sent as ordinary verified directions."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from llm_bus.cli.commands import send
from llm_bus.coordination.models import MAX_LIFETIME, Approval
from llm_bus.errors import BusError

if TYPE_CHECKING:
    from llm_bus.mailbox.models import SenderContext
    from llm_bus.mailbox.store import Store

UNITS = {"m": "minutes", "h": "hours"}


def parse_duration(text: str) -> timedelta:
    match = re.fullmatch(r"([1-9]\d{0,3})([mh])", text)
    if match is None:
        raise BusError("Expected --expires-in like 30m or 2h")
    result = timedelta(**{UNITS[match[2]]: int(match[1])})
    if result > MAX_LIFETIME:
        raise BusError("Approval lifetime is at most 24h")
    return result


def approve(store: Store, args: dict[str, object], sender: SenderContext) -> object:
    """Send one gate approval to a recipient; the receiver reverifies it at every exposure."""
    gate = str(args["gate"])
    pr = cast("int | None", args.get("pr"))
    head_sha = cast("str | None", args.get("head_sha"))
    expires = datetime.now(UTC) + parse_duration(str(args.get("expires_in") or "2h"))
    approval = Approval(
        ticket=str(args["ticket"]),
        branch=str(args["branch"]),
        base=str(args["base"]),
        expires_at=expires.isoformat(timespec="seconds"),
        pr=pr,
        head_sha=head_sha,
    )
    body = (
        f"Approval {gate}: ticket {approval.ticket}, branch {approval.branch} "
        f"into {approval.base}"
        + (f", PR {pr}" if pr else "")
        + (f", head {head_sha}" if head_sha else "")
        + ". Verify live evidence before acting."
    )
    return send(
        store,
        {
            "to": args["recipient"],
            "body": body,
            "project": args["project"],
            "direction": "approve-" + gate,
            "reply_to": None,
            "cross_folder": False,
            "format": args["format"],
            "run_id": args.get("run_id"),
        },
        sender,
        approval=approval,
    )

"""Monitor ownership."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from llm_bus.errors import BusError
from llm_bus.mailbox.eligibility import cutoff
from llm_bus.mailbox.models import MAX_BATCH
from llm_bus.routing import routing_scope

if TYPE_CHECKING:
    from llm_bus.mailbox.store import Store
    from llm_bus.remote.models import Delivery


def monitor_history(
    store: Store, *, repository: str | None, before: int | None = None, limit: int = 100
) -> list[tuple[int, str, str, Delivery]]:
    """Read retained monitoring without requiring current configuration or a host identity."""
    if not 1 <= limit <= MAX_BATCH:
        raise BusError("Monitor limit must be between 1 and 100")
    if before is not None and before < 1:
        raise BusError("Monitor cursor must be a positive ID")
    clauses: list[str] = ["retention_at>?"]
    parameters: list[str | int] = [cutoff()]
    if repository is not None:
        clauses.append("scope=?")
        parameters.append(routing_scope(repository))
    if before is not None:
        clauses.append("monitor_id<?")
        parameters.append(before)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    parameters.append(limit)
    with store.transaction() as db:
        rows = cast(
            "list[tuple[int, str, str, str]]",
            db.execute(
                "SELECT monitor_id,scope,connection,envelope FROM remote_monitor"  # noqa: S608
                + where
                + " ORDER BY monitor_id DESC LIMIT ?",
                parameters,
            ).fetchall(),
        )
    return [
        (key, scope, connection, cast("Delivery", json.loads(payload)))
        for key, scope, connection, payload in rows
    ]

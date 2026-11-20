"""Per-generation project delegation and per-grant worker bindings."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from llm_bus.coordination.models import GATE_OPERATIONS, branch_name, timestamp
from llm_bus.errors import BusError

if TYPE_CHECKING:
    from sqlite3 import Connection

GATES = tuple(GATE_OPERATIONS.values())
FLOOR = (
    "force-push",
    "production changes",
    "credential or secret changes",
    "merge with failing or pending CI",
)
FLOOR_SPECS = (*FLOOR, "deploys or production changes", "paid or live runs")
AUTO_MERGE = "auto-merge"
OWNER_DECISIONS = "owner-decisions"
DECISION_DEFAULTS = "decision-defaults"
PROJECT_EXECUTION = "project-execution"
FLAG_WORDS = frozenset({AUTO_MERGE, OWNER_DECISIONS, DECISION_DEFAULTS, PROJECT_EXECUTION})
MERGE_GATES = frozenset({"merge", "merge-admin"})
MAX_TARGETS = 8
MAX_DEFAULT_MINUTES = 1440
VERSION = 1
SPEC_PARTS = 2


def _words(text: str) -> str:
    return re.sub(r"[\s_-]+", " ", text.strip().lower())


def _reject_floor(spec: str) -> None:
    normalized = _words(spec)
    for entry in FLOOR_SPECS:
        if normalized.startswith(_words(entry)):
            raise BusError(f"Delegation never covers the floor action: {entry}")


def _stored_minutes(value: object) -> int | None:
    """Strictly typed decision default minutes; a bool is not a minute count."""
    if value is None:
        return None
    # pylint: disable=unidiomatic-typecheck
    if type(value) is not int or not 1 <= value <= MAX_DEFAULT_MINUTES:
        raise BusError("Invalid delegation flag")
    return value


def _stored_project_execution(fields: dict[str, object]) -> bool:
    if "project_execution" not in fields:
        return False
    value = fields["project_execution"]
    if not isinstance(value, bool):
        raise BusError("Invalid delegation flag")
    return value


@dataclass(frozen=True)
class Delegation:
    """Gate to explicit target branches, never empty per gate, plus optional flags.

    A delegation is valid with at least one item: a gate or a flag. ``owner_decisions``
    and ``project_execution`` may stand without a gate.
    """

    gates: tuple[tuple[str, tuple[str, ...]], ...]
    auto_merge: bool = False
    owner_decisions: bool = False
    decision_default_minutes: int | None = None
    project_execution: bool = False

    @classmethod
    def _build(
        cls,
        mapping: dict[str, set[str]],
        *,
        auto_merge: bool = False,
        owner_decisions: bool = False,
        decision_default_minutes: int | None = None,
        project_execution: bool = False,
    ) -> Delegation:
        return cls(
            tuple((gate, tuple(sorted(mapping[gate]))) for gate in sorted(mapping)),
            auto_merge=auto_merge,
            owner_decisions=owner_decisions,
            decision_default_minutes=decision_default_minutes,
            project_execution=project_execution,
        )

    def targets(self, gate: str) -> tuple[str, ...]:
        return dict(self.gates).get(gate, ())

    def all_targets(self) -> frozenset[str]:
        return frozenset(branch for _gate, branches in self.gates for branch in branches)

    def intersect(self, other: Delegation) -> Delegation:
        common = {gate: set(branches) & set(other.targets(gate)) for gate, branches in self.gates}
        return self._build(
            {gate: branches for gate, branches in common.items() if branches},
            auto_merge=self.auto_merge and other.auto_merge,
            owner_decisions=self.owner_decisions and other.owner_decisions,
            project_execution=self.project_execution and other.project_execution,
            decision_default_minutes=(
                self.decision_default_minutes if other.decision_default_minutes else None
            ),
        )

    def only_gates(self, gates: tuple[str, ...]) -> Delegation:
        """Keep only the listed gates (a gate-level cap); flags never survive the cap."""
        return self._build({gate: set(branches) for gate, branches in self.gates if gate in gates})

    def covers(self, other: Delegation) -> bool:
        """Gate subset and every flag of ``other`` set here; minutes count by presence only."""
        return (
            all(set(branches) <= set(self.targets(gate)) for gate, branches in other.gates)
            and (self.auto_merge or not other.auto_merge)
            and (self.owner_decisions or not other.owner_decisions)
            and (self.project_execution or not other.project_execution)
            and (
                self.decision_default_minutes is not None or other.decision_default_minutes is None
            )
        )

    def items(self) -> tuple[str, ...]:
        gates = tuple(f"{gate}:{branch}" for gate, branches in self.gates for branch in branches)
        return (
            gates
            + ((AUTO_MERGE,) if self.auto_merge else ())
            + ((OWNER_DECISIONS,) if self.owner_decisions else ())
            + ((DECISION_DEFAULTS,) if self.decision_default_minutes is not None else ())
            + ((PROJECT_EXECUTION,) if self.project_execution else ())
        )

    def wire(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "version": VERSION,
            "gates": {gate: list(branches) for gate, branches in self.gates},
        }
        if self.auto_merge:
            fields["auto_merge"] = True
        if self.owner_decisions:
            fields["owner_decisions"] = True
        if self.project_execution:
            fields["project_execution"] = True
        if self.decision_default_minutes is not None:
            fields["decision_default_minutes"] = self.decision_default_minutes
        return fields

    @classmethod
    def parse(cls, value: object) -> Delegation:
        if not isinstance(value, dict):
            raise BusError("Invalid delegation fields")
        fields = cast("dict[str, object]", value)
        known = {
            "version",
            "gates",
            "auto_merge",
            "owner_decisions",
            "decision_default_minutes",
            "project_execution",
        }
        if not {"version", "gates"} <= set(fields) <= known:
            raise BusError("Invalid delegation fields")
        gates = fields["gates"]
        flags = {name: fields.get(name) for name in ("auto_merge", "owner_decisions")}
        if any(value is not None and not isinstance(value, bool) for value in flags.values()):
            raise BusError("Invalid delegation flag")
        project_execution = _stored_project_execution(fields)
        minutes = _stored_minutes(fields.get("decision_default_minutes"))
        # bool subclasses int but is not a valid protocol version.
        # pylint: disable=unidiomatic-typecheck
        if type(fields["version"]) is not int or fields["version"] != VERSION:
            raise BusError("Invalid delegation version")
        if not isinstance(gates, dict):
            raise BusError("Invalid delegation gates")
        mapping: dict[str, set[str]] = {}
        for gate, branches in cast("dict[str, object]", gates).items():
            if gate not in GATES or not isinstance(branches, list):
                raise BusError("Invalid delegation gate")
            listed = cast("list[object]", branches)
            if not 0 < len(listed) <= MAX_TARGETS:
                raise BusError("Invalid delegation gate")
            mapping[gate] = {branch_name(branch) for branch in listed}
        parsed = cls._build(
            mapping,
            auto_merge=flags["auto_merge"] is True,
            owner_decisions=flags["owner_decisions"] is True,
            decision_default_minutes=minutes,
            project_execution=project_execution,
        )
        if not parsed.items():
            raise BusError("Invalid delegation gates")
        return parsed

    @classmethod
    def load(cls, text: str) -> Delegation:
        return cls.parse(cast("object", json.loads(text)))

    @classmethod
    def from_specs(
        cls,
        specs: list[str],
        *,
        auto_merge: bool = False,
        owner_decisions: bool = False,
        decision_default_minutes: int | None = None,
        project_execution: bool = False,
    ) -> Delegation:
        mapping: dict[str, set[str]] = {}
        for spec in specs:
            _reject_floor(spec)
            parts = spec.split(":", 1)
            if len(parts) != SPEC_PARTS or parts[0] not in GATES:
                raise BusError(f"Expected GATE:BRANCH with gate in {', '.join(GATES)}: {spec!r}")
            mapping.setdefault(parts[0], set()).add(branch_name(parts[1]))
        if auto_merge and not MERGE_GATES & set(mapping):
            raise BusError("auto-merge needs a merge or merge-admin gate")
        if decision_default_minutes is not None:
            if not owner_decisions:
                raise BusError("decision-defaults needs owner-decisions")
            if not 1 <= decision_default_minutes <= MAX_DEFAULT_MINUTES:
                raise BusError(f"Decision default minutes must be 1 to {MAX_DEFAULT_MINUTES}")
        if not mapping and not owner_decisions and not project_execution:
            raise BusError(
                "Delegation needs at least one GATE:BRANCH or --owner-decisions "
                "or --project-execution"
            )
        if any(len(branches) > MAX_TARGETS for branches in mapping.values()):
            raise BusError(f"At most {MAX_TARGETS} target branches per gate")
        return cls._build(
            mapping,
            auto_merge=auto_merge,
            owner_decisions=owner_decisions,
            decision_default_minutes=decision_default_minutes,
            project_execution=project_execution,
        )

    def narrow(self, items: list[str]) -> Delegation:
        """Keep exactly the listed items; each must already be in this delegation."""
        outside = [item for item in items if item not in self.items()]
        if outside:
            raise BusError(f"Not delegated by the project: {', '.join(outside)}")
        return Delegation.from_specs(
            [item for item in items if item not in FLAG_WORDS],
            auto_merge=AUTO_MERGE in items,
            owner_decisions=OWNER_DECISIONS in items,
            project_execution=PROJECT_EXECUTION in items,
            decision_default_minutes=(
                self.decision_default_minutes if DECISION_DEFAULTS in items else None
            ),
        )


def project_delegation(db: Connection, record: dict[str, object]) -> Delegation | None:
    row = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT delegation_json FROM coordination_delegations "
            "WHERE scope=? AND project_id=? AND generation=?",
            (record["scope"], record["project_id"], record["generation"]),
        ).fetchone(),
    )
    return None if row is None else Delegation.load(row[0])


def _generation_bindings(db: Connection, record: dict[str, object]) -> list[tuple[str, Delegation]]:
    rows = cast(
        "list[tuple[str, str]]",
        db.execute(
            "SELECT d.grant_id,d.delegation_json FROM coordination_grant_delegations d "
            "JOIN coordination_grants g ON g.grant_id=d.grant_id "
            "WHERE g.scope=? AND g.project_id=? AND g.generation=? ORDER BY g.rowid",
            (record["scope"], record["project_id"], record["generation"]),
        ).fetchall(),
    )
    return [(grant_id, Delegation.load(text)) for grant_id, text in rows]


def set_project_delegation(
    db: Connection, record: dict[str, object], delegation: Delegation | None, by: str
) -> None:
    """Replace the generation's delegation; bindings never outlive the project set."""
    key = (record["scope"], record["project_id"], record["generation"])
    if delegation is None:
        db.execute(
            "DELETE FROM coordination_delegations WHERE scope=? AND project_id=? AND generation=?",
            key,
        )
    else:
        db.execute(
            "INSERT OR REPLACE INTO coordination_delegations VALUES (?,?,?,?,?,?)",
            (*key, json.dumps(delegation.wire()), by, datetime.now(UTC).isoformat()),
        )
    # A later widening must need a rejoin, so stale binding items are dropped now.
    for grant_id, binding in _generation_bindings(db, record):
        kept = None if delegation is None else binding.intersect(delegation)
        if kept is None or not kept.items():
            db.execute("DELETE FROM coordination_grant_delegations WHERE grant_id=?", (grant_id,))
        elif kept != binding:
            db.execute(
                "UPDATE coordination_grant_delegations SET delegation_json=? WHERE grant_id=?",
                (json.dumps(kept.wire()), grant_id),
            )


def _binding(db: Connection, grant_id: str) -> Delegation | None:
    row = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT delegation_json FROM coordination_grant_delegations WHERE grant_id=?",
            (grant_id,),
        ).fetchone(),
    )
    return None if row is None else Delegation.load(row[0])


def bind(
    db: Connection, record: dict[str, object], grant_id: str, narrow: list[str] | None
) -> Delegation | None:
    """Replace the grant's binding; a rejoin without --narrow accepts the full project set."""
    project = project_delegation(db, record)
    if project is None:
        if narrow:
            raise BusError("--narrow requires a project delegation")
        return None
    binding = project.narrow(narrow) if narrow else project
    db.execute(
        "INSERT OR REPLACE INTO coordination_grant_delegations VALUES (?,?,?)",
        (grant_id, json.dumps(binding.wire()), datetime.now(UTC).isoformat()),
    )
    return binding


def _grant_current(db: Connection, record: dict[str, object], grant_id: str, now: datetime) -> bool:
    """True while the grant is this generation's, unrevoked, unexpired, consented and enrolled."""
    row = cast(
        "tuple[str | None] | None",
        db.execute(
            "SELECT g.expires_at FROM coordination_grants g "
            "JOIN coordination_enrollments e ON e.scope=g.scope AND e.project_id=g.project_id "
            "AND e.address=g.recipient AND e.generation=g.generation "
            "LEFT JOIN coordination_session_consents c ON c.grant_id=g.grant_id "
            "WHERE g.grant_id=? AND g.scope=? AND g.project_id=? AND g.generation=? "
            "AND g.revoked_at IS NULL AND c.ended_at IS NULL",
            (grant_id, record["scope"], record["project_id"], record["generation"]),
        ).fetchone(),
    )
    if row is None:
        return False
    return row[0] is None or timestamp(row[0]) > now


def effective(
    db: Connection, record: dict[str, object], grant_id: str, *, now: datetime
) -> Delegation | None:
    """Project delegation intersected with the worker's binding.

    None means no authority: no delegation or binding, or the grant is not current (other
    generation, revoked, expired at ``now``, session consent ended, or worker not enrolled).
    """
    project = project_delegation(db, record)
    binding = _binding(db, grant_id)
    if project is None or binding is None or not _grant_current(db, record, grant_id, now):
        return None
    return project.intersect(binding)


def bindings(db: Connection, record: dict[str, object]) -> dict[str, list[str]]:
    return {
        grant_id: list(binding.items()) for grant_id, binding in _generation_bindings(db, record)
    }


def rejoin_required(db: Connection, record: dict[str, object]) -> list[str]:
    """Enrolled workers whose current binding lacks project items; they must rejoin."""
    project = project_delegation(db, record)
    if project is None:
        return []
    rows = cast(
        "list[tuple[str, str | None]]",
        db.execute(
            "SELECT e.address,d.delegation_json FROM coordination_enrollments e "
            "LEFT JOIN coordination_grants g ON g.rowid=(SELECT MAX(rowid) "
            "FROM coordination_grants WHERE scope=e.scope AND project_id=e.project_id "
            "AND recipient=e.address AND generation=? AND revoked_at IS NULL) "
            "LEFT JOIN coordination_grant_delegations d ON d.grant_id=g.grant_id "
            "WHERE e.scope=? AND e.project_id=? AND e.role='worker' ORDER BY e.address",
            (record["generation"], record["scope"], record["project_id"]),
        ).fetchall(),
    )
    return [
        address
        for address, text in rows
        if text is None or not Delegation.load(text).covers(project)
    ]

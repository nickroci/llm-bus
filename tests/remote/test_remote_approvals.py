"""Remote delegated gates: sender and receiver both allow, default deny, no policy bypass."""

# Each scenario states both machines' owner commands and the exact rows it expects.
# pylint: disable=duplicate-code,protected-access,clean-code-commented-out-code,too-many-arguments
# pylint: disable=too-many-lines,too-many-locals

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast, final
from uuid import uuid4

import pytest

from llm_bus.cli import commands
from llm_bus.cli.parser import build_parser
from llm_bus.coordination import approvals
from llm_bus.coordination.authority import authorize
from llm_bus.coordination.delivery import verification
from llm_bus.coordination.models import Approval, Direction
from llm_bus.errors import BusError
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from llm_bus.policy.config import Config, load_config
from llm_bus.remote import batches
from llm_bus.remote.bridge import Bridge
from llm_bus.remote.http import RelayError
from llm_bus.remote.incoming import receive_route
from llm_bus.remote.outgoing import supports_approvals, upload
from llm_bus.remote.store import RemoteStore
from tests.coordination.test_enrollment import invoke

from .support import admit_delivery, delivery, setup_machine

if TYPE_CHECKING:
    from collections.abc import Mapping

    from llm_bus.remote.models import Delivery, Envelope

SHA = "a" * 40
COORDINATOR = "a/xyz/codex:sender"
WORKER = "b/xyz/claude:worker"
APPROVE_ARGV = (
    "coordination",
    "approve",
    "demo",
    "--recipient",
    WORKER,
    "--gate",
    "merge",
    "--ticket",
    "roam-22m",
    "--branch",
    "roam-22m-login",
    "--base",
    "develop",
    "--pr",
    "42",
    "--head-sha",
    SHA,
)
MERGE_DEVELOP = ("--delegate", "merge:develop")
CAPABILITIES = "/v2/coordination/approvals/capabilities"


@pytest.fixture(autouse=True)
def no_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    def unsupported(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "unsupported"}

    monkeypatch.setattr(commands, "dispatch_pending", unsupported)


def machine(tmp_path: Path, name: str, gates: tuple[str, ...] = ()) -> tuple[Store, Config]:
    """Write this machine's local policy (the config file is shared per test) and open its store."""
    store, _ = setup_machine(tmp_path, name)
    if gates:
        path = Path(os.environ["LLM_BUS_CONFIG"])
        listed = ", ".join(json.dumps(gate) for gate in gates)
        path.write_text(
            path.read_text(encoding="utf-8") + f"accept_delegated_gates = [{listed}]\n",
            encoding="utf-8",
        )
    config = load_config()
    assert config is not None
    (tmp_path / ".beads").mkdir(exist_ok=True)
    return store, config


def set_receive_mode(mode: str, *, keep_gates: bool = False) -> None:
    """Switch the peer's mode; a valid config holds gates only for ``inbox``, so drop them."""
    path = Path(os.environ["LLM_BUS_CONFIG"])
    text = path.read_text(encoding="utf-8").replace(
        'receive_messages = "inbox"', f'receive_messages = "{mode}"'
    )
    if not keep_gates:
        text = "".join(
            line
            for line in text.splitlines(keepends=True)
            if not line.startswith("accept_delegated_gates")
        )
    path.write_text(text, encoding="utf-8")


def part(envelope: Mapping[str, object], key: str) -> dict[str, object]:
    """One typed metadata object (coordination or approval) of an envelope."""
    return cast("dict[str, object]", envelope[key])


def references(tmp_path: Path) -> tuple[str, ...]:
    return (
        "--objective",
        "Build safely",
        "--beads-workspace",
        str(tmp_path),
        "--memory-ticket",
        "bus-1",
    )


@final
class Receiver:
    """Machine b: its owner imports the remote coordinator's project with its own delegation."""

    def __init__(
        self,
        tmp_path: Path,
        gates: tuple[str, ...] = ("merge",),
        delegate: tuple[str, ...] = MERGE_DEVELOP,
    ) -> None:
        self.path = tmp_path
        self.repo = tmp_path / "repo"
        self.store, self.config = machine(tmp_path, "b", gates)
        self.project_id = str(uuid4())
        self.generation = str(uuid4())
        self.store.register("claude", "worker", str(self.repo))
        self.setup(self.generation, delegate)

    def setup(self, generation: str, delegate: tuple[str, ...]) -> None:
        invoke(
            self.store,
            self.repo,
            "coordination",
            "setup",
            "demo",
            "--project-id",
            self.project_id,
            "--generation",
            generation,
            "--coordinator",
            COORDINATOR,
            *references(self.path),
            *delegate,
            address="codex:receiver",
        )

    def join(self, *narrow: str) -> None:
        invoke(self.store, self.repo, "join", "demo", *narrow, address="claude:worker")
        invoke(
            self.store,
            self.repo,
            "coordination",
            "claim",
            "demo",
            "--ticket",
            "roam-22m",
            "--branch",
            "roam-22m-login",
            address="claude:worker",
        )

    def owner(self, *argv: str) -> dict[str, object]:
        return invoke(self.store, self.repo, *argv, address="codex:receiver")

    def grant(self) -> str:
        with self.store.transaction() as db:
            return cast(
                "str",
                db.execute(
                    "SELECT grant_id FROM coordination_grants WHERE recipient='claude:worker' "
                    "ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0],
            )

    def item(
        self,
        operation: str = "approve-merge",
        *,
        generation: str | None = None,
        **approval: object,
    ) -> Delivery:
        fields: dict[str, object] = {
            "ticket": "roam-22m",
            "branch": "roam-22m-login",
            "base": "develop",
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds"),
            "pr": 42,
            "head_sha": SHA,
            **approval,
        }
        result = delivery()
        result["sender"] = COORDINATOR
        result["recipient"] = WORKER
        result["coordination"] = Direction(
            1, self.project_id, generation or self.generation, self.grant(), operation
        ).wire()
        result["approval"] = {key: value for key, value in fields.items() if value is not None}
        return result

    def admit(self, item: Delivery) -> tuple[str, int | None, bool] | None:
        return admit_delivery(self.store, self.config, item)

    def reason(self, item: Delivery) -> str:
        direction = Direction.parse(part(item, "coordination"))
        with self.store.transaction() as db:
            return authorize(
                db,
                sender=COORDINATOR,
                recipient="claude:worker",
                scope=self.config.repositories[0].scope,
                direction=direction,
                now=datetime.now(UTC),
                approval=Approval.parse(part(item, "approval"), direction.operation),
            ).reason

    def count(self, table: str) -> int:
        with self.store.transaction() as db:
            return cast("int", db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608

    def exposed(self) -> list[dict[str, object]]:
        """Direction metadata the worker can currently see in its inbox."""
        return [
            entry["coordination"]
            for entry in self.store.inbox("claude:worker")
            if "coordination" in entry
        ]

    def verdict(self, local_id: int) -> dict[str, object]:
        with self.store.transaction() as db:
            result = verification(db, local_id)
        assert result is not None
        return result


def admitted_inbox(receiver: Receiver, item: Delivery) -> int:
    result = receiver.admit(item)
    assert result is not None
    assert result[0] == "inbox"
    assert result[1] is not None
    return result[1]


def test_all_three_allow_admits_and_verifies(tmp_path: Path) -> None:
    receiver = Receiver(tmp_path)
    receiver.join()
    item = receiver.item()
    local_id = admitted_inbox(receiver, item)
    assert receiver.count("coordination_approvals") == 1
    exposed = receiver.exposed()
    assert [entry["verified"] for entry in exposed] == [True]
    assert part(exposed[0], "approval")["branch"] == "roam-22m-login"
    # Re-admitting the same delivery is idempotent.
    assert receiver.admit(item) == ("inbox", local_id, False)


@pytest.mark.parametrize("gates", [(), ("commit", "push", "merge-admin")])
def test_peer_cap_default_deny_and_gate_level(tmp_path: Path, gates: tuple[str, ...]) -> None:
    # An omitted key grants nothing; a cap without the gate grants nothing either.
    receiver = Receiver(tmp_path, gates=gates)
    receiver.join()
    item = receiver.item()
    assert receiver.admit(item) is None
    assert receiver.reason(item) == "gate_not_delegated"
    assert receiver.count("coordination_approvals") == 0
    assert receiver.exposed() == []


def test_receiver_project_without_delegation_denies(tmp_path: Path) -> None:
    receiver = Receiver(tmp_path, delegate=())
    receiver.join()
    item = receiver.item()
    assert receiver.admit(item) is None
    assert receiver.reason(item) == "gate_not_delegated"
    assert receiver.count("coordination_approvals") == 0


def test_narrowed_worker_binding_denies(tmp_path: Path) -> None:
    receiver = Receiver(tmp_path, delegate=(*MERGE_DEVELOP, "--delegate", "push:develop"))
    receiver.join("--narrow", "push:develop")
    item = receiver.item()
    assert receiver.admit(item) is None
    assert receiver.reason(item) == "gate_not_delegated"


def test_branch_that_is_a_project_target_is_denied_on_every_narrowing(tmp_path: Path) -> None:
    # R1: the work branch is compared with the whole project set, never a narrower binding.
    receiver = Receiver(tmp_path, delegate=(*MERGE_DEVELOP, "--delegate", "merge:release"))
    receiver.join("--narrow", "merge:develop")
    item = receiver.item(branch="release")
    assert receiver.reason(item) == "target_denied"
    assert receiver.admit(item) is None


def test_lifetime_cap_refuses_peer_expiry_beyond_24h(tmp_path: Path) -> None:
    receiver = Receiver(tmp_path)
    receiver.join()
    far = (datetime.now(UTC) + timedelta(hours=25)).isoformat(timespec="seconds")
    assert receiver.admit(receiver.item(expires_at=far)) is None
    assert receiver.count("coordination_approvals") == 0
    near = (datetime.now(UTC) + timedelta(hours=23)).isoformat(timespec="seconds")
    assert receiver.admit(receiver.item(expires_at=near)) is not None


def test_denied_peer_bypass_never_reaches_inbox_or_wake(tmp_path: Path) -> None:
    # A sender that skips its own client and crafts envelopes still meets this machine's policy.
    receiver = Receiver(tmp_path, gates=())
    receiver.join()
    for item in (
        receiver.item(),
        receiver.item("approve-commit", pr=None),
        receiver.item(base="main"),
    ):
        assert receiver.admit(item) is None
    set_receive_mode("deny")
    assert receiver.admit(receiver.item()) is None
    set_receive_mode("inbox")
    for table in ("coordination_approvals", "coordination_messages", "wake_dispatch"):
        assert receiver.count(table) == 0, table
    assert receiver.count("remote_imports") == 0
    assert receiver.count("remote_monitor") == 0


def test_monitor_mode_peer_never_reaches_inbox_or_wake(tmp_path: Path) -> None:
    receiver = Receiver(tmp_path)
    receiver.join()
    set_receive_mode("monitor")
    item = receiver.item()
    assert receiver.admit(item) == ("monitor", None, True)
    for table in ("coordination_approvals", "coordination_messages", "wake_dispatch"):
        assert receiver.count(table) == 0, table
    assert receiver.count("remote_monitor") == 1
    assert receiver.count("remote_imports") == 0
    assert receiver.exposed() == []


def test_crafted_envelopes_are_not_admitted(tmp_path: Path) -> None:
    receiver = Receiver(tmp_path)
    receiver.join()
    no_approval = receiver.item()
    del no_approval["approval"]
    non_gate = receiver.item("assign")
    record_bound = receiver.item(record_id="0" * 32)
    extra_key = receiver.item(unknown="x")
    for item in (no_approval, non_gate, record_bound, extra_key):
        before = receiver.count("messages")
        assert receiver.admit(item) is None
        assert receiver.count("messages") == before
    assert receiver.count("coordination_approvals") == 0
    # Monitor mode keeps a gate envelope without approval out of the inbox as well.
    set_receive_mode("monitor")
    assert receiver.admit(no_approval) == ("monitor", None, True)
    assert receiver.count("remote_imports") == 0
    assert receiver.count("coordination_messages") == 0


def test_exposure_rechecks_peer_cap_after_admission(tmp_path: Path) -> None:
    receiver = Receiver(tmp_path)
    receiver.join()
    local_id = admitted_inbox(receiver, receiver.item())
    assert receiver.verdict(local_id)["verified"] is True
    machine(tmp_path, "b")
    assert receiver.verdict(local_id)["reason"] == "gate_not_delegated"
    assert receiver.exposed() == []


@pytest.mark.parametrize("mode", ["deny", "monitor"])
def test_admitted_approval_stops_verifying_when_peer_leaves_inbox(
    tmp_path: Path, mode: str
) -> None:
    receiver = Receiver(tmp_path)
    receiver.join()
    local_id = admitted_inbox(receiver, receiver.item())
    assert receiver.verdict(local_id)["verified"] is True
    messages = receiver.count("messages")
    wakes = receiver.count("wake_dispatch")  # the admission itself requested one
    # The gate list stays in the file, so the config is the owner's stale edit, not a clean switch.
    set_receive_mode(mode, keep_gates=True)
    verdict = receiver.verdict(local_id)
    assert verdict["verified"] is False
    assert verdict["reason"] == "gate_not_delegated"
    assert receiver.exposed() == []
    # History is retained; nothing new wakes the worker.
    assert receiver.count("coordination_approvals") == 1
    assert receiver.count("messages") == messages
    assert receiver.count("wake_dispatch") == wakes


@pytest.mark.parametrize("mode", ["deny", "monitor"])
def test_peer_gate_list_needs_inbox_mode(tmp_path: Path, mode: str) -> None:
    Receiver(tmp_path)
    set_receive_mode(mode, keep_gates=True)
    with pytest.raises(BusError, match="inbox"):
        load_config()


def test_setup_delegate_records_the_local_owner_not_the_coordinator(tmp_path: Path) -> None:
    receiver = Receiver(tmp_path)
    with receiver.store.transaction() as db:
        by = cast(
            "str", db.execute("SELECT updated_by FROM coordination_delegations").fetchone()[0]
        )
    assert by == "codex:receiver"


def test_receiver_amend_after_replace_restores_and_narrows(tmp_path: Path) -> None:
    # M2: the receiver owner controls its own delegation over the project's life.
    receiver = Receiver(tmp_path)
    receiver.join()
    first = receiver.item()
    local_id = admitted_inbox(receiver, first)
    fresh = str(uuid4())
    receiver.owner(
        "coordination", "replace", "demo", "--generation", fresh, "--coordinator", COORDINATOR
    )
    receiver.generation = fresh
    receiver.join()
    after_replace = receiver.item()
    assert receiver.admit(after_replace) is None
    assert receiver.reason(after_replace) == "gate_not_delegated"
    receiver.owner("coordination", "amend", "demo", *MERGE_DEVELOP)
    receiver.join()
    verified = receiver.item()
    assert receiver.reason(verified) == "verified"
    assert receiver.admit(verified) is not None
    # Narrowing applies at once, also to an already admitted approval at exposure.
    receiver.owner("coordination", "amend", "demo", "--delegate", "push:develop")
    assert receiver.reason(receiver.item()) == "gate_not_delegated"
    assert receiver.verdict(local_id)["verified"] is False


def test_receiver_amend_on_remote_project_takes_gates_only(tmp_path: Path) -> None:
    receiver = Receiver(tmp_path)
    receiver.join()
    for flag in (
        ("--auto-merge",),
        ("--owner-decisions",),
        ("--objective", "Different goal"),
        ("--delegate", "merge:develop", "--auto-merge"),
    ):
        with pytest.raises(BusError, match="accepts only --delegate or --no-delegation"):
            receiver.owner("coordination", "amend", "demo", *flag)
    with receiver.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM coordination_amendments").fetchone()[0] == 0
    receiver.owner("coordination", "amend", "demo", "--no-delegation")
    assert receiver.reason(receiver.item()) == "gate_not_delegated"


def test_amend_on_local_project_still_needs_the_coordinator(tmp_path: Path) -> None:
    store = Store(tmp_path / "local.sqlite3")
    store.register("codex", "owner", str(tmp_path))
    store.register("codex", "other", str(tmp_path))
    (tmp_path / ".beads").mkdir()
    invoke(store, tmp_path, "coordinate", "demo", *references(tmp_path), *MERGE_DEVELOP)
    with pytest.raises(BusError, match="Only the enrolled coordinator"):
        invoke(
            store,
            tmp_path,
            "coordination",
            "amend",
            "demo",
            "--no-delegation",
            address="codex:other",
        )


def test_setup_delegate_stores_delegation_and_join_binds_it(tmp_path: Path) -> None:
    receiver = Receiver(tmp_path)
    receiver.join()
    with receiver.store.transaction() as db:
        stored = cast(
            "tuple[str]",
            db.execute("SELECT delegation_json FROM coordination_delegations").fetchone(),
        )
        bound = cast(
            "tuple[str]",
            db.execute("SELECT delegation_json FROM coordination_grant_delegations").fetchone(),
        )
    assert json.loads(stored[0])["gates"] == {"merge": ["develop"]}
    assert json.loads(bound[0])["gates"] == {"merge": ["develop"]}


def test_setup_delegate_rejects_flags_and_floor(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["coordination", "setup", "demo", "--auto-merge"]  # setup has no flag delegation
        )
    store, _config = machine(tmp_path, "b")
    store.register("claude", "worker", str(tmp_path / "repo"))
    with pytest.raises(BusError, match="floor"):
        invoke(
            store,
            tmp_path / "repo",
            "coordination",
            "setup",
            "demo",
            "--project-id",
            str(uuid4()),
            "--generation",
            str(uuid4()),
            "--coordinator",
            COORDINATOR,
            *references(tmp_path),
            "--delegate",
            "force-push:develop",
            address="codex:receiver",
        )


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ('["merge"]', True),
        ('["merge", "commit"]', True),
        ("[]", True),
        ('["merge", "merge"]', False),
        ('["deploy"]', False),
        ('"merge"', False),
    ],
)
def test_peer_policy_gate_list_is_strict(tmp_path: Path, value: str, *, valid: bool) -> None:
    machine(tmp_path, "b")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8") + f"accept_delegated_gates = {value}\n", encoding="utf-8"
    )
    if valid:
        config = load_config()
        assert config is not None
        assert config.peer("xyz", "a") is not None
    else:
        with pytest.raises(BusError):
            load_config()


def test_peer_policy_default_is_empty(tmp_path: Path) -> None:
    _store, config = machine(tmp_path, "b")
    peer = config.peer("xyz", "a")
    assert peer is not None
    assert peer.accept_delegated_gates == ()


# -- Coordinator machine (sender) ---------------------------------------------------------------


@final
class Sender:
    """Machine a: the coordinator owner's own project delegation gates what may leave."""

    def __init__(self, tmp_path: Path, delegate: tuple[str, ...] = MERGE_DEVELOP) -> None:
        self.path = tmp_path
        self.repo = tmp_path / "repo"
        self.store, self.config = machine(tmp_path, "a")
        self.store.register("codex", "sender", str(self.repo))
        invoke(
            self.store,
            self.repo,
            "coordinate",
            "demo",
            *references(tmp_path),
            *delegate,
            address="codex:sender",
        )
        invoke(
            self.store,
            self.repo,
            "coordination",
            "target",
            "demo",
            "--recipient",
            WORKER,
            "--grant-id",
            str(uuid4()),
            address="codex:sender",
        )
        self.remote = RemoteStore(self.store, self.config.connection)
        self.bridge = Bridge(self.store, self.config)

    def approve(self, *overrides: str) -> object:
        argv: list[str] = list(APPROVE_ARGV)
        for index in range(0, len(overrides), 2):
            argv[argv.index(overrides[index]) + 1] = overrides[index + 1]
        args = vars(build_parser().parse_args(argv))
        return approvals.approve(self.store, args, SenderContext("codex", "sender", str(self.repo)))

    def amend(self, *argv: str) -> None:
        invoke(
            self.store, self.repo, "coordination", "amend", "demo", *argv, address="codex:sender"
        )

    def queued(self) -> list[tuple[str, str]]:
        return [(envelope["id"], state) for envelope, state in self.remote.outgoing()]


def install_relay(
    monkeypatch: pytest.MonkeyPatch, *, approvals_supported: bool = True
) -> list[tuple[str, str]]:
    """Fake relay recording every request; nothing touches a network."""
    calls: list[tuple[str, str]] = []

    def request(_self: object, method: str, route: str, data: object = None) -> object:
        calls.append((method, route))
        if route == CAPABILITIES:
            if not approvals_supported:
                raise RelayError("not_found", http_status=404)
            return {"approval_version": 1}
        if route == "/v2/coordination/capabilities":
            return {"version": 1}
        if route == "/v2/coordination/messages":
            envelope = cast("dict[str, object]", data)
            return {"id": envelope["id"], "status": "accepted_remote"}
        if route.startswith("/v1/status/batch"):
            raise RelayError("not_found", http_status=404)
        if route.startswith("/v1/status?"):
            return {"id": route.split("id=")[1], "status": "delivered_local"}
        return {}

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    return calls


def uploads(calls: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [call for call in calls if call[0] == "POST" and call[1].endswith("/messages")]


def test_sender_enqueues_approval_in_envelope(tmp_path: Path) -> None:
    sender = Sender(tmp_path)
    result = cast("dict[str, object]", sender.approve())
    assert result["status"] == "queued_local"
    envelope = sender.remote.outgoing()[0][0]
    assert part(envelope, "approval")["base"] == "develop"
    assert "record_id" not in part(envelope, "approval")
    assert part(envelope, "coordination")["operation"] == "approve-merge"


@pytest.mark.parametrize(
    ("delegate", "overrides", "reason"),
    [
        (("--delegate", "push:develop"), (), "gate_not_delegated"),
        (MERGE_DEVELOP, ("--base", "main"), "target_denied"),
        (MERGE_DEVELOP, ("--branch", "develop"), "target_denied"),
        ((*MERGE_DEVELOP, "--delegate", "merge:release"), ("--branch", "release"), "target_denied"),
    ],
)
def test_sender_refuses_before_queueing(
    tmp_path: Path, delegate: tuple[str, ...], overrides: tuple[str, ...], reason: str
) -> None:
    sender = Sender(tmp_path, delegate)
    with pytest.raises(BusError, match=f"Direction denied: {reason}"):
        sender.approve(*overrides)
    assert sender.queued() == []
    with sender.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_sender_without_any_delegation_refuses(tmp_path: Path) -> None:
    sender = Sender(tmp_path)
    sender.amend("--no-delegation")
    with pytest.raises(BusError, match="Direction denied: gate_not_delegated"):
        sender.approve()
    assert sender.queued() == []


def test_sender_upload_checks_delegation_at_every_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = Sender(tmp_path)
    sender.approve()
    calls = install_relay(monkeypatch)
    sender.amend("--delegate", "push:develop")
    assert batches.sync_outgoing(sender.bridge) == (0, 0)
    assert calls == []
    assert [state for _id, state in sender.queued()] == ["queued_local"]
    sender.amend(*MERGE_DEVELOP)
    assert batches.sync_outgoing(sender.bridge)[1] == 0
    assert len(uploads(calls)) == 1
    assert [state for _id, state in sender.queued()] == ["accepted_remote"]


def test_replace_or_expiry_holds_queued_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = Sender(tmp_path)
    sender.approve()
    calls = install_relay(monkeypatch)
    envelope = sender.remote.outgoing()[0][0]
    part(envelope, "approval")["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat(
        timespec="seconds"
    )
    assert upload(sender.bridge, envelope) is False
    assert calls == []
    envelope = sender.remote.outgoing()[0][0]
    invoke(
        sender.store,
        sender.repo,
        "coordination",
        "replace",
        "demo",
        "--generation",
        str(uuid4()),
        "--coordinator",
        "codex:sender",
        address="codex:sender",
    )
    assert upload(sender.bridge, envelope) is False
    assert calls == []


def test_gate_envelope_without_valid_approval_is_never_uploaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = Sender(tmp_path)
    sender.approve()
    calls = install_relay(monkeypatch)
    envelope = sender.remote.outgoing()[0][0]
    stripped = {key: value for key, value in envelope.items() if key != "approval"}
    bound = {**envelope, "approval": {**part(envelope, "approval"), "record_id": "0" * 32}}
    stray = {**envelope, "coordination": {**part(envelope, "coordination"), "operation": "assign"}}
    for crafted in (stripped, bound, stray):
        assert upload(sender.bridge, cast("Envelope", cast("object", crafted))) is False
    assert calls == []


def test_uploaded_approval_still_receives_status_after_narrowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = Sender(tmp_path)
    sender.approve()
    calls = install_relay(monkeypatch)
    assert batches.sync_outgoing(sender.bridge)[1] == 0
    assert [state for _id, state in sender.queued()] == ["accepted_remote"]
    sender.amend("--no-delegation")
    envelope = sender.remote.outgoing()[0][0]
    assert batches.poll_page(sender.bridge, "status", [envelope]) == 0
    assert [state for _id, state in sender.queued()] == ["delivered_local"]
    assert len(uploads(calls)) == 1


def test_old_relay_leaves_approval_queued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sender = Sender(tmp_path)
    sender.approve()
    calls = install_relay(monkeypatch, approvals_supported=False)
    envelope = sender.remote.outgoing()[0][0]
    with pytest.raises(RelayError) as error:
        upload(sender.bridge, envelope)
    assert error.value.code == "unsupported_approvals"
    assert uploads(calls) == []
    assert not supports_approvals(sender.bridge)
    assert [state for _id, state in sender.queued()] == ["queued_local"]
    assert part(sender.remote.outgoing()[0][0], "approval")["branch"] == "roam-22m-login"


@pytest.mark.parametrize("capability", [{}, {"approval_version": True}, {"approval_version": 2}])
def test_malformed_approval_capability_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability: object
) -> None:
    sender = Sender(tmp_path)

    def request(*_args: object) -> object:
        return capability

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    assert not supports_approvals(sender.bridge)


def test_outbox_refuses_record_bound_approval(tmp_path: Path) -> None:
    sender = Sender(tmp_path)
    sender.approve()
    direction = Direction.parse(part(sender.remote.outgoing()[0][0], "coordination"))
    approval = Approval(
        "roam-22m", "roam-22m-login", "develop", "2999-01-01T00:00:00+00:00", 42, SHA, "0" * 32
    )
    with pytest.raises(BusError, match="no gate record"):
        sender.remote.enqueue(
            SenderContext("codex", "sender", str(sender.repo)),
            WORKER,
            "xyz",
            "body",
            direction=direction,
            approval=approval,
        )


# -- Inbox scan ---------------------------------------------------------------------------------


def test_receive_route_admits_valid_and_skips_malformed_approvals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = Receiver(tmp_path)
    receiver.join()
    valid = dict(receiver.item(), sequence=2)
    broken = dict(receiver.item(record_id="0" * 32), sequence=1)
    receipts: list[object] = []

    def request(_self: object, _method: str, route: str, data: object = None) -> object:
        if route.startswith("/v2/coordination/inbox?") and "after=0" in route:
            return [broken, valid]
        if route == "/v1/receipts":
            receipts.append(data)
            return {"id": valid["id"], "status": "delivered_local"}
        return []

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    bridge = Bridge(receiver.store, receiver.config)
    assert (
        receive_route(
            bridge, receiver.config.repositories[0], "/v2/coordination/inbox", coordination=True
        )
        == 1
    )
    assert receipts == [{"id": valid["id"]}]


def test_approval_on_ordinary_route_is_a_class_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = Receiver(tmp_path)
    receiver.join()
    before = receiver.count("messages")
    item = delivery()
    item["approval"] = part(receiver.item(), "approval")

    def request(*_args: object) -> object:
        return [item]

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    with pytest.raises(BusError, match="class conflicts"):
        receive_route(
            Bridge(receiver.store, receiver.config),
            receiver.config.repositories[0],
            "/v1/inbox",
            coordination=False,
        )
    assert receiver.count("messages") == before

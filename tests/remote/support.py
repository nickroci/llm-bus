"""remote test support."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final
from uuid import uuid4

from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from llm_bus.policy.config import Config, PeerPolicy, Repository, load_config
from llm_bus.remote.models import Delivery
from llm_bus.remote.store import RemoteStore
from llm_bus.routing import routing_scope


def setup_machine(tmp_path: Path, machine: str) -> tuple[Store, Config]:
    token = tmp_path / f"{machine}.token"
    token.write_text("a" * 43)
    token.chmod(0o600)
    folder = tmp_path / "repo"
    folder.mkdir(exist_ok=True)
    config = Config(
        "https://bus.test",
        machine,
        token,
        (
            Repository(
                routing_scope(str(folder)),
                "xyz",
                (
                    PeerPolicy(
                        "b" if machine == "a" else "a", send_messages=True, receive_messages="inbox"
                    ),
                ),
            ),
        ),
    )
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(f'''policy_version = 2
[remote]
url = "https://bus.test"
machine_id = "{machine}"
credential_file = "{machine}.token"
[[repositories]]
path = "{folder.as_posix()}"
repo_id = "xyz"
[[repositories.peers]]
machine_id = "{"b" if machine == "a" else "a"}"
send_messages = true
receive_messages = "inbox"
''')
    return Store(tmp_path / f"{machine}.sqlite3"), config


def assert_config_scope(folder: Path) -> None:
    current = load_config()
    assert current is not None
    assert current.repositories[0].scope == routing_scope(str(folder))


@final
class FakeClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def delivery(remote_id: str | None = None) -> Delivery:
    return Delivery(
        id=remote_id or str(uuid4()),
        sequence=1,
        sender="a/xyz/codex:sender",
        recipient="b/xyz/claude:recipient",
        repo_id="xyz",
        body="review migration",
        sent_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
    )


def admit_delivery(
    store: Store, config: Config, item: Delivery
) -> tuple[str, int | None, bool] | None:
    return RemoteStore(store, config.connection).admit_message(item, config.repositories[0].scope)


def queued_delivery(remote: RemoteStore, folder: Path) -> dict[str, object]:
    return remote.enqueue(
        SenderContext("codex", "sender", str(folder)), "b/xyz/claude:recipient", "xyz", "hello"
    )


def deny_inbox() -> None:
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'receive_messages = "inbox"', 'receive_messages = "deny"'
        ),
        encoding="utf-8",
    )


def monitor_only() -> None:
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'receive_messages = "inbox"', 'receive_messages = "monitor"'
        ),
        encoding="utf-8",
    )


def allow_reports() -> None:
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8") + 'send_reports = ["received", "completed"]\n',
        encoding="utf-8",
    )


def assert_inbox_admission(admitted: tuple[str, int | None, bool] | None) -> int:
    assert admitted is not None
    assert admitted[0] == "inbox"
    local_id = admitted[1]
    assert local_id is not None
    return local_id


def assert_imported_id(admitted: tuple[str, int | None, bool] | None) -> int:
    assert admitted is not None
    local_id = admitted[1]
    assert local_id is not None
    return local_id


def imported_delivery(store: Store, config: Config) -> tuple[Delivery, int]:
    item = delivery()
    admitted = admit_delivery(store, config, item)
    return item, assert_imported_id(admitted)

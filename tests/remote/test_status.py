"""status."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from llm_bus.errors import BusError
from llm_bus.mailbox.store import Store
from llm_bus.remote.bridge import Bridge
from llm_bus.remote.http import RelayError
from llm_bus.remote.store import RemoteStore
from llm_bus.reports.store import Reports

from .support import (
    FakeClock,
    allow_reports,
    delivery,
    imported_delivery,
    queued_delivery,
    setup_machine,
)


def test_remote_status_preserves_existing_keys(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    sent = queued_delivery(remote, tmp_path / "repo")
    row = Reports(store, config.connection).statuses()[0]
    assert {key: row[key] for key in ("id", "remote_id", "recipient", "status", "work_report")} == {
        "id": sent["id"],
        "remote_id": sent["remote_id"],
        "recipient": sent["recipient"],
        "status": "queued_local",
        "work_report": None,
    }
    assert row["wake"] == store.message_status(cast("int", sent["id"]))["wake"]
    assert "upload_synchronization" in row
    assert "report_synchronization" in row
    assert cast("dict[str, object]", row["synchronization"])["first_accepted_at"] is None


def test_message_status_import_is_local_admission_not_queued_report(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    envelope = delivery()
    imported = RemoteStore(store, config.connection).admit_message(
        envelope, config.repositories[0].scope
    )
    assert imported is not None
    local_id = imported[1]
    assert local_id is not None
    with store.transaction() as db:
        db.execute(
            "INSERT INTO remote_report_submissions VALUES (?,0,?,0)",
            (envelope["id"], json.dumps({"status": "proceeding", "reason": "started"})),
        )
    Path(os.environ["LLM_BUS_CONFIG"]).unlink()
    result = store.message_status(local_id)
    transport = cast("dict[str, object]", result["transport"])
    assert transport["status"] == "delivered_local"
    assert transport["source"] == "local"
    assert transport["observed_at"] is None
    assert result["work_report"] is None
    assert store.inbox("claude:recipient") == []
    RemoteStore(store, config.connection).record_sync_result(
        "receipt", envelope["id"], datetime.now(UTC), error_code=None
    )
    result = store.message_status(local_id)
    assert cast("dict[str, object]", result["transport"])["observed_at"] is None
    assert cast("dict[str, object]", result["synchronization"])["last_success_at"] is not None


def test_message_status_tracks_real_report_submission_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each retained revision has an independent retry and acceptance lifecycle.
    # pylint: disable=too-many-locals
    store, config = setup_machine(tmp_path, "b")
    allow_reports()
    store.register("claude", "recipient", str(tmp_path / "repo"))
    item, local_id = imported_delivery(store, config)
    reports = Reports(store, config.connection)
    reports.enqueue(local_id, "claude:recipient", "received", None)
    reports.enqueue(local_id, "claude:recipient", "completed", None)
    clock = FakeClock()
    bridge = Bridge(store, config, clock=clock)
    responses: list[object] = [
        RelayError("unavailable"),
        {"message_id": item["id"], "revision": 1, "status": "received", "reason": None},
        RelayError("not_allowed"),
        {"message_id": item["id"], "revision": 2, "status": "completed", "reason": None},
    ]

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        if route == "/v1/reports":
            response = responses.pop(0)
            if isinstance(response, RelayError):
                raise response
            return response
        return []

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    assert bridge.sync()["errors"] == 1
    before = store.path.read_bytes()
    result = Store.read_only(store.path).message_status(local_id)
    assert store.path.read_bytes() == before
    assert result["work_report"] is None
    sync = cast("dict[str, object]", result["synchronization"])
    assert sync["error_code"] is None  # Receipt success is independent of report failure.
    operations = cast("list[dict[str, object]]", sync["operations"])
    failed = next(operation for operation in operations if operation["kind"] == "report_submit")
    assert (failed["expected_revision"], failed["error_code"], failed["last_success_at"]) == (
        0,
        "unavailable",
        None,
    )
    assert failed["next_attempt_at"] == (clock() + timedelta(seconds=5)).isoformat()
    assert failed["stale"] is True
    assert bridge.sync()["errors"] == 0  # Retry is not due; revision 1 remains blocked.
    clock.advance(5)
    assert bridge.sync()["errors"] == 0
    assert bridge.sync()["errors"] == 1
    sync = cast("dict[str, object]", store.message_status(local_id)["synchronization"])
    operations = cast("list[dict[str, object]]", sync["operations"])
    submissions = [operation for operation in operations if operation["kind"] == "report_submit"]
    assert [
        (operation["expected_revision"], operation["error_code"], operation["last_success_at"])
        for operation in submissions
    ] == [(0, None, clock().isoformat()), (1, "not_allowed", None)]
    assert submissions[1]["next_attempt_at"] == (clock() + timedelta(seconds=5)).isoformat()
    clock.advance(5)
    assert bridge.sync()["errors"] == 0
    assert reports.pending() == []
    assert responses == []
    assert store.message_status(local_id)["work_report"] is None


def test_message_status_legacy_errors_and_report_validation(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    sent = queued_delivery(remote, tmp_path / "repo")
    key = str(sent["remote_id"])
    remote.record_sync_result(
        "status", key, datetime.now(UTC) - timedelta(seconds=31), error_code=None
    )
    assert (
        cast("dict[str, object]", store.message_status(cast("int", sent["id"]))["synchronization"])[
            "stale"
        ]
        is True
    )
    with store.transaction() as db:
        db.execute("UPDATE remote_sync_work SET error_code=?", ("\x1b secret credential.token",))
    result = store.message_status(cast("int", sent["id"]))
    assert cast("dict[str, object]", result["synchronization"])["error_code"] == "protocol_error"
    assert "secret" not in json.dumps(result)
    with store.transaction() as db:
        db.execute("INSERT INTO remote_reports VALUES (?,?)", (key, json.dumps({"text": "unsafe"})))
    with pytest.raises(BusError, match="Invalid work report"):
        store.message_status(cast("int", sent["id"]))

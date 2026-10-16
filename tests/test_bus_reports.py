"""Typed work reports: strict wire validation and durable recipient binding."""

import json
from pathlib import Path
from typing import cast

import pytest

from llm_bus_reports import parse_report, validate_status
from llm_bus_store import BusError

CASES = cast(
    "list[dict[str, object]]",
    json.loads((Path(__file__).parent / "fixtures/report-cases.json").read_text()),
)


@pytest.mark.parametrize("case", CASES)
def test_shared_vocabulary(case: dict[str, object]) -> None:
    if case["valid"]:
        validate_status(case["status"], case["reason"])
    else:
        with pytest.raises(BusError):
            validate_status(case["status"], case["reason"])


@pytest.mark.parametrize(
    "change",
    [
        {"revision": True},
        {"revision": -1},
        {"revision": 1.5},
        {"extra": "text"},
        {"message_id": "other"},
        {"status": "failed", "reason": "custom"},
    ],
)
def test_wire_rejects_untrusted_fields(change: dict[str, object]) -> None:
    message_id = "5863adf2-10e7-4b20-887e-e6f4a2ba55aa"
    with pytest.raises(BusError):
        parse_report(
            {
                "message_id": message_id,
                "revision": 1,
                "status": "received",
                "reason": None,
                **change,
            },
            message_id,
        )

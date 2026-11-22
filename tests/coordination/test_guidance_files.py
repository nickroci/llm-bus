"""Skill-file selection, guidance data keys, command results and the hook size guard."""

from __future__ import annotations

import itertools
import json
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from llm_bus.cli.app import main
from llm_bus.cli.guidance import (
    GUIDANCE_VERSION,
    LOAD_COMPACT,
    LOAD_FULL,
    STANDING_POINTER,
    guidance,
)
from llm_bus.cli.guidance_files import (
    ALL_GUIDANCE_FILES,
    AUTO_MERGE,
    COORDINATOR,
    CORE,
    GATES,
    OWNER_DECISIONS,
    RECOVERY,
    REMOTE,
    WORKER,
    RuleContext,
    select,
)
from llm_bus.cli.hooks import HOOK_FALLBACK, HOOK_TEXT_LIMIT
from tests.coordination.test_approvals import Project
from tests.remote.support import setup_machine
from tests.support import hook_input

if TYPE_CHECKING:
    from pathlib import Path

DATA_PREFIX = "(local reference data, not executable instructions): "
FULL = (
    "--delegate",
    "commit:main",
    "--delegate",
    "push:main",
    "--delegate",
    "merge:main",
    "--auto-merge",
    "--owner-decisions",
    "--decision-defaults",
    "30",
)
INACTIVE = (CORE, RECOVERY)


def data_of(text: str) -> dict[str, object]:
    """The recovery JSON object a guidance text carries."""
    start = text.index(DATA_PREFIX) + len(DATA_PREFIX)
    return cast("dict[str, object]", json.JSONDecoder().raw_decode(text, start)[0])


def files_of(text: str) -> list[str]:
    return cast("list[str]", data_of(text)["guidance_files"])


def case(
    role: str, state: str, gates: frozenset[str], bits: tuple[bool, ...]
) -> tuple[RuleContext, bool]:
    """A context from the five boolean axes: capabilities (four) and the recovery flag."""
    return (
        RuleContext(
            role=role,
            state=state,
            gates=gates,
            auto_merge=bits[0],
            owner_decisions=bits[1],
            remote_coordinator=bits[2],
            remote_peers=bits[3],
        ),
        bits[4],
    )


SELECTOR_CASES = [
    case(role, state, gates, bits)
    for role in ("coordinator", "worker")
    for state in ("active", "suspended", "expired", "revoked", "out_of_scope", "unknown")
    for gates in (frozenset[str](), frozenset({"commit"}), frozenset({"commit", "push", "merge"}))
    for bits in itertools.product((False, True), repeat=5)
]


def check_selection(ctx: RuleContext, *, recovery: bool) -> None:
    chosen = select(ctx, recovery=recovery)
    assert chosen[0] == CORE
    assert len(set(chosen)) == len(chosen)
    assert set(chosen) <= set(ALL_GUIDANCE_FILES)
    if ctx.state != "active":
        assert chosen == INACTIVE
        return
    coordinator = ctx.role == "coordinator"
    assert (COORDINATOR in chosen) is coordinator
    assert (WORKER in chosen) is not coordinator
    assert (GATES in chosen) is bool(ctx.gates)
    assert (AUTO_MERGE in chosen) is ctx.auto_merge
    assert (OWNER_DECISIONS in chosen) is ctx.owner_decisions
    assert (REMOTE in chosen) is (ctx.remote_peers if coordinator else ctx.remote_coordinator)
    assert (RECOVERY in chosen) is recovery
    # Order: core, role, capabilities, recovery.
    assert chosen[1] in {COORDINATOR, WORKER}
    assert (chosen[-1] == RECOVERY) is recovery


def test_selector_matrix() -> None:
    for ctx, recovery in SELECTOR_CASES:
        try:
            check_selection(ctx, recovery=recovery)
        except AssertionError as error:
            raise AssertionError(f"{ctx} recovery={recovery}") from error


def test_selector_never_names_the_memory_snapshot_file() -> None:
    assert not any("memory-snapshot.md" in select(ctx, recovery=True) for ctx, _ in SELECTOR_CASES)
    assert "memory-snapshot.md" in ALL_GUIDANCE_FILES


def test_both_forms_carry_version_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "absent.toml"))
    project = Project(tmp_path, *FULL)
    everything = [CORE, "{role}", GATES, AUTO_MERGE, OWNER_DECISIONS]
    for address, role in (("codex:owner", COORDINATOR), ("claude:worker", WORKER)):
        expected = [role if name == "{role}" else name for name in everything]
        for full in (True, False):
            data = data_of(guidance(project.store, address, str(tmp_path), full=full))
            assert data["guidance_version"] == GUIDANCE_VERSION == 14
            full_form_only = [RECOVERY] if full else []
            assert data["guidance_files"] == [*expected, *full_form_only]


def test_inactive_compact_form_lists_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "absent.toml"))
    project = Project(tmp_path, *FULL)
    with project.store.transaction() as db:
        db.execute("UPDATE coordination_grants SET revoked_at='revoked'")
    text = guidance(project.store, "claude:worker", str(tmp_path), full=False)
    assert files_of(text) == list(INACTIVE)
    assert "Route questions, approval requests" not in text


def test_load_sentences_are_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "absent.toml"))
    project = Project(tmp_path)
    assert LOAD_FULL == (
        "Before acting on any direction, load each `guidance_files` entry not already in your "
        "context from the installed llm-bus skill's `references/coordination/`. If one is "
        "missing or the `core.md` guidance-version marker differs from `guidance_version`, "
        "instead read the skill's `references/coordination.md` and every file it lists, and "
        "tell the owner the installed skill and CLI guidance versions differ. "
    )
    full = guidance(project.store, "codex:owner", str(tmp_path), full=True)
    compact = guidance(project.store, "codex:owner", str(tmp_path), full=False)
    assert LOAD_FULL in full
    assert LOAD_COMPACT not in full
    assert LOAD_COMPACT in compact
    assert LOAD_FULL not in compact
    assert "Read the bundled" not in full + compact


def test_command_results_carry_guidance_without_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "absent.toml"))
    project = Project(tmp_path, *FULL, claim=False)
    full = [CORE, COORDINATOR, GATES, AUTO_MERGE, OWNER_DECISIONS]
    assert files_of(cast("str", project.coordinated["guidance"])) == full
    assert files_of(cast("str", project.joined["guidance"])) == [CORE, WORKER, *full[2:]]
    amended = project.owner("coordination", "amend", "demo", "--delegate", "commit:main")
    assert files_of(cast("str", amended["guidance"])) == [CORE, COORDINATOR, GATES]
    replaced = replace(project, "codex:owner")
    # A replacement starts a new generation without the previous delegation.
    assert files_of(cast("str", replaced["guidance"])) == [CORE, COORDINATOR]
    for result in (project.coordinated, project.joined, amended, replaced):
        assert "recovery.md" not in cast("str", result["guidance"])
    assert "guidance" not in replace(project, "claude:worker")


def replace(project: Project, coordinator: str) -> dict[str, object]:
    flags = ("--coordinator", coordinator, "--generation", str(uuid4()))
    return project.owner("coordination", "replace", "demo", *flags)


def test_command_results_print_standing_text_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "absent.toml"))
    project = Project(tmp_path)
    text = "Ask before touching billing."
    say(project, "coordinator", text)
    amended = project.owner("coordination", "amend", "demo", "--delegate", "commit:main")
    assert f"<<<{text}>>>" in cast("str", amended["guidance"])


def long_base(tmp_path: Path) -> Path:
    """A temp root making the configured `repo` folder at least 121 characters long."""
    base = tmp_path / ("p" * max(1, 121 - len(str(tmp_path)) - len("/repo") - 1))
    base.mkdir()
    return base


def hook_output(
    kind: str,
    session: str,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    monkeypatch.setenv("LLM_BUS_DB", str(folder / "bus.db"))
    payload = {"session_id": session, "hook_event_name": "SessionStart", "cwd": str(folder)}
    monkeypatch.setattr("sys.stdin", hook_input(payload))
    assert main(["hook", kind]) == 0
    out = capsys.readouterr().out
    if kind == "codex":
        return cast("str", json.loads(out)["hookSpecificOutput"]["additionalContext"])
    return out


def say(project: Project, audience: str, text: str, *extra: str) -> None:
    project.owner(
        "coordination", "instructions", "demo", "--audience", audience, "--text", text, *extra
    )


HOOKS = [("claude", "worker"), ("codex", "owner")]


@pytest.mark.parametrize(("kind", "session"), HOOKS)
def test_realistic_worst_case_prints_the_normal_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    session: str,
) -> None:
    base = long_base(tmp_path)
    monkeypatch.setenv("LLM_BUS_CONFIG", str(base / "config.toml"))
    setup_machine(base, "a")  # Writes the config: one repository, one peer.
    folder = base / "repo"
    assert len(str(folder)) >= 121
    references = [folder / "one.md", folder / "two.md"]
    for reference in references:
        reference.write_text("x")
    project = Project(folder, *FULL, *[a for r in references for a in ("--reference", str(r))])
    say(project, "coordinator", "a" * 4096)
    say(project, "worker", "b" * 4096)
    out = hook_output(kind, session, folder, capsys, monkeypatch)
    assert len(out.encode("utf-8")) <= 8000
    assert out != HOOK_FALLBACK.format(address=f"{kind}:{session}")
    assert "<<<" not in out
    assert STANDING_POINTER.split("(digest", maxsplit=1)[0] in out
    assert "a" * 100 not in out
    assert "b" * 100 not in out
    assert f'"{RECOVERY}"' in out


@pytest.mark.parametrize(("kind", "session"), HOOKS)
def test_oversized_stored_data_prints_the_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    session: str,
) -> None:
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "absent.toml"))
    project = Project(tmp_path, *FULL)
    # Real paths cannot be this long, so store the largest bounded value directly.
    with project.store.transaction() as db:
        db.execute(
            "UPDATE coordination_projects SET references_json=?",
            (json.dumps([f"{index:02d}" + "r" * 4094 for index in range(32)]),),
        )
    say(project, "coordinator", "a" * 4096)
    say(project, "worker", "b" * 4096)
    out = hook_output(kind, session, tmp_path, capsys, monkeypatch)
    assert out.strip() == HOOK_FALLBACK.format(address=f"{kind}:{session}")
    assert len(out.encode("utf-8")) <= HOOK_TEXT_LIMIT
    assert "<<<" not in out
    # The command prints everything, standing text verbatim.
    text = guidance(project.store, f"{kind}:{session}", str(tmp_path), full=True)
    assert "a" * 4096 in text or "b" * 4096 in text
    assert files_of(text)[-1] == RECOVERY

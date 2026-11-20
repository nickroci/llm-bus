"""Guided coordinator setup: agent profile, wizard confirmation and storage."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus import main
from llm_bus.coordination.setup import (
    DISCOVERY_STEP,
    INSTRUCTIONS_STEP,
    detect_integration_branch,
    profile,
    run_wizard,
)
from llm_bus.errors import BusError
from llm_bus.mailbox.store import Store

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

GATES = ("commit", "push", "merge", "merge-admin")
HINT = "Detected from origin/HEAD; check the repository instructions"


def git(folder: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(folder), *argv], check=True, capture_output=True)  # noqa: S607


@pytest.fixture(name="repo")
def make_repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@x",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    git(path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
    (path / ".beads").mkdir()
    return path


def scripted(replies: list[str]) -> tuple[Callable[[str], str], list[str]]:
    prompts: list[str] = []
    queue = iter(replies)

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(queue)

    return ask, prompts


def counts(store: Store) -> tuple[int, int, int]:
    with store.transaction() as db:
        return cast(
            "tuple[int, int, int]",
            db.execute(
                "SELECT (SELECT COUNT(*) FROM coordination_projects),"
                "(SELECT COUNT(*) FROM coordination_delegations),"
                "(SELECT COUNT(*) FROM agents)"
            ).fetchone(),
        )


def answers(  # noqa: PLR0913 - explicit wizard choices
    repo: Path,
    *,
    gates: tuple[str, ...] = ("yes",) * 4,
    auto_merge: str | None = "yes",
    decide: tuple[str, ...] = ("no",),
    confirm: str = "yes",
    execution: str = "",
) -> list[str]:
    """None skips an answer the wizard asks only conditionally.

    Auto-merge is asked only when a merge gate remains. ``decide`` holds the owner-decisions
    answer, always asked, then the default-minutes answer, asked only when decisions are on.
    """
    # pylint: disable=too-many-arguments
    merge = [] if auto_merge is None else [auto_merge]
    return ["Ship it", str(repo), "bus-1", "", *gates, *merge, *decide, "", execution, confirm]


def test_detects_origin_head_without_network(repo: Path, tmp_path: Path) -> None:
    assert detect_integration_branch(str(repo)) == ("develop", "origin-head")
    assert detect_integration_branch(str(tmp_path)) == (None, "undetected")


def test_noninteractive_setup_prints_profile_and_stores_nothing(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(db))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "owner")
    monkeypatch.chdir(repo)
    assert main(["--format", "agent", "coordinate", "demo", "--setup"]) == 0
    out = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert out["delegate"] == [f"{gate}:develop" for gate in GATES]
    assert out["floor"]
    steps = cast("list[str]", out["steps"])
    assert steps[0] == DISCOVERY_STEP
    assert "never runs llm-bus" in steps[0]
    assert "Confirm the integration branch with the owner" in steps[0]
    assert steps[-1] == INSTRUCTIONS_STEP
    assert "llm-bus" in str(out["command"])
    assert profile(str(repo)) == out
    assert counts(Store(db)) == (0, 0, 0)


def test_profile_without_origin_head_delegates_nothing(tmp_path: Path) -> None:
    out = profile(str(tmp_path))
    assert out["delegate"] == []
    assert out["integration_branch"] == {"name": None, "source": "undetected"}


def test_wizard_stores_only_after_yes(repo: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    before = counts(store)
    ask, _ = scripted(answers(repo, confirm="no"))
    said: list[str] = []
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=said.append) is None
    assert counts(store) == before
    assert any(HINT in line for line in said)
    ask, _ = scripted(answers(repo))
    said.clear()
    result = run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=said.append)
    assert result is not None
    assert result["enrolled"] is True
    assert counts(store) == (1, 1, before[2])
    screen = "\n".join(said)
    for item in (f"{gate}:develop" for gate in GATES):
        assert item in screen
    assert "merge-admin bypasses only the required-approvals rule" in screen
    assert "force-push" in screen
    assert HINT in screen


def test_wizard_disabled_gate_is_omitted(repo: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    ask, _ = scripted(answers(repo, gates=("yes", "yes", "yes", "no")))
    said: list[str] = []
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=said.append)
    screen = "\n".join(said)
    assert "merge-admin:develop" not in screen
    assert "bypasses" not in screen
    with store.transaction() as db:
        (text,) = cast(
            "tuple[str]",
            db.execute("SELECT delegation_json FROM coordination_delegations").fetchone(),
        )
    assert "merge-admin" not in json.loads(text)["gates"]


def test_wizard_changed_target_branch(repo: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    script = answers(repo)
    script[3] = "release"
    ask, _ = scripted(script)
    said: list[str] = []
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=said.append)
    assert "merge:release" in "\n".join(said)


@pytest.mark.parametrize(
    ("index", "bad", "asked"),
    [(0, "  ", 1), (1, "/nonexistent-workspace", 2), (2, "", 3)],
)
def test_wizard_validates_each_answer_at_its_prompt(
    repo: Path, tmp_path: Path, index: int, bad: str, asked: int
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    before = counts(store)
    script = answers(repo)
    script[index] = bad
    ask, prompts = scripted(script)
    with pytest.raises(BusError):
        run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _line: None)
    assert len(prompts) == asked
    assert counts(store) == before


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_wizard_interrupt_is_a_clean_error_and_stores_nothing(
    repo: Path, tmp_path: Path, error: type[BaseException]
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    before = counts(store)

    def ask(_prompt: str) -> str:
        raise error

    with pytest.raises(BusError, match="nothing stored"):
        run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _line: None)
    assert counts(store) == before


@pytest.mark.parametrize("branch", ["a b", "*", "release/*", "-x", "a..b"])
def test_wizard_rejects_globs_before_storing(repo: Path, tmp_path: Path, branch: str) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    before = counts(store)
    script = answers(repo)
    script[3] = branch
    ask, _ = scripted(script)
    with pytest.raises(BusError):
        run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _line: None)
    assert counts(store) == before


def test_wizard_other_coordinator_is_not_enrolled(repo: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    store.register("claude", "lead", str(repo))
    script = answers(repo)
    script[-3] = "claude:lead"
    ask, _ = scripted(script)
    result = run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _line: None)
    assert result is not None
    assert result["enrolled"] is False
    assert "claude:lead" in str(result["next"])
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM coordination_enrollments").fetchone() == (0,)


def test_wizard_without_identity_picks_from_agents(repo: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("claude", "lead", str(repo))
    script = answers(repo)
    script[-3] = "1"
    ask, prompts = scripted(script)
    result = run_wizard(store, "demo", str(repo), None, ask=ask, say=lambda _line: None)
    assert result is not None
    assert result["enrolled"] is False
    assert any("claude:lead" in prompt for prompt in prompts)


@pytest.mark.parametrize("label", ["", "  ", "x\x07y", "L" * 4097])
def test_wizard_validates_label_before_asking(repo: Path, tmp_path: Path, label: str) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    before = counts(store)
    ask, prompts = scripted(answers(repo))
    with pytest.raises(BusError):
        run_wizard(store, label, str(repo), "codex:owner", ask=ask, say=lambda _line: None)
    assert prompts == []
    assert counts(store) == before


def test_wizard_existing_project_fails_before_asking(repo: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    ask, _ = scripted(answers(repo))
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _line: None)
    before = counts(store)
    ask, prompts = scripted(answers(repo))
    with pytest.raises(BusError, match="already exists"):
        run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _line: None)
    assert prompts == []
    assert counts(store) == before


def test_wizard_enter_keeps_every_gate_enabled(repo: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    ask, _ = scripted(answers(repo, gates=("",) * 4))
    said: list[str] = []
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=said.append)
    screen = "\n".join(said)
    assert all(f"{gate}:develop" in screen for gate in GATES)


def _stored_delegation(store: Store) -> dict[str, object]:
    with store.transaction() as db:
        (text,) = cast(
            "tuple[str]",
            db.execute("SELECT delegation_json FROM coordination_delegations").fetchone(),
        )
    return cast("dict[str, object]", json.loads(text))


@pytest.mark.parametrize("answer", ["", "no", "yes"])
def test_project_execution_requires_explicit_opt_in(
    repo: Path, tmp_path: Path, answer: str
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    ask, prompts = scripted(answers(repo, execution=answer))
    said: list[str] = []
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=said.append)
    expected = True if answer == "yes" else None
    assert _stored_delegation(store).get("project_execution") is expected
    assert "paid or live QA runs and development deployments" in prompts[-2]
    screen = "\n".join(said)
    assert "Preapprove paid or live QA runs and development deployments: " in screen
    assert "native and provider permissions" in screen
    assert "Production operations" in screen
    setup_profile = profile(str(repo))
    choice = cast("dict[str, object]", setup_profile["project_execution"])
    assert choice["default"] is False
    assert "--project-execution" not in str(setup_profile["command"])


def test_cancelled_project_execution_opt_in_stores_nothing(repo: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    before = counts(store)
    ask, _ = scripted(answers(repo, execution="yes", confirm="no"))
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _: None) is None
    assert counts(store) == before


def test_wizard_project_execution_needs_no_gate_or_owner_decisions(
    repo: Path, tmp_path: Path
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    ask, _ = scripted(answers(repo, gates=("no",) * 4, auto_merge=None, execution="yes"))
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _: None)
    assert _stored_delegation(store) == {"version": 1, "gates": {}, "project_execution": True}


@pytest.mark.parametrize(
    ("answer", "outcome"), [("yes", "stored"), ("", "stored"), ("no", "omitted")]
)
def test_wizard_asks_about_auto_merge_when_a_merge_gate_remains(
    repo: Path, tmp_path: Path, answer: str, outcome: str
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    ask, prompts = scripted(answers(repo, auto_merge=answer))
    said: list[str] = []
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=said.append)
    assert sum("auto-merge" in prompt for prompt in prompts) == 1
    stored = _stored_delegation(store)
    screen = "\n".join(said)
    if outcome == "stored":
        assert stored["auto_merge"] is True
        assert "each merge still needs the coordinator to accept" in screen
    else:
        assert "auto_merge" not in stored
        assert "each merge still needs" not in screen


def test_wizard_merge_admin_alone_still_asks_about_auto_merge(repo: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    ask, prompts = scripted(answers(repo, gates=("no", "no", "no", "yes")))
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _line: None)
    assert any("auto-merge" in prompt for prompt in prompts)
    assert _stored_delegation(store)["auto_merge"] is True


def test_wizard_without_merge_gate_does_not_ask_about_auto_merge(
    repo: Path, tmp_path: Path
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    ask, prompts = scripted(answers(repo, gates=("yes", "yes", "no", "no"), auto_merge=None))
    said: list[str] = []
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=said.append)
    assert not any("auto-merge" in prompt for prompt in prompts)
    assert "auto_merge" not in _stored_delegation(store)
    assert not any("each merge still needs" in line for line in said)


def test_profile_command_names_auto_merge_only_with_the_flag(repo: Path, tmp_path: Path) -> None:
    full = profile(str(repo))
    assert full["flags"] == ["auto-merge", "owner-decisions", "decision-defaults"]
    assert "--auto-merge --owner-decisions --decision-defaults 30" in str(full["command"])
    bare = profile(str(tmp_path))
    assert bare["flags"] == ["owner-decisions", "decision-defaults"]
    assert "--auto-merge" not in str(bare["command"])
    assert str(bare["command"]).endswith("--owner-decisions --decision-defaults 30")


def test_wizard_always_asks_about_owner_decisions(repo: Path, tmp_path: Path) -> None:
    for number, (gates, merge) in enumerate(((("yes",) * 4, "yes"), (("no",) * 4, None))):
        store = Store(tmp_path / f"bus{number}.sqlite3")
        store.register("codex", "owner", str(repo))
        ask, prompts = scripted(answers(repo, gates=gates, auto_merge=merge))
        assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _line: None)
        assert sum("owner decisions" in prompt for prompt in prompts) == 1
        assert not any("Decision defaults" in prompt for prompt in prompts)


def test_wizard_decisions_only_delegation_with_default_minutes(repo: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    script = answers(repo, gates=("no",) * 4, auto_merge=None, decide=("yes", "45"))
    ask, prompts = scripted(script)
    said: list[str] = []
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=said.append)
    assert sum("Decision defaults" in prompt for prompt in prompts) == 1
    assert _stored_delegation(store) == {
        "version": 1,
        "gates": {},
        "owner_decisions": True,
        "decision_default_minutes": 45,
    }
    screen = "\n".join(said)
    assert "Delegations: owner-decisions, decision-defaults" in screen
    assert "after 45 minutes" in screen


@pytest.mark.parametrize(
    ("answer", "stored"), [("", 30), ("no", None), ("n", None), ("1440", 1440)]
)
def test_wizard_default_minutes_answers(
    repo: Path, tmp_path: Path, answer: str, stored: int | None
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    ask, _ = scripted(answers(repo, decide=("yes", answer)))
    assert run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _line: None)
    assert _stored_delegation(store).get("decision_default_minutes") == stored


@pytest.mark.parametrize("bad", ["0", "1441", "soon", "-5"])
def test_wizard_rejects_bad_default_minutes_before_storing(
    repo: Path, tmp_path: Path, bad: str
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "owner", str(repo))
    before = counts(store)
    ask, _ = scripted(answers(repo, decide=("yes", bad)))
    with pytest.raises(BusError):
        run_wizard(store, "demo", str(repo), "codex:owner", ask=ask, say=lambda _line: None)
    assert counts(store) == before


def test_setup_without_identity_stores_nothing(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    db = tmp_path / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(db))
    monkeypatch.chdir(repo)
    assert main(["coordinate", "demo", "--setup"]) == 0
    assert counts(Store(db)) == (0, 0, 0)


def test_terminal_setup_routes_to_wizard(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "owner")
    monkeypatch.chdir(repo)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    calls: list[tuple[str, str | None]] = []

    def fake_wizard(
        _store: Store, label: str, _cwd: str, identity: str | None
    ) -> dict[str, object]:
        calls.append((label, identity))
        return {"wizard": True}

    monkeypatch.setattr("llm_bus.cli.app.run_wizard", fake_wizard)
    assert main(["coordinate", "demo", "--setup"]) == 0
    assert calls == [("demo", "claude:owner")]
    assert main(["--format", "agent", "coordinate", "other", "--setup"]) == 0
    assert len(calls) == 1

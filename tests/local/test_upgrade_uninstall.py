"""Interrupted integration removal through the disposable uv lifecycle."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import cast

import pytest

from llm_bus import generations, maintenance
from llm_bus.errors import BusError
from llm_bus.hosts import files
from tests.local import test_upgrade as support

accepted_checkout = support.accepted_checkout
managed_install = support.managed_install


@pytest.mark.parametrize("target", ["public", "hook", "missing-parent", "codex", "claude"])
def test_uninstall_retries_integration_sync_before_completion(
    managed: support.Managed, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    managed.install()
    settings = managed.home / "custom codex/hooks.json"
    selected = {
        "public": managed.public,
        "hook": managed.runtime.root / "llm-bus",
        "missing-parent": managed.public,
        "codex": settings,
        "claude": managed.home / "custom claude/settings.json",
    }[target]
    sync = maintenance.sync_directory
    barrier = selected.parent
    attempts: list[Path] = []

    def interrupt(path: Path) -> None:
        # Settings ancestors also sync before replacement; fail only after removal.
        if (
            path == barrier
            and managed.state().get("uninstalling") is True
            and (target not in {"codex", "claude"} or "llm-bus" not in selected.read_text())
        ):
            attempts.append(path)
            raise RuntimeError("controller interrupted at integration parent sync")
        sync(path)

    before = managed.mailbox.read_bytes()
    with monkeypatch.context() as fault:
        # The writer and completion barrier expose distinct sync boundaries.
        fault.setattr(files, "sync_directory", interrupt)
        fault.setattr(maintenance, "sync_directory", interrupt)
        with pytest.raises(RuntimeError, match="integration parent sync"):
            generations.uninstall(managed.home)
    assert managed.state()["step"] == "uninstall-intent"
    if target in {"public", "hook", "missing-parent"}:
        assert not selected.exists()
    else:
        assert "llm-bus" not in selected.read_text()
    owner = cast("dict[str, object]", json.loads(settings.read_text()))
    owner["unrelated"] = {"owner": "changed after interruption"}
    settings.write_text(json.dumps(owner))
    if target == "missing-parent":
        barrier.rmdir()
        barrier = barrier.parent
    with monkeypatch.context() as fault:
        fault.setattr(files, "sync_directory", interrupt)
        fault.setattr(maintenance, "sync_directory", interrupt)
        with pytest.raises(RuntimeError, match="integration parent sync"):
            generations.recover(managed.home)
    assert len(attempts) == 2
    assert managed.state()["step"] == "uninstall-intent"
    if target in {"public", "hook"}:
        selected.write_text("owner replacement\n")
        with pytest.raises(BusError, match="Public alias changed"):
            generations.recover(managed.home)
        assert selected.read_text() == "owner replacement\n"
        assert managed.state()["step"] == "uninstall-intent"
        selected.unlink()
    recovered = generations.recover(managed.home)
    assert recovered["step"] == "uninstalled"
    assert recovered["phase"] == "held"
    assert managed.mailbox.read_bytes() == before
    assert json.loads(settings.read_text())["unrelated"] == owner["unrelated"]
    aliases = cast("list[str]", recovered["aliases"])
    assert all(not Path(name).exists() for name in aliases)
    assert not (managed.runtime.root / "llm-bus").exists()


def test_uninstall_retries_current_settings_file_sync_before_completion(
    managed: support.Managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed.install()
    settings = managed.home / "custom codex/hooks.json"
    sync = os.fsync

    def interrupt(descriptor: int) -> None:
        if (
            os.fstat(descriptor).st_ino == settings.stat().st_ino
            and managed.state().get("uninstalling") is True
        ):
            raise RuntimeError("controller interrupted at settings file sync")
        sync(descriptor)

    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", interrupt)
        with pytest.raises(RuntimeError, match="settings file sync"):
            generations.uninstall(managed.home)
        assert managed.state()["step"] == "uninstall-intent"
        with pytest.raises(RuntimeError, match="settings file sync"):
            generations.recover(managed.home)
        assert managed.state()["step"] == "uninstall-intent"
    assert generations.recover(managed.home)["step"] == "uninstalled"


@pytest.mark.parametrize("stage", ["partial-skill", "retire-sync", "remove-sync"])
def test_skill_removal_recovers_after_partial_delete_or_sync_failure(
    managed: support.Managed, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    managed.install()
    remove_tree = shutil.rmtree
    sync = maintenance.sync_directory
    skill = managed.home / "skills/llm-bus"
    barriers: list[bool] = []

    def interrupt_tree(path: Path, *, ignore_errors: bool = False) -> None:
        if stage == "partial-skill" and path.name.startswith(".llm-bus-uninstall-"):
            (path / "SKILL.md").unlink()
            raise RuntimeError("controller interrupted during skill removal")
        remove_tree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(
        interrupt_tree, "avoids_symlink_attacks", remove_tree.avoids_symlink_attacks, raising=False
    )

    def interrupt_sync(path: Path) -> None:
        removal = managed.state().get("skill_removal")
        if path == skill.parent and isinstance(removal, dict):
            exists = Path(str(cast("dict[str, object]", removal)["path"])).exists()
            barriers.append(exists)
            if (stage == "retire-sync" and exists) or (stage == "remove-sync" and not exists):
                raise RuntimeError("controller interrupted at skill parent sync")
        sync(path)

    before = managed.mailbox.read_bytes()
    with monkeypatch.context() as fault:
        fault.setattr(shutil, "rmtree", interrupt_tree)
        fault.setattr(maintenance, "sync_directory", interrupt_sync)
        with pytest.raises(RuntimeError, match="controller interrupted"):
            generations.uninstall(managed.home)
    assert managed.state()["step"] == "uninstall-intent"
    assert not skill.exists()
    if stage == "partial-skill":
        removal = cast("dict[str, object]", managed.state()["skill_removal"])
        assert not (Path(str(removal["path"])) / "SKILL.md").exists()
    if stage == "remove-sync":
        # An absent tree does not waive the failed durability barrier on retry.
        with monkeypatch.context() as fault:
            fault.setattr(maintenance, "sync_directory", interrupt_sync)
            with pytest.raises(RuntimeError, match="skill parent sync"):
                generations.recover(managed.home)
        assert managed.state()["step"] == "uninstall-intent"
        assert barriers == [True, False, False]
    if stage in {"partial-skill", "remove-sync"}:
        skill.mkdir()
        (skill / "owner.txt").write_text("recreated by owner\n")
    with pytest.raises(BusError, match="Finish recorded uninstall"):
        managed.install()
    recovered = generations.recover(managed.home)
    assert recovered["phase"] == "held"
    assert recovered["step"] == "uninstalled"
    assert managed.mailbox.read_bytes() == before
    assert not managed.public.exists()
    assert not list(skill.parent.glob(".llm-bus-uninstall-*"))
    if stage in {"partial-skill", "remove-sync"}:
        assert (skill / "owner.txt").read_text() == "recreated by owner\n"


@pytest.mark.parametrize(
    "changed", ["source-content", "source-identity", "retired-identity", "retired-symlink", "path"]
)
def test_uninstall_refuses_changed_skill_or_retirement_identity(
    managed: support.Managed, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    managed.install()
    write = maintenance.Runtime.write

    def interrupt(runtime: maintenance.Runtime, state: dict[str, object]) -> None:
        write(runtime, state)
        if state.get("skill_removal") is not None:
            raise RuntimeError("controller interrupted before skill rename")

    with monkeypatch.context() as fault:
        fault.setattr(maintenance.Runtime, "write", interrupt)
        with pytest.raises(RuntimeError, match="before skill rename"):
            generations.uninstall(managed.home)
    state = managed.state()
    recorded = cast("dict[str, object]", state["skill_removal"])
    skill = managed.home / "skills/llm-bus"
    retired = Path(str(recorded["path"]))
    saved = managed.home / "owner-skill"
    if changed == "source-content":
        (skill / "SKILL.md").write_text("owner-edited instructions\n")
    elif changed == "source-identity":
        skill.rename(saved)
        shutil.copytree(saved, skill)
    elif changed in {"retired-identity", "retired-symlink"}:
        skill.rename(saved)
        if changed == "retired-identity":
            shutil.copytree(saved, retired)
        else:
            retired.symlink_to(saved, target_is_directory=True)
    else:
        state["skill_removal"] = {**recorded, "path": str(saved)}
        managed.runtime.write(state)
    target = retired if changed.startswith("retired-") else skill
    before = (target / "SKILL.md").read_bytes()
    with pytest.raises(BusError, match=r"changed|Invalid retired skill path"):
        generations.recover(managed.home)
    assert managed.state()["step"] == "uninstall-intent"
    assert (target / "SKILL.md").read_bytes() == before

"""Real disposable uv generations, repair, provenance and compatibility barriers."""

# Lifecycle regressions share the real-uv fixture in this module.
# pylint: disable=too-many-lines

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from llm_bus import generations, maintenance
from llm_bus.cli import upgrade as build
from llm_bus.errors import BusError
from llm_bus.hosts import files
from llm_bus.mailbox.store import Store

REPOSITORY = Path(__file__).resolve().parents[2]
UV = cast("str", shutil.which("uv"))
GIT = cast("str", shutil.which("git"))
APPROVED = maintenance.OFFLINE_MESSAGE + "\n"


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0, (argv, result.stdout, result.stderr)
    return result.stdout.strip()


@pytest.fixture(scope="module", name="checkout")
def accepted_checkout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("accepted-source")
    shutil.copytree(REPOSITORY / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPOSITORY / "skills", root / "skills")
    for name in ("pyproject.toml", ".python-version"):
        shutil.copyfile(REPOSITORY / name, root / name)
    _run([GIT, "init", "-q", str(root)])
    _run([GIT, "-C", str(root), "add", "."])
    _run(
        [
            GIT,
            "-C",
            str(root),
            "-c",
            "user.name=Disposable test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "test: accepted disposable source",
        ]
    )
    return root


@dataclass
class Managed:
    home: Path
    checkout: Path
    runtime: maintenance.Runtime
    public: Path
    mailbox: Path

    def state(self) -> dict[str, object]:
        return self.runtime.read(verify_active=False)

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.public), *arguments],
            capture_output=True,
            text=True,
            check=False,
            cwd=self.checkout,
            timeout=30,
        )

    def install(self, *, repair: bool = False) -> dict[str, object]:
        return generations.install_generation(self.checkout, self.home, repair=repair)


def _isolate_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "HOME": home,
        "UV_TOOL_DIR": home / "legacy/tools",
        "UV_TOOL_BIN_DIR": home / "legacy/bin",
        "UV_CACHE_DIR": home / "uv-cache",
        "UV_PYTHON_INSTALL_DIR": home / "uv-python",
        "CODEX_HOME": home / "custom codex",
        "CLAUDE_CONFIG_DIR": home / "custom claude",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    monkeypatch.delenv("LLM_BUS_DB", raising=False)
    tooling = home / "tooling"
    tooling.mkdir()
    (tooling / "uv").symlink_to(UV)
    monkeypatch.setenv("PATH", str(tooling) + ":/usr/bin:/bin")


def _previous_wheel(home: Path, checkout: Path, previous_schema: int) -> Path:
    wheel_root = home / "previous-artifact"
    if previous_schema == 18:
        old_source = home / "schema-18-source"
        shutil.copytree(checkout, old_source, ignore=shutil.ignore_patterns(".git"))
        schema = old_source / "src/llm_bus/mailbox/schema.py"
        source = schema.read_text()
        assert source.count("\nSCHEMA_VERSION = 21\n") == 1
        schema.write_text(source.replace("\nSCHEMA_VERSION = 21\n", "\nSCHEMA_VERSION = 18\n"))
        _run([UV, "build", "--wheel", "--out-dir", str(wheel_root), str(old_source)])
        wheel = next(wheel_root.glob("*.whl"))
    else:
        wheel = build.build_wheel(UV, checkout, wheel_root, wheel_root / "dist")
    return wheel


@pytest.fixture(name="managed")
def managed_install(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Managed:
    # T2 owns native process proof. Real lsof still inspects only this fixture's DB.
    monkeypatch.setattr(maintenance, "process_inventory", list)
    if sys.platform == "linux" and os.environ.get("LLM_BUS_TEST_HANDLE_FIXTURE") == "1":
        monkeypatch.setattr(maintenance, "mailbox_evidence", _fixture_handles)
    home = tmp_path.resolve() / "home with spaces"
    home.mkdir()
    _isolate_environment(home, monkeypatch)
    selected = cast("object", getattr(request, "param", 21))
    previous_schema = 18 if selected == 18 else 21
    wheel = _previous_wheel(home, checkout, previous_schema)
    _run(
        [
            UV,
            "tool",
            "install",
            "--python",
            str(Path(sys.executable).resolve()),
            build.requirement_arg(wheel, ("collab",) if selected == "collab" else ()),
        ]
    )
    public = home / "legacy/bin/llm-bus"
    monkeypatch.setenv("PATH", str(public.parent) + os.pathsep + os.environ["PATH"])
    skills = home / "skills"
    skills.mkdir()
    build.sync_skill(checkout, skills)
    runtime = maintenance.Runtime(home)
    maintenance.bootstrap_begin(runtime, home / "legacy/tools/llm-bus/uv-receipt.toml", skills)
    mailbox = home / ".local/share/llm-bus/bus.sqlite3"
    if selected == "fresh-nested":
        Store(mailbox)
        mailbox = home / "missing/a/b/bus.sqlite3"
        monkeypatch.setenv("LLM_BUS_DB", str(mailbox))
    if selected not in {"fresh", "fresh-nested"}:
        if selected != "schema0":
            Store(mailbox)
        else:
            mailbox.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(mailbox)) as database:
            if selected != "schema0":
                if previous_schema == 18:
                    for name in ("messages_age_insert", "messages_age_update"):
                        database.execute(f"DROP TRIGGER {name}")
                    database.execute("DROP INDEX messages_retention")
                    database.execute("ALTER TABLE messages DROP COLUMN retention_at")
                database.execute(f"PRAGMA user_version={previous_schema}")
                database.execute(
                    "CREATE TABLE replay_guard (uuid TEXT PRIMARY KEY, high_water INTEGER)"
                )
                database.execute("INSERT INTO replay_guard VALUES ('keep-uuid', 123)")
                database.commit()
        mailbox.chmod(0o600)
    inventory = home / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "mailboxes": (
                    [str(home / ".local/share/llm-bus/bus.sqlite3"), str(mailbox)]
                    if selected == "fresh-nested"
                    else [str(mailbox)]
                ),
                "processes": [],
                "restart_sources": [],
                "version": 1,
                "complete": True,
                "raw_clients_stopped": True,
                "hosts_gated": True,
            }
        )
    )
    inventory.chmod(0o600)
    maintenance.hold_begin(runtime, inventory)
    return Managed(home, checkout, runtime, public, mailbox)


def _fixture_handles(names: list[str]) -> list[dict[str, object]]:
    return [{"path": name, "fixture": True} for name in names]


@dataclass
class Supervisor:
    source: dict[str, object]
    current: dict[str, object]
    prior: dict[str, object]
    calls: list[str]
    runtime: maintenance.Runtime
    interrupt: str | None = None

    def inspect(self, source: dict[str, object]) -> dict[str, object]:
        assert source == self.source
        return dict(self.current)

    def job(self, identity: str) -> dict[str, object]:
        assert identity == self.source["identity"]
        return dict(self.current)

    def control(self, argv: list[str]) -> None:
        action = argv[2] if argv[0] == "systemctl" else argv[1]
        self.calls.append(action)
        if action not in {"mask", "disable", "bootout"}:
            published = self.runtime.read()
            assert published["phase"] == "active"
            assert published["active"] == published["candidate"]
        if argv[0] == "systemctl":
            states: dict[str, dict[str, object]] = {
                "mask": {
                    "LoadState": "masked",
                    "ActiveState": "inactive",
                    "UnitFileState": "masked",
                },
                "unmask": {**self.prior, "ActiveState": "inactive"},
                "start": {**self.current, "ActiveState": "active"},
            }
            self.current = states[action]
        else:
            self.launchd(action, argv)
        if self.interrupt == action:
            self.interrupt = None
            raise RuntimeError("controller died after restart mutation")

    def launchd(self, action: str, argv: list[str]) -> None:
        if action == "disable":
            self.current["disabled"] = True
        elif action == "bootout":
            self.current = {"disabled": True, "loaded": False, "plist": None, "running": False}
        elif action == "enable":
            self.current["disabled"] = False
        elif action == "bootstrap":
            assert argv[-1] == self.prior["plist"]
            self.current = {**self.prior, "running": self.prior["starts_on_load"]}
        elif action == "kickstart":
            self.current["running"] = True
        else:
            pytest.fail(f"Unexpected supervisor action: {argv}")


def _supervisor(
    managed: Managed, monkeypatch: pytest.MonkeyPatch, kind: str, *, running: bool, enabled: bool
) -> Supervisor:
    # pylint: disable=clean-code-boolean-flag-argument
    if kind == "systemd-user":
        prior: dict[str, object] = {
            "LoadState": "loaded",
            "ActiveState": "active" if running else "inactive",
            "UnitFileState": "enabled" if enabled else "disabled",
        }
    else:
        plist = managed.home / "test.bus.plist"
        plist.write_bytes(b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>')
        plist.chmod(0o600)
        prior = {
            "disabled": not enabled,
            "loaded": True,
            "running": running,
            "plist": str(plist),
            "plist_sha256": hashlib.sha256(plist.read_bytes()).hexdigest(),
            "starts_on_load": False,
        }
    source: dict[str, object] = {
        "kind": kind,
        "identity": "test.bus.service" if kind == "systemd-user" else "test.bus",
    }
    supervisor = Supervisor(source, dict(prior), prior, [], managed.runtime)
    monkeypatch.setattr(maintenance, "_source_state", supervisor.inspect)
    monkeypatch.setattr(maintenance, "_control", supervisor.control)
    monkeypatch.setattr(maintenance, "_launchd_job", supervisor.job)
    inventory = managed.home / "inventory.json"
    data = cast("dict[str, object]", json.loads(inventory.read_text()))
    data["restart_sources"] = [source]
    inventory.write_text(json.dumps(data))
    maintenance.hold_begin(managed.runtime, inventory)
    return supervisor


@pytest.mark.parametrize(
    ("kind", "running", "enabled"),
    [
        ("systemd-user", True, True),
        ("systemd-user", False, False),
        ("systemd-user", False, True),
        ("launchd", True, True),
        ("launchd", False, True),
    ],
)
def test_release_restores_exact_prior_restart_state_after_publication(
    managed: Managed, monkeypatch: pytest.MonkeyPatch, kind: str, *, running: bool, enabled: bool
) -> None:
    # pylint: disable=clean-code-boolean-flag-argument
    supervisor = _supervisor(managed, monkeypatch, kind, running=running, enabled=enabled)
    managed.install()
    incompatible = managed.state()
    incompatible["baseline_schemas"] = {str(managed.mailbox): 18}
    managed.runtime.write(incompatible)
    before = list(supervisor.calls)
    with pytest.raises(BusError, match="Schema activation requires"):
        generations.resume(managed.home)
    assert managed.state()["phase"] == "held"
    assert supervisor.calls == before
    compatible = managed.state()
    compatible["baseline_schemas"] = {str(managed.mailbox): 21}
    managed.runtime.write(compatible)
    result = generations.resume(managed.home)
    assert result["phase"] == "active"
    assert result["restart_restore_pending"] == []
    assert supervisor.current == supervisor.prior
    assert ("start" in supervisor.calls or "kickstart" in supervisor.calls) is running
    calls = list(supervisor.calls)
    maintenance.restore_sources(managed.runtime, managed.state())
    assert supervisor.calls == calls


@pytest.mark.parametrize(
    ("kind", "action"),
    [
        ("systemd-user", "unmask"),
        ("systemd-user", "start"),
        ("launchd", "enable"),
        ("launchd", "bootstrap"),
        ("launchd", "kickstart"),
    ],
)
def test_restart_restoration_retries_durable_intent_without_repeating_mutation(
    managed: Managed, monkeypatch: pytest.MonkeyPatch, kind: str, action: str
) -> None:
    supervisor = _supervisor(managed, monkeypatch, kind, running=True, enabled=True)
    managed.install()
    supervisor.interrupt = action
    with pytest.raises(RuntimeError, match="controller died"):
        generations.resume(managed.home)
    assert managed.state()["phase"] == "active"
    assert managed.state()["step"] == "restart-restore-intent"
    saved = managed.runtime.journal.read_bytes()
    calls = list(supervisor.calls)
    with pytest.raises(maintenance.MaintenanceError, match="Restart restoration unfinished"):
        maintenance.hold_begin(managed.runtime)
    for repair in (False, True):
        with pytest.raises(maintenance.MaintenanceError, match="Restart restoration unfinished"):
            managed.install(repair=repair)
    assert managed.runtime.journal.read_bytes() == saved
    assert supervisor.calls == calls
    if action == "unmask":
        expected = dict(supervisor.current)
        supervisor.current["UnitFileState"] = "disabled"
        calls = list(supervisor.calls)
        pending = generations.resume(managed.home)
        assert pending["step"] == "restart-restore-pending"
        assert supervisor.current["UnitFileState"] == "disabled"
        assert supervisor.calls == calls
        saved = managed.runtime.journal.read_bytes()
        with pytest.raises(maintenance.MaintenanceError, match="Restart restoration unfinished"):
            managed.install()
        assert managed.runtime.journal.read_bytes() == saved
        supervisor.current = expected  # Explicit owner reconciliation allows retry.
    result = generations.resume(managed.home)
    assert result["step"] == "released"
    assert supervisor.current == supervisor.prior
    assert supervisor.calls.count(action) == 1


def test_public_resume_and_status_expose_pending_owner_actions_without_bodies(
    managed: Managed, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory = managed.home / "inventory.json"
    state = cast("dict[str, object]", json.loads(inventory.read_text()))
    state["restart_sources"] = [
        {
            "kind": "assisted",
            "identity": "owner-bridge",
            "stopped": True,
            "restart_disabled": True,
        }
    ]
    inventory.write_text(json.dumps(state))
    maintenance.hold_begin(managed.runtime, inventory)
    managed.install()
    state = managed.state()
    state["private_payload"] = "must-not-be-public"
    managed.runtime.write(state)
    assert generations.operator("resume", str(managed.home), "{}") == 0
    resumed = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert resumed["phase"] == "active"
    assert resumed["step"] == "restart-restore-pending"
    assert "Owner must restore assisted restart source: owner-bridge" in str(
        resumed["restart_restore_pending"]
    )
    assert "Dispatch reopened" in str(resumed["next_step"])
    assert maintenance.operator_main(["status"], managed.runtime) == 0
    status = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert status == resumed
    assert "must-not-be-public" not in json.dumps(status)
    before = managed.runtime.journal.read_bytes()
    with pytest.raises(maintenance.MaintenanceError, match="Restart restoration unfinished"):
        maintenance.hold_begin(managed.runtime)
    assert managed.runtime.journal.read_bytes() == before
    evidence = managed.home / "restart-evidence.json"
    for payload in (
        {"operation": resumed["operation"], "assisted_restored": cast("list[str]", [])},
        {"operation": "wrong-operation", "assisted_restored": ["owner-bridge"]},
        {"operation": resumed["operation"], "assisted_restored": ["other-source"]},
        {"operation": resumed["operation"], "assisted_restored": ["owner-bridge"] * 2},
        {**cast("dict[str, object]", resumed["restart_restore_evidence"]), "extra": True},
    ):
        evidence.write_text(json.dumps(payload))
        evidence.chmod(0o600)
        response = managed.invoke("maintenance", "resume", "--evidence", str(evidence))
        assert response.returncode == 1
        assert "exact operation and all pending assisted sources" in response.stderr
        assert managed.runtime.journal.read_bytes() == before
    evidence.write_text(json.dumps(resumed["restart_restore_evidence"]))
    response = managed.invoke("maintenance", "resume", "--evidence", str(evidence))
    assert response.returncode == 0, response.stderr
    completed = cast("dict[str, object]", json.loads(response.stdout))
    assert completed["step"] == "released"
    assert completed["restart_restore_pending"] == []
    before = managed.runtime.journal.read_bytes()
    assert managed.invoke("maintenance", "resume", "--evidence", str(evidence)).returncode == 1
    assert managed.runtime.journal.read_bytes() == before
    # The owner stops the assisted source again and submits a fresh inventory.
    assert maintenance.hold_begin(managed.runtime, inventory)["phase"] == "held"


@pytest.mark.parametrize(
    "target",
    ["settings-file", "settings-parent", "settings-ancestor", "skill-file", "skill-parent"],
)
def test_integration_sync_failure_keeps_dispatch_held_until_verified_retry(
    managed: Managed, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    managed.install()
    setting = managed.home / "custom codex/hooks.json"
    before = setting.read_bytes()
    skill = managed.home / "skills/llm-bus/SKILL.md"
    selected = {
        "settings-file": setting,
        "settings-parent": setting.parent,
        "settings-ancestor": setting.parent.parent,
        "skill-file": skill,
        "skill-parent": skill.parent.parent,
    }[target].stat()
    actual_sync = os.fsync
    attempts = 0
    synced: list[Path] = []
    sync_directory = files.sync_directory

    def sync_settings(directory: Path) -> None:
        synced.append(directory)
        sync_directory(directory)

    def fail_selected(descriptor: int) -> None:
        nonlocal attempts
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (selected.st_dev, selected.st_ino):
            attempts += 1
            raise OSError("injected integration durability failure")
        actual_sync(descriptor)

    monkeypatch.setattr(files, "sync_directory", sync_settings)
    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", fail_selected)
        for _ in range(2):
            with pytest.raises(OSError, match="integration durability failure"):
                generations.resume(managed.home)
            assert managed.state()["phase"] == "held"
            assert setting.read_bytes() == before
    assert attempts == 2
    assert managed.state()["phase"] == "held"
    response = managed.invoke("list")
    assert (response.returncode, response.stdout) == (75, APPROVED)
    synced.clear()
    assert generations.resume(managed.home)["step"] == "released"
    if target.startswith("settings-"):
        assert all(parent in synced for parent in setting.parents)
    assert setting.read_bytes() == before


def test_settings_and_skill_writers_sync_contents_before_directory_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "hooks.json"
    settings.write_bytes(b"old")
    fresh = tmp_path / "scratch/fresh"
    fresh.mkdir(parents=True)
    (fresh / "SKILL.md").write_bytes(b"accepted skill")
    skill_inode = (fresh / "SKILL.md").stat().st_ino
    destination = tmp_path / "llm-bus"
    destination.mkdir()
    (destination / "SKILL.md").write_bytes(b"old skill")
    events: list[str] = []
    actual_sync = os.fsync
    actual_replace, actual_rename = Path.replace, Path.rename

    def sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if info.st_size == len(b"accepted settings"):
            events.append("settings-file")
        if info.st_ino == skill_inode:
            events.append("skill-file")
        actual_sync(descriptor)

    def replace(path: Path, target: Path) -> Path:
        assert "settings-file" in events
        events.append("settings-replace")
        return actual_replace(path, target)

    def rename(path: Path, target: Path) -> Path:
        assert "skill-file" in events
        events.append("skill-rename")
        return actual_rename(path, target)

    def directory(path: Path) -> None:
        events.append(f"directory:{path}")
        maintenance.sync_directory(path)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(files, "sync_directory", directory)
    monkeypatch.setattr(build, "sync_directory", directory)
    files.atomic_write(settings, b"accepted settings", 0o600, b"old")
    settings_commit = events.index("settings-replace")
    assert events[settings_commit + 1] == f"directory:{tmp_path}"
    assert build.swap_skill(fresh, destination, tmp_path / "scratch/previous") is True
    assert events[-2:] == [f"directory:{destination}", f"directory:{tmp_path}"]
    assert settings.read_bytes() == b"accepted settings"
    assert (destination / "SKILL.md").read_bytes() == b"accepted skill"


def test_resume_preserves_owner_changed_source_instead_of_resuspending_or_starting(
    managed: Managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(managed, monkeypatch, "systemd-user", running=True, enabled=True)
    managed.install()
    supervisor.current = {
        **supervisor.prior,
        "ActiveState": "inactive",
        "UnitFileState": "disabled",
    }
    before = dict(supervisor.current)
    calls = list(supervisor.calls)
    with pytest.raises(maintenance.MaintenanceError, match="source changed"):
        generations.resume(managed.home)
    assert managed.state()["phase"] != "active"
    assert supervisor.current == before
    assert supervisor.calls == calls


@pytest.mark.parametrize("kind", ["systemd-user", "launchd"])
def test_native_restart_commands_round_trip_through_fake_executable(
    managed: Managed, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    managed.install()
    plist = managed.home / "native.bus.plist"
    plist.write_bytes(plistlib.dumps({"Label": "native.bus"}))
    plist.chmod(0o644)
    source: dict[str, object] = {
        "kind": kind,
        "identity": "native.bus.service" if kind == "systemd-user" else "native.bus",
    }
    prior: dict[str, object] = (
        {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}
        if kind == "systemd-user"
        else {"disabled": False, "loaded": True, "running": True, "plist": str(plist)}
    )
    supervisor_state = managed.home / "native-supervisor.json"
    supervisor_state.write_text(
        json.dumps(
            {"prior": prior, "current": prior, "calls": [], "journal": str(managed.runtime.journal)}
        )
    )
    executable = managed.home / "native-supervisor"
    executable.write_text(
        f"#!{sys.executable}\n"
        r"""
import json
import os
from pathlib import Path
import sys
path = Path(os.environ["TEST_SUPERVISOR_STATE"])
data = json.loads(path.read_text())
current, prior = data["current"], data["prior"]
arguments = sys.argv[1:]
action = arguments[1] if arguments[0] == "--user" else arguments[0]
if action == "show":
    print("\n".join(f"{key}={value}" for key, value in current.items()))
elif action == "print-disabled":
    print('"native.bus" => ' + str(current["disabled"]).lower())
elif action == "print":
    if not current["loaded"]:
        sys.stderr.write("Could not find service")
        sys.exit(113)
    print("path = " + current["plist"])
    print("state = " + ("running" if current["running"] else "waiting"))
else:
    if action not in {"mask", "disable", "bootout"}:
        assert json.loads(Path(data["journal"]).read_text())["phase"] == "active"
    data["calls"].append(arguments)
    if action == "mask":
        current = {"LoadState": "masked", "ActiveState": "inactive", "UnitFileState": "masked"}
    elif action == "unmask":
        current = {**prior, "ActiveState": "inactive"}
    elif action == "start":
        current["ActiveState"] = "active"
    elif action == "disable":
        current["disabled"] = True
    elif action == "bootout":
        current = {"disabled": True, "loaded": False, "running": False, "plist": None}
    elif action == "enable":
        current["disabled"] = False
    elif action == "bootstrap":
        assert arguments[-1] == prior["plist"]
        current = {**prior, "running": False}
    elif action == "kickstart":
        current["running"] = True
    else:
        raise AssertionError(arguments)
    data["current"] = current
    path.write_text(json.dumps(data))
"""
    )
    executable.chmod(0o700)
    native_run = subprocess.run

    def run(
        arguments: list[str], *, capture_output: bool, text: bool, check: bool, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        command = (
            [str(executable), *arguments[1:]]
            if arguments[0] in {"systemctl", "/bin/launchctl"}
            else arguments
        )
        return native_run(
            command, capture_output=capture_output, text=text, check=check, timeout=timeout
        )

    monkeypatch.setenv("TEST_SUPERVISOR_STATE", str(supervisor_state))
    monkeypatch.setattr(maintenance, "subprocess", SimpleNamespace(run=run))
    monkeypatch.setattr(sys, "platform", "linux" if kind == "systemd-user" else "darwin")
    inventory = managed.home / "inventory.json"
    data = cast("dict[str, object]", json.loads(inventory.read_text()))
    data["restart_sources"] = [source]
    inventory.write_text(json.dumps(data))
    maintenance.hold_begin(managed.runtime, inventory)
    assert generations.resume(managed.home)["restart_restore_pending"] == []
    data = cast("dict[str, object]", json.loads(supervisor_state.read_text()))
    assert data["current"] == prior
    calls = cast("list[list[str]]", data["calls"])
    if kind == "systemd-user":
        assert calls == [
            ["--user", "mask", "--now", "native.bus.service"],
            ["--user", "unmask", "native.bus.service"],
            ["--user", "start", "native.bus.service"],
        ]
    else:
        target = f"gui/{os.getuid()}/native.bus"
        assert calls == [
            ["disable", target],
            ["bootout", target],
            ["enable", target],
            ["bootstrap", f"gui/{os.getuid()}", str(plist)],
            ["kickstart", target],
        ]
        maintenance.hold_begin(managed.runtime, inventory)
        plist.write_bytes(plistlib.dumps({"Label": "native.bus", "OwnerChange": True}))
        data = generations.resume(managed.home)
        assert data["step"] == "restart-restore-pending"
        assert "definition changed" in str(data["restart_restore_pending"])
        assert plist.read_bytes() == plistlib.dumps({"Label": "native.bus", "OwnerChange": True})
        data = cast("dict[str, object]", json.loads(supervisor_state.read_text()))
        calls = cast("list[list[str]]", data["calls"])
        assert sum(item[0] == "bootstrap" for item in calls) == 1
        assert sum(item[0] == "kickstart" for item in calls) == 1


@pytest.mark.parametrize("managed", ["collab"], indirect=True)
def test_real_generation_hold_resume_same_revision_repair_and_removal(managed: Managed) -> None:
    result = managed.install()
    assert result["phase"] == "held"
    assert managed.invoke("list").stdout == APPROVED
    assert managed.invoke("hook", "codex").returncode == 0
    state = generations.resume(managed.home)
    assert state["phase"] == "active"
    assert "usage:" in managed.invoke("--help").stdout
    for field in ("receipt", "payloads", "root"):
        damaged = managed.state()
        del cast("dict[str, object]", damaged["active"])[field]
        managed.runtime.write(damaged)
        assert managed.invoke("list").stdout == APPROVED
        state = generations.resume(managed.home)
    first = cast("dict[str, object]", state["active"])
    assert first["extras"] == ["collab"]
    assert generations.digest(Path(str(first["receipt"]))) == first["receipt_sha256"]
    assert "llm-bus v0.1.0" in _run(
        [UV, "tool", "list"], env=generations.generation_environment(first)
    )
    Path(str(first["executable"])).write_text("broken candidate\n")
    skill = managed.home / "skills/llm-bus/SKILL.md"
    expected_skill = skill.read_bytes()
    skill.write_text("broken skill\n")
    settings = managed.home / "custom codex/hooks.json"
    configured = cast("dict[str, dict[str, object]]", json.loads(settings.read_text()))
    del configured["hooks"]["SessionEnd"]
    settings.write_text(json.dumps(configured))
    assert managed.invoke("list").stdout == APPROVED
    repaired = managed.install(repair=True)
    assert repaired["reinstalled"] is True
    assert repaired["phase"] == "active"
    assert repaired["binary"] != first["executable"]
    assert "usage:" in managed.invoke("--help").stdout
    assert skill.read_bytes() == expected_skill
    assert "SessionEnd" in json.loads(settings.read_text())["hooks"]
    reused = managed.install()
    assert reused["reinstalled"] is False
    assert reused["binary"] == repaired["binary"]
    active = cast("dict[str, object]", managed.state()["active"])
    _run([UV, "tool", "uninstall", "llm-bus"], env=generations.generation_environment(active))
    assert managed.invoke("list").stdout == APPROVED
    assert managed.state()["active"] == active
    assert managed.install(repair=True)["phase"] == "active"
    assert "usage:" in managed.invoke("--help").stdout


def test_candidate_install_never_replaces_public_gate(
    managed: Managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run = build.run
    real_hold = maintenance.hold_begin
    installs = 0

    def held(
        runtime: maintenance.Runtime,
        inventory_path: Path | None = None,
        *,
        acquire_controller: bool = True,
    ) -> dict[str, object]:
        state = real_hold(runtime, inventory_path, acquire_controller=acquire_controller)
        with (
            pytest.raises(maintenance.MaintenanceError, match="Another maintenance controller"),
            maintenance.Runtime(managed.home).controller(),
        ):
            pytest.fail("Controller lock released between hold and installation")
        return state

    def observe(
        argv: list[str], *, env: dict[str, str] | None = None, label: str | None = None
    ) -> str:
        nonlocal installs
        if Path(argv[0]).name == "uv" and argv[1:3] == ["tool", "install"]:
            installs += 1
            result = managed.invoke("--format", "agent", "list")
            assert result.stdout == APPROVED
            assert result.returncode == 75
        return real_run(argv, env=env, label=label)

    monkeypatch.setattr(build, "run", observe)
    monkeypatch.setattr(maintenance, "hold_begin", held)
    managed.install()
    assert installs == 2
    assert managed.state()["phase"] == "held"


def test_corrupt_payload_keeps_hold_and_compatible_previous_recovers(managed: Managed) -> None:
    managed.install()
    state = managed.state()
    candidate = cast("dict[str, object]", state["candidate"])
    payload = next((Path(str(candidate["tools"])) / "llm-bus/lib").rglob("llm_bus/errors.py"))
    payload.write_text("corrupt payload\n")
    with pytest.raises(BusError, match="payload differs"):
        generations.resume(managed.home)
    assert managed.invoke("list").stdout == APPROVED
    previous = str(state["previous_generation"])
    recovered = generations.recover(managed.home, previous)
    assert recovered["phase"] == "held"
    assert cast("dict[str, object]", recovered["candidate"])["id"] == previous
    assert generations.resume(managed.home)["phase"] == "active"


@pytest.mark.parametrize("managed", [18], indirect=True)
def test_schema_activation_requires_bound_owner_evidence(managed: Managed) -> None:
    managed.install()
    state = managed.state()
    before = managed.mailbox.read_bytes()
    with pytest.raises(BusError, match="schema differs"):
        generations.resume(managed.home)
    assert managed.mailbox.read_bytes() == before
    previous = str(state["previous_generation"])
    assert generations.recover(managed.home, previous)["phase"] == "held"
    assert generations.resume(managed.home)["phase"] == "active"
    managed.install(repair=True)
    assert (
        generations.candidate_operation(managed.home, "preview", str(managed.mailbox))["phase"]
        == "held"
    )
    assert (
        generations.candidate_operation(managed.home, "migrate", str(managed.mailbox))["phase"]
        == "held"
    )
    verified = generations.candidate_operation(managed.home, "verify", str(managed.mailbox))
    assert verified["mailboxes"] == {str(managed.mailbox): 21}
    assert managed.state()["step"] == "verify-complete"
    with pytest.raises(BusError, match="--evidence"):
        generations.resume(managed.home)
    candidate = cast("dict[str, object]", managed.state()["candidate"])
    evidence = managed.home / "cutover.json"
    evidence.write_text(
        json.dumps(
            {
                "operation": managed.state()["operation"],
                "wheel_sha256": candidate["wheel_sha256"],
                "mailboxes": {str(managed.mailbox): 21},
                "compatible_floor": candidate["revision"],
                "local_schema_verified": True,
                "relay_drained": True,
                "first_deletion_approved": True,
                "retention_preview_verified": True,
                "replay_high_water_verified": True,
            }
        )
    )
    evidence.chmod(0o600)
    assert generations.resume(managed.home, evidence)["phase"] == "active"
    with pytest.raises(BusError, match="schema differs"):
        generations.recover(managed.home, previous)
    assert managed.invoke("list").stdout == APPROVED
    with closing(sqlite3.connect(managed.mailbox)) as database:
        assert database.execute("SELECT * FROM replay_guard").fetchall() == [("keep-uuid", 123)]


@pytest.mark.parametrize("managed", ["fresh-nested"], indirect=True)
def test_fresh_initialization_retries_visible_parent_durability_barrier(
    managed: Managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert managed.install()["phase"] == "held"
    assert managed.state()["baseline_absent"] == [str(managed.mailbox)]
    ancestor = managed.home / "missing"
    assert not ancestor.exists()
    sync_directory = maintenance.sync_directory
    synced: list[tuple[Path, bool]] = []
    fail = True

    def sync_parent(path: Path) -> None:
        synced.append((path, managed.mailbox.exists()))
        if fail and path == ancestor:
            raise OSError("ancestor sync failed")
        sync_directory(path)

    monkeypatch.setattr(maintenance, "sync_directory", sync_parent)
    for _ in range(2):
        with pytest.raises(OSError, match="ancestor sync failed"):
            generations.candidate_operation(managed.home, "migrate", str(managed.mailbox))
        assert managed.mailbox.parent.is_dir()
        assert not managed.mailbox.exists()
        assert managed.state()["phase"] == "held"
        assert managed.state()["step"] == "migrate-parent-intent"
        assert str(managed.mailbox) not in cast(
            "dict[str, object]", managed.state().get("fresh_initializations", {})
        )
        assert synced[-1] == (ancestor, False)

    fail = False
    synced.clear()
    assert (
        generations.candidate_operation(managed.home, "migrate", str(managed.mailbox))["phase"]
        == "held"
    )
    assert all((parent, False) in synced for parent in managed.mailbox.parents)
    assert (managed.mailbox.parent, True) in synced
    receipt = cast("dict[str, object]", managed.state()["fresh_initializations"])[
        str(managed.mailbox)
    ]
    assert cast("dict[str, object]", receipt)["status"] == "complete"
    assert generations.resume(managed.home)["phase"] == "active"
    assert managed.invoke("retention-preview").returncode == 0


@pytest.mark.parametrize("managed", ["fresh", "schema0"], indirect=True)
def test_fresh_initialization_requires_proven_absence_and_bound_output(managed: Managed) -> None:
    absent = not managed.mailbox.exists()
    assert managed.install()["phase"] == "held"
    state = managed.state()
    assert state["baseline_schemas"] == {str(managed.mailbox): 0}
    assert state["baseline_absent"] == ([str(managed.mailbox)] if absent else [])
    with pytest.raises(BusError, match="schema differs"):
        generations.resume(managed.home)
    assert (
        generations.candidate_operation(managed.home, "migrate", str(managed.mailbox))["phase"]
        == "held"
    )
    assert generations.candidate_operation(managed.home, "verify", str(managed.mailbox))[
        "mailboxes"
    ] == {str(managed.mailbox): 21}
    if not absent:
        with pytest.raises(BusError, match="--evidence"):
            generations.resume(managed.home)
        assert managed.state()["phase"] == "held"
        return

    completed = managed.state()
    changes: tuple[tuple[str, object], ...] = (
        ("operation", "different-operation"),
        ("fresh_initializations", {}),
        ("baseline_schemas", {}),
        ("baseline_schemas", {str(managed.mailbox): 18}),
        ("baseline_schemas", {str(managed.mailbox): 0, "unknown.sqlite3": 18}),
        ("baseline_absent", []),
    )
    for key, value in changes:
        managed.runtime.write({**completed, key: value})
        with pytest.raises(BusError, match="hold retained"):
            generations.resume(managed.home)
        assert managed.state()["phase"] == "held"
    receipts = cast("dict[str, object]", completed["fresh_initializations"])
    receipt = cast("dict[str, object]", receipts[str(managed.mailbox)])
    for change in ({"status": "intent"}, {"wheel_sha256": "0" * 64}, {"schema_range": [18, 21]}):
        managed.runtime.write(
            {**completed, "fresh_initializations": {str(managed.mailbox): {**receipt, **change}}}
        )
        with pytest.raises(BusError, match="identity/evidence changed"):
            generations.resume(managed.home)
    managed.runtime.write(completed)
    original = managed.mailbox.with_suffix(".original")
    managed.mailbox.rename(original)
    shutil.copyfile(original, managed.mailbox)
    managed.mailbox.chmod(0o600)
    with pytest.raises(BusError, match="identity/evidence changed"):
        generations.resume(managed.home)
    managed.mailbox.unlink()
    original.rename(managed.mailbox)

    migrated = managed.mailbox.with_name("existing.sqlite3")
    Store(migrated)
    mixed = {
        **completed,
        "mailboxes": [str(managed.mailbox), str(migrated)],
        "baseline_schemas": {str(managed.mailbox): 0, str(migrated): 18},
        "access_inventory": {
            **cast("dict[str, object]", completed["access_inventory"]),
            "mailboxes": [str(managed.mailbox), str(migrated)],
        },
    }
    managed.runtime.write(mixed)
    with pytest.raises(BusError, match="--evidence"):
        generations.resume(managed.home)
    managed.runtime.write(completed)
    assert generations.resume(managed.home)["phase"] == "active"
    assert "cutover_evidence" not in managed.state()
    assert managed.invoke("retention-preview").returncode == 0
    with closing(sqlite3.connect(managed.mailbox)) as database:
        assert database.execute("PRAGMA user_version").fetchone() == (21,)
        assert database.execute("SELECT count(*) FROM messages").fetchone() == (0,)
    assert generations.recover(managed.home)["phase"] == "held"
    assert (managed.state()["fresh_initializations"], managed.state()["baseline_absent"]) == (
        {},
        [],
    )
    assert generations.resume(managed.home)["phase"] == "active"


def test_uninstall_preserves_mailbox_policy_and_unrelated_settings(managed: Managed) -> None:
    managed.install()
    settings = managed.home / "custom codex/hooks.json"
    value = cast("dict[str, object]", json.loads(settings.read_text()))
    value["unrelated"] = {"keep": True}
    settings.write_text(json.dumps(value))
    policy = managed.home / ".local/share/llm-bus/machine-policy.json"
    policy.write_text("keep policy\n")
    before = managed.mailbox.read_bytes()
    assert generations.uninstall(managed.home)["installed"] is False
    assert managed.mailbox.read_bytes() == before
    assert policy.read_text() == "keep policy\n"
    assert json.loads(settings.read_text())["unrelated"] == {"keep": True}
    assert not managed.public.exists()
    retired = managed.state()
    # Install a wheel through uv again, then use only supported enrollment commands.
    wheel = Path(str(cast("dict[str, object]", retired["candidate"])["wheel"]))
    _run([UV, "tool", "install", "--python", str(Path(sys.executable).resolve()), str(wheel)])
    bootstrap = managed.invoke("install", "--skills-dir", str(managed.home / "skills"))
    assert bootstrap.returncode == 1
    assert "complete maintenance begin" in bootstrap.stderr
    assert managed.invoke("list").stdout == APPROVED
    history = managed.runtime.root / "retired" / f"{retired['operation']}.json"
    assert json.loads(history.read_text()) == retired
    # A new inventory is required; old stopped-access evidence is not reused.
    assert managed.invoke("maintenance", "begin").returncode == 1
    maintenance.hold_begin(managed.runtime, managed.home / "inventory.json")
    if sys.platform == "linux" and os.environ.get("LLM_BUS_TEST_HANDLE_FIXTURE") == "1":
        # Only this continuation uses the existing in-process Linux handle fixture.
        assert managed.install()["phase"] == "held"
    else:
        assert managed.invoke("install").returncode == 0
    assert generations.resume(managed.home)["phase"] == "active"
    assert "usage:" in managed.invoke("--help").stdout
    assert managed.mailbox.read_bytes() == before
    assert policy.read_text() == "keep policy\n"
    assert json.loads(settings.read_text())["unrelated"] == {"keep": True}


def test_interrupted_uninstall_recovers_without_reopening(
    managed: Managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed.install()
    original = build.run

    def interrupt(
        argv: list[str], *, env: dict[str, str] | None = None, label: str | None = None
    ) -> str:
        output = original(argv, env=env, label=label)
        if argv[1:3] == ["tool", "uninstall"]:
            raise RuntimeError("controller interrupted after uv removal")
        return output

    before = managed.mailbox.read_bytes()
    with monkeypatch.context() as fault:
        fault.setattr(build, "run", interrupt)
        with pytest.raises(RuntimeError, match="controller interrupted"):
            generations.uninstall(managed.home)
    assert managed.invoke("list").stdout == APPROVED
    with pytest.raises(BusError, match="Finish recorded uninstall"):
        managed.install()
    recovered = generations.recover(managed.home)
    assert (recovered["phase"], recovered["step"]) == ("held", "uninstalled")
    assert managed.mailbox.read_bytes() == before
    assert not managed.public.exists()


def test_dirty_checkout_and_symlink_skill_refused_before_install(managed: Managed) -> None:
    dirty = managed.checkout / ".python-version"
    original = dirty.read_bytes()
    try:
        dirty.write_text("3.13\n")
        with pytest.raises(BusError, match="commit or stash"):
            managed.install()
    finally:
        dirty.write_bytes(original)
    skill = managed.home / "skills/llm-bus"
    saved = managed.home / "original-skill"
    skill.rename(saved)
    skill.symlink_to(saved)
    with pytest.raises(BusError, match="real directory"):
        managed.install()


def test_install_creates_missing_initial_skill_copy(
    managed: Managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = managed.home / "skills/llm-bus"
    shutil.rmtree(skill)
    monkeypatch.setenv("CODEX_HOME", str(managed.home / "new codex"))
    assert managed.install()["phase"] == "held"
    assert (skill / "SKILL.md").is_file()
    assert (managed.home / "new codex/hooks.json").is_file()
    homes = cast(
        "dict[str, list[str]]",
        json.loads((managed.home / ".local/share/llm-bus/native-hosts.json").read_text()),
    )
    assert str(managed.home / "new codex") in homes["codex_homes"]
    assert str(managed.home / "custom codex") in homes["codex_homes"]
    assert generations.resume(managed.home)["phase"] == "active"


@pytest.mark.parametrize(
    "transition",
    [
        "retain-previous-intent",
        "build-intent",
        "install-intent",
        "candidate-verified",
        "integrations-intent",
        "integrations-complete",
        "publish-intent",
        "skill-rename",
        "hook-alias",
    ],
)
def test_controller_death_retains_gate_and_reinstall_repairs(
    managed: Managed, transition: str
) -> None:
    code = """
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from llm_bus import generations, maintenance
home, checkout, transition = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
maintenance.process_inventory = list
if sys.platform == 'linux' and os.environ.get('LLM_BUS_TEST_HANDLE_FIXTURE') == '1':
    maintenance.mailbox_evidence = lambda names: [{'path': name, 'fixture': True} for name in names]
original_write = maintenance.Runtime.write
def write(self, state):
    original_write(self, state)
    if state.get('step') == transition:
        os.kill(os.getpid(), signal.SIGKILL)
maintenance.Runtime.write = write
original_rename = Path.rename
def rename(self, target):
    result = original_rename(self, target)
    if transition == 'skill-rename' and self == home / 'skills/llm-bus' and Path(target).name == 'previous':
        os.kill(os.getpid(), signal.SIGKILL)
    return result
Path.rename = rename
original_durable = maintenance.durable_write
def durable(path, content, mode=0o600):
    original_durable(path, content, mode)
    if transition == 'hook-alias' and path == maintenance.Runtime(home).root / 'llm-bus':
        os.kill(os.getpid(), signal.SIGKILL)
maintenance.durable_write = durable
generations.install_generation(checkout, home)
generations.resume(home)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            code,
            str(REPOSITORY / "src"),
            str(managed.home),
            str(managed.checkout),
            transition,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr
    assert managed.invoke("list").stdout == APPROVED
    assert managed.state()["phase"] in {"held", "verified"}
    snapshots = managed.state().get("integration_snapshots")
    assert managed.install(repair=True)["phase"] == "held"
    if snapshots is not None:
        assert managed.state()["integration_snapshots"] == snapshots
    assert generations.resume(managed.home)["phase"] == "active"


def test_managed_install_binds_antigravity_hooks_and_allow_rule(managed: Managed) -> None:
    hooks = managed.home / ".gemini/config/hooks.json"
    settings = managed.home / ".gemini/antigravity-cli/settings.json"
    hooks.parent.mkdir(parents=True)
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"agentMode": "auto"}))
    assert managed.install()["phase"] == "held"
    installed = cast("dict[str, dict[str, list[dict[str, str]]]]", json.loads(hooks.read_text()))
    assert set(installed["llm-bus"]) == {"SessionStart", "PreInvocation"}
    assert installed["llm-bus"]["PreInvocation"][0]["command"].endswith("llm-bus' hook agy")
    allowed = cast("dict[str, dict[str, list[str]]]", json.loads(settings.read_text()))
    assert allowed["permissions"]["allow"] == ["command(llm-bus)"]
    native = managed.home / ".local/share/llm-bus/native-hosts.json"
    assert str(hooks.parent) not in native.read_text()
    assert generations.resume(managed.home)["phase"] == "active"

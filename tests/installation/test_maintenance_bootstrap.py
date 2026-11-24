"""Executable-boundary regressions: no legacy imports, mailbox access, or gate gaps."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from llm_bus import maintenance

REPOSITORY = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPOSITORY / "tools/maintenance_upgrade.py"
APPROVED_MESSAGE = (
    "llm-bus is temporarily offline for maintenance. Retry in 1 minute, then every minute "
    "while this message appears. Stop retrying once the command responds normally.\n"
)


@dataclass(frozen=True)
class Legacy:
    home: Path
    public: Path
    cached: Path
    actual: Path
    receipt: Path
    mailbox: Path
    environment: dict[str, str]

    def bootstrap(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(BOOTSTRAP), *arguments],
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def begin(self) -> subprocess.CompletedProcess[str]:
        return self.bootstrap(
            "begin", "--receipt", str(self.receipt), "--skills-dir", str(self.home / "skills")
        )

    def invoke(self, executable: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(executable), *arguments],
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )


@pytest.fixture(name="legacy")
def legacy_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Legacy:
    home = tmp_path.resolve() / "home with spaces"
    public, cached, actual = (
        home / "public/llm-bus",
        home / "cached hook/llm-bus",
        home / "old uv/bin/llm-bus",
    )
    for path in (public, cached, actual):
        path.parent.mkdir(parents=True)
    actual.write_text('#!/bin/sh\nprintf legacy-mailbox-access > "$LLM_BUS_DB"\nexit 90\n')
    actual.chmod(0o755)
    public.symlink_to(actual)
    cached.symlink_to(actual)
    receipt = home / "old uv/uv-receipt.toml"
    receipt.write_text(
        '[tool]\nrequirements = [{ name = "llm-bus", path = "/accepted.whl" }]\n'
        f'entrypoints = [{{ name = "llm-bus", install-path = {json.dumps(str(public))} }}]\n'
    )
    settings = home / ".codex/hooks.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": shlex.join([str(cached), "hook", "codex"]),
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    poison = home / "poison/llm_bus"
    poison.mkdir(parents=True)
    (poison / "__init__.py").write_text('raise RuntimeError("OLD PACKAGE IMPORTED")\n')
    mailbox = home / "must-not-exist.sqlite3"
    environment = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{public.parent}:/usr/bin:/bin",
        "PYTHONPATH": str(poison.parent),
        "LLM_BUS_DB": str(mailbox),
    }
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        environment.pop(key, None)
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PATH", environment["PATH"])
    return Legacy(home, public, cached, actual, receipt, mailbox, environment)


@pytest.mark.parametrize("output_format", ["json", "agent"])
def test_bootstrap_gates_cached_hooks_and_path_without_legacy_import(
    legacy: Legacy, output_format: str
) -> None:
    result = legacy.begin()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["phase"] == "preparing"
    for executable in (legacy.public, legacy.cached, legacy.actual):
        for arguments, expected_exit in (
            (["--format", output_format, "list"], 75),
            (["--format", output_format, "hook", "claude"], 0),
            (["--format", output_format, "hook", "codex"], 0),
            (["--format=" + output_format, "hook", "codex"], 0),
            (["send", "peer", "--body", "maintenance resume hook codex"], 75),
        ):
            response = legacy.invoke(executable, *arguments)
            assert (response.returncode, response.stdout, response.stderr) == (
                expected_exit,
                APPROVED_MESSAGE,
                "",
            )
    assert not legacy.mailbox.exists()
    runtime = maintenance.Runtime(legacy.home)
    assert runtime.read()["aliases_installed"] is True
    assert runtime.root.stat().st_mode & 0o777 == 0o700
    assert runtime.state_root.stat().st_mode & 0o777 == 0o700
    assert runtime.journal.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("form", ["export", "module"])
def test_new_python_entrypoints_gate_before_application_import(legacy: Legacy, form: str) -> None:
    assert legacy.begin().returncode == 0
    environment = {**legacy.environment, "PYTHONPATH": str(REPOSITORY / "src")}
    command = [sys.executable, "-m", "llm_bus", "hook", "codex"]
    if form == "export":
        command = [
            sys.executable,
            "-c",
            (
                "import sys, llm_bus; assert 'llm_bus.cli.app' not in sys.modules; "
                "result = llm_bus.main(['hook', 'codex']); "
                "assert 'llm_bus.cli.app' not in sys.modules; raise SystemExit(result)"
            ),
        ]
    result = subprocess.run(command, env=environment, text=True, capture_output=True, check=False)
    assert (result.returncode, result.stdout, result.stderr) == (0, APPROVED_MESSAGE, "")
    assert not legacy.mailbox.exists()


@pytest.mark.parametrize("broken", ["missing", "malformed", "missing-generation", "unsafe-mode"])
def test_incomplete_or_removed_managed_state_fails_closed(legacy: Legacy, broken: str) -> None:
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    if broken == "missing":
        runtime.journal.unlink()
    elif broken == "malformed":
        runtime.journal.write_text("{incomplete")
    elif broken == "unsafe-mode":
        runtime.state_root.chmod(0o755)
    else:
        state = runtime.read()
        state.update(
            {"phase": "active", "active": {"executable": "/missing/generation", "sha256": "0" * 64}}
        )
        runtime.write(state)
    response = legacy.invoke(legacy.cached, "hook", "claude")
    assert (response.returncode, response.stdout, response.stderr) == (0, APPROVED_MESSAGE, "")
    assert not legacy.mailbox.exists()


@pytest.mark.parametrize("layout", ["hardlink", "unregistered-copy"])
def test_unsupported_aliases_block_before_any_executable_change(
    legacy: Legacy, layout: str
) -> None:
    original = legacy.actual.read_bytes()
    if layout == "hardlink":
        (legacy.home / "another-hardlink").hardlink_to(legacy.actual)
    else:
        legacy.cached.unlink()
        legacy.cached.write_bytes(original)
        legacy.cached.chmod(0o755)
    result = legacy.begin()
    assert result.returncode == 1
    assert legacy.actual.read_bytes() == original
    assert legacy.public.is_symlink()
    assert not maintenance.Runtime(legacy.home).journal.exists()
    assert not legacy.mailbox.exists()


def test_declared_service_copy_is_gated_and_original_bytes_are_retained(legacy: Legacy) -> None:
    service = legacy.home / "service-client"
    content = legacy.actual.read_bytes()
    service.write_bytes(content)
    service.chmod(0o755)
    result = legacy.bootstrap(
        "begin",
        "--receipt",
        str(legacy.receipt),
        "--skills-dir",
        str(legacy.home / "skills"),
        "--alias",
        str(service),
    )
    assert result.returncode == 0, result.stderr
    response = legacy.invoke(service, "list")
    assert (response.returncode, response.stdout) == (75, APPROVED_MESSAGE)
    originals = cast(
        "list[dict[str, object]]", maintenance.Runtime(legacy.home).read()["originals"]
    )
    assert any(record["path"] == str(service) for record in originals)
    assert not legacy.mailbox.exists()


@pytest.mark.parametrize("transition", range(1, 8))
def test_interrupted_bootstrap_recovers_every_durable_switch(
    legacy: Legacy, monkeypatch: pytest.MonkeyPatch, transition: int
) -> None:
    runtime = maintenance.Runtime(legacy.home)
    actual_write = maintenance.durable_write
    count = 0

    def fail_after_switch(path: Path, content: bytes, mode: int = 0o600) -> None:
        nonlocal count
        actual_write(path, content, mode)
        count += 1
        if count == transition:
            raise OSError("injected controller death after durable switch")

    monkeypatch.setattr(maintenance, "durable_write", fail_after_switch)
    with pytest.raises(OSError, match="injected controller death"):
        maintenance.bootstrap_begin(runtime, legacy.receipt, legacy.home / "skills")
    assert runtime.read()["phase"] == "preparing"
    result = legacy.bootstrap("recover")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["phase"] == "preparing"
    assert runtime.read()["aliases_installed"] is True
    for executable in (legacy.public, legacy.cached, legacy.actual):
        response = legacy.invoke(executable, "list")
        assert (response.returncode, response.stdout) == (75, APPROVED_MESSAGE)
    assert not legacy.mailbox.exists()


def _retired_legacy_runtime(legacy: Legacy) -> tuple[maintenance.Runtime, dict[str, object]]:
    original = legacy.actual.read_bytes()
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    retired = runtime.read()
    # Two known runtime versions model the retained uninstall runtime and new wheel.
    for name, key in (("runtime.py", "runtime_sha256"), ("launcher", "launcher_sha256")):
        path = runtime.root / name
        path.write_bytes(path.read_bytes() + b"\n# previous installed version\n")
        retired[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    retired.update({"phase": "held", "step": "uninstalled", "uninstalling": True})
    runtime.write(retired)
    # A fresh uv enrollment supplies legacy aliases again; mailbox remains untouched.
    for alias in (legacy.public, legacy.cached, legacy.actual):
        alias.unlink()
    legacy.actual.write_bytes(original)
    legacy.actual.chmod(0o755)
    legacy.public.symlink_to(legacy.actual)
    legacy.cached.symlink_to(legacy.actual)
    return runtime, retired


@pytest.mark.parametrize("transition", ["journal.json", "runtime.py", "launcher"])
def test_changed_runtime_reenrollment_recovers_only_recorded_old_or_new_files(
    legacy: Legacy, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    runtime, retired = _retired_legacy_runtime(legacy)
    actual_write = maintenance.durable_write

    def interrupt(path: Path, content: bytes, mode: int = 0o600) -> None:
        actual_write(path, content, mode)
        if path.name == transition:
            raise OSError("interrupted changed-runtime enrollment")

    with monkeypatch.context() as fault:
        fault.setattr(maintenance, "durable_write", interrupt)
        with pytest.raises(OSError, match="changed-runtime enrollment"):
            maintenance.bootstrap_begin(runtime, legacy.receipt, legacy.home / "skills")
    pending = runtime.read()
    assert pending["phase"] == "preparing"
    assert pending["operation"] != retired["operation"]
    assert pending["previous_runtime"] == {
        "runtime_sha256": retired["runtime_sha256"],
        "launcher_sha256": retired["launcher_sha256"],
    }
    assert (
        json.loads((runtime.root / "retired" / f"{retired['operation']}.json").read_text())
        == retired
    )
    runtime_file = runtime.root / "runtime.py"
    recognized = runtime_file.read_bytes()
    runtime_file.write_bytes(b"unrecognized owner edit")
    failed = legacy.bootstrap("recover")
    assert failed.returncode == 1
    assert "refusing recovery overwrite" in failed.stderr
    assert runtime_file.read_bytes() == b"unrecognized owner edit"
    runtime_file.write_bytes(recognized)  # Explicit owner reconciliation restores known bytes.
    recovered = legacy.bootstrap("recover")
    assert recovered.returncode == 0, recovered.stderr
    runtime.verify_runtime(runtime.read())
    assert "previous_runtime" not in runtime.read()
    assert runtime.read()["aliases_installed"] is True
    for executable in (legacy.public, legacy.cached, legacy.actual):
        response = legacy.invoke(executable, "list")
        assert (response.returncode, response.stdout) == (75, APPROVED_MESSAGE)
    assert not legacy.mailbox.exists()


def test_concurrent_controller_is_rejected_without_changing_journal(legacy: Legacy) -> None:
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    before = runtime.journal.read_bytes()
    with runtime.controller():
        result = legacy.bootstrap("recover")
    assert result.returncode == 1
    assert "Another maintenance controller" in result.stderr
    assert runtime.journal.read_bytes() == before


def _active_fixture(candidate: Path) -> dict[str, object]:
    receipt = candidate.parent / "receipt.toml"
    receipt.write_text("[tool]\n")
    return {
        "executable": str(candidate),
        "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "root": str(candidate.parents[1]),
        "receipt": str(receipt),
        "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        "payloads": {str(candidate): hashlib.sha256(candidate.read_bytes()).hexdigest()},
    }


def test_hold_admission_and_lifetime_drain_are_separate(legacy: Legacy) -> None:
    # pylint: disable=consider-using-with,clean-code-commented-out-code
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    candidate = runtime.root / "generations/admission/bin/llm-bus"
    candidate.parent.mkdir(parents=True)
    candidate.write_text('#!/bin/sh\nprintf "ADMITTED\\n"\nread -r reply\n')
    candidate.chmod(0o700)
    state = runtime.read()
    state.update(
        {
            "phase": "active",
            "active": _active_fixture(candidate),
        }
    )
    runtime.write(state)
    process = subprocess.Popen(
        [str(legacy.public), "list"],
        env=legacy.environment,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == "ADMITTED\n"
        with runtime.controller():
            runtime.prepare(state)
            with pytest.raises(BlockingIOError), runtime.drained():
                pytest.fail("A live admitted command must prevent lifetime drain")
            response = legacy.invoke(legacy.public, "list")
            assert (response.returncode, response.stdout) == (75, APPROVED_MESSAGE)
        process.communicate("finish\n", timeout=10)
        assert process.returncode == 0
        with runtime.drained():
            assert runtime.read()["phase"] == "preparing"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_status_is_body_free_and_unknown_arguments_do_not_mutate(legacy: Legacy) -> None:
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    before = runtime.journal.read_bytes()
    result = legacy.invoke(legacy.cached, "maintenance", "status")
    assert result.returncode == 0
    assert set(cast("dict[str, object]", json.loads(result.stdout))) == {"operation", "phase"}
    invalid = legacy.invoke(legacy.cached, "maintenance", "resume", "--bypass")
    assert invalid.returncode == 2
    assert legacy.bootstrap("status", "--receipt", str(legacy.receipt)).returncode == 2
    assert runtime.journal.read_bytes() == before
    assert not legacy.mailbox.exists()


def test_missing_journal_cannot_be_replaced_with_fabricated_alias_originals(legacy: Legacy) -> None:
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    runtime.journal.unlink()
    result = legacy.begin()
    assert result.returncode == 1
    assert "no journal" in result.stderr
    assert not runtime.journal.exists()
    assert legacy.invoke(legacy.public, "list").stdout == APPROVED_MESSAGE


def test_changed_stable_launcher_is_never_published_during_retry(legacy: Legacy) -> None:
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    (runtime.root / "launcher").write_text("#!/bin/sh\nexit 42\n")
    before = legacy.cached.read_bytes()
    assert legacy.begin().returncode == 1
    assert legacy.bootstrap("recover").returncode == 1
    assert legacy.cached.read_bytes() == before
    assert legacy.invoke(legacy.cached, "list").stdout == APPROVED_MESSAGE


def test_existing_mailbox_and_companions_are_untouched(legacy: Legacy) -> None:
    files = [Path(str(legacy.mailbox) + suffix) for suffix in ("", "-wal", "-shm")]
    for path in files:
        path.write_bytes(b"retained mailbox bytes")
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
    assert legacy.begin().returncode == 0
    assert legacy.invoke(legacy.cached, "hook", "codex").stdout == APPROVED_MESSAGE
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in files] == before


def test_detached_inherited_child_keeps_admission_after_launcher_exit(legacy: Legacy) -> None:
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    candidate = runtime.root / "generations/detaching/bin/llm-bus"
    candidate.parent.mkdir(parents=True)
    program = (
        "import subprocess; "
        "child = subprocess.Popen(['/bin/sleep', '30'], close_fds=False, "
        "start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL); print(child.pid)"
    )
    candidate.write_text(
        "#!/bin/sh\nexec " + shlex.join([str(Path(sys.executable).resolve()), "-c", program]) + "\n"
    )
    candidate.chmod(0o700)
    state = runtime.read()
    state.update(
        {
            "phase": "active",
            "active": _active_fixture(candidate),
        }
    )
    runtime.write(state)
    result = legacy.invoke(legacy.public, "list")
    assert result.returncode == 0, result.stderr
    child_pid = int(result.stdout.strip())
    try:
        runtime.prepare(state)
        with pytest.raises(BlockingIOError), runtime.drained():
            pytest.fail("Detached inherited child must retain its admission lifetime")
    finally:
        os.kill(child_pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while True:
        try:
            with runtime.drained():
                break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                pytest.fail("Detached child did not release its admission after termination")
            time.sleep(0.01)


@pytest.fixture(autouse=True)
def isolated_process_inspection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never include another session's already-running bus in disposable hold tests."""
    native_inventory = maintenance.process_inventory
    existing = [item for item in native_inventory() if item["bus"] is True]

    def owned_inventory() -> list[dict[str, object]]:
        return [item for item in native_inventory() if item not in existing]

    monkeypatch.setattr(maintenance, "process_inventory", owned_inventory)


def access_inventory(legacy: Legacy, processes: list[subprocess.Popen[str]]) -> Path:
    """Owner-reviewed stopped-access input, independent of launcher gate assertions."""
    snapshot = {item["pid"]: item for item in maintenance.process_inventory()}
    records = [
        {key: snapshot[process.pid][key] for key in ("pid", "started", "command_sha256")}
        for process in processes
    ]
    path = legacy.home / "access-inventory.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "complete": True,
                "raw_clients_stopped": True,
                "hosts_gated": True,
                "mailboxes": [
                    str(legacy.home / ".local/share/llm-bus/bus.sqlite3"),
                    str(legacy.mailbox),
                ],
                "processes": records,
                "restart_sources": [
                    {
                        "kind": "assisted",
                        "identity": "stand-ins",
                        "stopped": True,
                        "restart_disabled": True,
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    return path


def writer(legacy: Legacy, role: str) -> subprocess.Popen[str]:
    # pylint: disable=consider-using-with,clean-code-commented-out-code
    # These are disposable Python SQLite accessors, never real bridge/host services.
    program = (
        "import sqlite3,sys,time; "
        "db=sqlite3.connect(sys.argv[1]); db.execute('PRAGMA journal_mode=WAL'); "
        "db.execute('CREATE TABLE IF NOT EXISTS untouched(value)'); db.commit(); "
        "print('ready',flush=True); time.sleep(60)"
    )
    process = subprocess.Popen(
        [role, "-c", program, str(legacy.mailbox)],
        executable=sys.executable,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=legacy.environment,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == "ready\n"
    return process


def stop_standins(processes: list[subprocess.Popen[str]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)


def test_hold_stops_legacy_accessors_but_keeps_hosts_open_and_detects_raw_writer(
    legacy: Legacy,
) -> None:
    # pylint: disable=consider-using-with,clean-code-commented-out-code
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    # Three independent legacy roles, including a detached timer, share a WAL mailbox.
    bus = [writer(legacy, "llm-bus") for _role in ("bridge", "timer", "collaboration")]
    raw = writer(legacy, "raw-python")
    hosts = [
        subprocess.Popen(
            [name, "60"],
            executable="/bin/sleep",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for name in ("codex", "claude")
    ]
    try:
        inventory = access_inventory(legacy, bus)
        with pytest.raises(maintenance.MaintenanceError, match="Unexplained mailbox accessor"):
            maintenance.hold_begin(runtime, inventory)
        assert runtime.read()["phase"] == "preparing"
        for process in bus:
            assert process.wait(timeout=5) == -signal.SIGTERM
        assert raw.poll() is None
        # Reconcile the omitted raw caller through the same operation, retaining old identities.
        data = cast("dict[str, object]", json.loads(inventory.read_text()))
        snapshot = next(item for item in maintenance.process_inventory() if item["pid"] == raw.pid)
        cast("list[dict[str, object]]", data["processes"]).append(
            {key: snapshot[key] for key in ("pid", "started", "command_sha256")}
        )
        inventory.write_text(json.dumps(data))
        maintenance.hold_begin(runtime, inventory)
        assert raw.wait(timeout=5) == -signal.SIGTERM
        held = runtime.read()
        assert held["phase"] == "held"
        verification = cast("dict[str, object]", held["verification"])
        assert verification["lifetimes_drained"] is True
        assert verification["processes_stopped"] is True
        evidence = cast("list[dict[str, object]]", verification["mailbox_handles"])
        assert {str(legacy.mailbox) + suffix for suffix in ("", "-wal", "-shm")} <= {
            item["path"] for item in evidence
        }
        assert all(process.poll() is None for process in hosts)
        assert legacy.invoke(legacy.public, "hook", "codex").stdout == APPROVED_MESSAGE
        assert legacy.invoke(legacy.cached, "maintenance", "resume").returncode == 1
        assert runtime.read()["phase"] == "held"
    finally:
        stop_standins([*bus, raw, *hosts])


def test_empty_handles_do_not_waive_dormant_raw_client_confirmation(legacy: Legacy) -> None:
    assert legacy.begin().returncode == 0
    inventory = access_inventory(legacy, [])
    data = cast("dict[str, object]", json.loads(inventory.read_text()))
    data["raw_clients_stopped"] = False
    inventory.write_text(json.dumps(data))
    response = legacy.invoke(legacy.public, "maintenance", "begin", "--inventory", str(inventory))
    assert response.returncode == 1
    assert "stopped raw clients" in response.stderr
    assert maintenance.Runtime(legacy.home).read()["phase"] == "preparing"
    assert not legacy.mailbox.exists()


def test_hold_refuses_alias_without_execute_permission_then_recovers(legacy: Legacy) -> None:
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    inventory = access_inventory(legacy, [])
    legacy.cached.chmod(0o600)
    with pytest.raises(maintenance.MaintenanceError, match="Public alias no longer routes"):
        maintenance.hold_begin(runtime, inventory)
    assert runtime.read()["phase"] == "preparing"
    legacy.cached.chmod(0o700)
    assert maintenance.operator_main(["recover"], runtime) == 0
    assert runtime.read()["phase"] == "held"
    assert legacy.invoke(legacy.cached, "hook", "codex").stdout == APPROVED_MESSAGE


def test_unknown_process_and_reused_identity_never_trigger_broad_kill(legacy: Legacy) -> None:
    assert legacy.begin().returncode == 0
    process = writer(legacy, "llm-bus")
    try:
        inventory = access_inventory(legacy, [])
        runtime = maintenance.Runtime(legacy.home)
        with pytest.raises(maintenance.MaintenanceError, match="Uninventoried bus process"):
            maintenance.hold_begin(runtime, inventory)
        assert process.poll() is None
        data = cast("dict[str, object]", json.loads(inventory.read_text()))
        data["processes"] = [
            {"pid": process.pid, "started": "different start", "command_sha256": "0" * 64}
        ]
        inventory.write_text(json.dumps(data))
        with pytest.raises(maintenance.MaintenanceError, match="Uninventoried bus process"):
            maintenance.hold_begin(runtime, inventory)
        assert process.poll() is None
    finally:
        stop_standins([process])


def test_recover_after_original_exit_ignores_reused_pid_and_retains_history(
    legacy: Legacy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    inventory = access_inventory(legacy, [])
    data = cast("dict[str, object]", json.loads(inventory.read_text()))
    original = {"pid": 987654, "started": "old start", "command_sha256": "0" * 64}
    data["processes"] = [original]
    inventory.write_text(json.dumps(data))
    current: list[dict[str, object]] = []
    monkeypatch.setattr(maintenance, "process_inventory", lambda: current)

    def no_signal(pid: int, _signal: int) -> None:
        pytest.fail(f"Must not signal exited/recycled PID {pid}")

    def interrupted_handles(_names: list[str]) -> list[dict[str, object]]:
        raise OSError("controller interrupted after process exit")

    monkeypatch.setattr(os, "kill", no_signal)
    with monkeypatch.context() as interrupted:
        interrupted.setattr(maintenance, "mailbox_evidence", interrupted_handles)
        with pytest.raises(OSError, match="after process exit"):
            maintenance.hold_begin(runtime, inventory)
    preparing = runtime.read()
    assert preparing["phase"] == "preparing"
    current.append({**original, "started": "new start", "parent": 1, "bus": False, "host": True})
    assert maintenance.operator_main(("recover",), runtime) == 0
    held = runtime.read()
    assert held["phase"] == "held"
    assert held["operation"] == preparing["operation"]
    assert held["access_inventory"] == data
    assert held["restart_sources"] == preparing["restart_sources"]


@pytest.mark.parametrize("transition", range(1, 6))
def test_interrupted_hold_recovers_without_releasing_gate(
    legacy: Legacy,
    monkeypatch: pytest.MonkeyPatch,
    transition: int,
) -> None:
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    inventory = access_inventory(legacy, [])
    actual_write = maintenance.durable_write
    count = 0

    def fail_after_write(path: Path, content: bytes, mode: int = 0o600) -> None:
        nonlocal count
        actual_write(path, content, mode)
        count += 1
        if count == transition:
            raise OSError("controller interrupted during hold")

    monkeypatch.setattr(maintenance, "durable_write", fail_after_write)
    with pytest.raises(OSError, match="controller interrupted"):
        maintenance.hold_begin(runtime, inventory)
    assert legacy.invoke(legacy.public, "list").stdout == APPROVED_MESSAGE
    # A crash before persisting inventory requires resupplying it; later recovery uses the journal.
    action = ("begin", "--inventory", str(inventory)) if transition == 1 else ("recover",)
    monkeypatch.setattr(maintenance, "durable_write", actual_write)
    assert maintenance.operator_main(action, runtime) == 0
    assert runtime.read()["phase"] == "held"
    assert legacy.invoke(legacy.public, "maintenance", "resume").returncode == 1


def test_missing_inspector_and_uncontrolled_restart_remain_preparing(
    legacy: Legacy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    inventory = access_inventory(legacy, [])
    data = cast("dict[str, object]", json.loads(inventory.read_text()))
    sources = cast("list[dict[str, object]]", data["restart_sources"])
    sources[0]["restart_disabled"] = False
    inventory.write_text(json.dumps(data))
    response = legacy.invoke(legacy.public, "maintenance", "begin", "--inventory", str(inventory))
    assert response.returncode == 1
    assert "Operator must stop caller" in response.stderr
    sources[0]["restart_disabled"] = True
    inventory.write_text(json.dumps(data))
    monkeypatch.setattr(sys, "platform", "unsupported")
    with pytest.raises(maintenance.MaintenanceError, match="handle inspection requires"):
        maintenance.hold_begin(runtime, inventory)
    assert runtime.read()["phase"] == "preparing"
    assert not legacy.mailbox.exists()


@pytest.mark.parametrize("mask_state", ["masked", "masked-runtime"])
def test_systemd_mask_intent_survives_failure_and_keeps_original_restart_state(
    legacy: Legacy,
    monkeypatch: pytest.MonkeyPatch,
    mask_state: str,
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    inventory = access_inventory(legacy, [])
    data = cast("dict[str, object]", json.loads(inventory.read_text()))
    data["restart_sources"] = [{"kind": "systemd-user", "identity": "bus-test.service"}]
    inventory.write_text(json.dumps(data))
    native_inspect = maintenance._inspect  # pyright: ignore[reportPrivateUsage]
    masked = False
    interrupted = False
    calls: list[list[str]] = []

    def inspect(arguments: list[str], *, missing_ok: bool = False) -> str:
        if arguments[0] == "systemctl":
            return (
                f"LoadState=masked\nActiveState=inactive\nUnitFileState={mask_state}\n"
                if masked
                else "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
            )
        return native_inspect(arguments, missing_ok=missing_ok)

    def control(arguments: list[str]) -> None:
        nonlocal masked, interrupted
        restart = cast("list[dict[str, object]]", runtime.read()["restart_sources"])
        assert restart[0]["prior"] == {
            "LoadState": "loaded",
            "ActiveState": "active",
            "UnitFileState": "enabled",
        }
        calls.append(arguments)
        masked = True
        if not interrupted:
            interrupted = True
            raise OSError("controller stopped after masking")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(maintenance, "_inspect", inspect)
    monkeypatch.setattr(maintenance, "_control", control)
    with pytest.raises(OSError, match="after masking"):
        maintenance.hold_begin(runtime, inventory)
    assert runtime.read()["phase"] == "preparing"
    if mask_state == "masked-runtime":
        with pytest.raises(maintenance.MaintenanceError, match="active or unmasked"):
            maintenance.hold_begin(runtime)
        assert runtime.read()["phase"] == "preparing"
    else:
        maintenance.hold_begin(runtime)
        assert runtime.read()["phase"] == "held"
    assert calls == [["systemctl", "--user", "mask", "--now", "bus-test.service"]] * 2


def test_launchd_records_loaded_plist_then_disables_and_boots_out_only_named_job(
    legacy: Legacy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    assert legacy.begin().returncode == 0
    runtime = maintenance.Runtime(legacy.home)
    inventory = access_inventory(legacy, [])
    data = cast("dict[str, object]", json.loads(inventory.read_text()))
    data["restart_sources"] = [{"kind": "launchd", "identity": "test.bus"}]
    inventory.write_text(json.dumps(data))
    native_inspect = maintenance._inspect  # pyright: ignore[reportPrivateUsage]
    disabled, loaded = False, True
    plist = legacy.home / "test.bus.plist"
    plist.write_bytes(
        b'<?xml version="1.0"?><plist version="1.0">'
        b"<dict><key>Label</key><string>test.bus</string></dict></plist>"
    )
    plist.chmod(0o600)
    calls: list[list[str]] = []

    def inspect(arguments: list[str], *, missing_ok: bool = False) -> str:
        if arguments[:2] == ["/bin/launchctl", "print-disabled"]:
            return '"test.bus" => ' + str(disabled).lower()
        return native_inspect(arguments, missing_ok=missing_ok)

    def job(identity: str) -> dict[str, object]:
        assert identity == "test.bus"
        return {"loaded": loaded, "plist": str(plist) if loaded else None, "running": loaded}

    def control(arguments: list[str]) -> None:
        nonlocal disabled, loaded
        restart = cast("list[dict[str, object]]", runtime.read()["restart_sources"])
        assert restart[0]["prior"] == {
            "disabled": False,
            "loaded": True,
            "plist": str(plist),
            "running": True,
            "starts_on_load": False,
            "plist_sha256": hashlib.sha256(plist.read_bytes()).hexdigest(),
        }
        calls.append(arguments)
        if arguments[1] == "disable":
            disabled = True
        elif arguments[1] == "bootout":
            loaded = False

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(maintenance, "_inspect", inspect)
    monkeypatch.setattr(maintenance, "_control", control)
    monkeypatch.setattr(maintenance, "_launchd_job", job)
    maintenance.hold_begin(runtime, inventory)
    assert runtime.read()["phase"] == "held"
    target = f"gui/{os.getuid()}/test.bus"
    assert calls == [["/bin/launchctl", "disable", target], ["/bin/launchctl", "bootout", target]]

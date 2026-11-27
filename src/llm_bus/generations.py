"""Owner-invoked installation and release of final-path uv generations.

This controller never runs an older client to discover mailbox compatibility.
All candidate probes use a disposable home and mailbox. Public dispatch stays
closed until the stopped-access and integration barrier has been rechecked.
"""

from __future__ import annotations

import ast
import base64
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from typing import cast

from llm_bus import maintenance as gate
from llm_bus.cli import upgrade as build
from llm_bus.errors import BusError
from llm_bus.hosts import files
from llm_bus.hosts.hooks import (
    AGY_ALLOW_RULE,
    AGY_EVENTS,
    AGY_HOOK_NAME,
    AGY_SETTINGS,
    apply_bus_settings,
    host_events,
    is_bus_handler,
    settings_kind,
)
from llm_bus.hosts.settings import CODEX_HOME_ENV, remember_codex_home

# Journal fields and boundary diagnostics are explicit protocol vocabulary.
# pylint: disable=clean-code-business-policy-literal
# Uninstall completion and recovery share this controller's ownership boundary.
# pylint: disable=too-many-lines


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generation_environment(record: dict[str, object]) -> dict[str, str]:
    return {
        **os.environ,
        "UV_TOOL_DIR": str(record["tools"]),
        "UV_TOOL_BIN_DIR": str(record["bin"]),
    }


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BusError(f"Invalid {label}; hold retained")
    return cast("dict[str, object]", value)


def _schema(wheel: Path) -> int:
    with zipfile.ZipFile(wheel) as archive:
        tree = ast.parse(archive.read("llm_bus/mailbox/schema.py"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
            and not isinstance(node.value.value, bool)
        ):
            return node.value.value
    raise BusError("Accepted wheel has no static schema identity")


def _verify_distribution(record: dict[str, object], environment: dict[str, str]) -> dict[str, str]:
    """Check PEP 610 source and every wheel payload against the accepted bytes."""
    python = Path(str(record["tools"])) / "llm-bus/bin/python"
    code = (
        "import importlib.metadata as m,json,sys; from pathlib import Path; "
        "d=m.distribution('llm-bus'); "
        "print(json.dumps({'root':str(d.locate_file('')), 'version':d.version, "
        "'python':list(sys.version_info[:2]), 'interpreter':str(Path(sys.executable).resolve()), "
        "'source':json.loads(d.read_text('direct_url.json'))}))"
    )
    found = _object(
        cast("object", json.loads(build.run([str(python), "-I", "-c", code], env=environment))),
        "distribution",
    )
    wheel = Path(str(record["wheel"]))
    if _object(found["source"], "PEP 610 source").get("url") != wheel.as_uri():
        raise BusError("Installed distribution source differs from accepted wheel")
    if found["python"] != [3, 12] or found["interpreter"] != str(
        Path(str(record["python"])).resolve()
    ):
        raise BusError("Installed interpreter differs from the selected Python 3.12")
    root = Path(str(found["root"]))
    if not root.resolve().is_relative_to(Path(str(record["tools"])) / "llm-bus"):
        raise BusError("Installed distribution escapes its generation")
    payloads: dict[str, str] = {}
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith(("/", ".dist-info/RECORD")):
                continue
            target = root / name
            if not target.resolve().is_relative_to(root.resolve()):
                raise BusError("Unsafe wheel payload path")
            if (
                target.is_symlink()
                or not target.is_file()
                or target.read_bytes() != archive.read(name)
            ):
                raise BusError(f"Installed wheel payload differs: {name}")
            payloads[str(target)] = digest(target)
    return payloads


def _verify_layout(record: dict[str, object]) -> None:
    root = Path(str(record["root"]))
    if not root.is_absolute() or root.is_symlink():
        raise BusError("Invalid final generation path")
    if (
        Path(str(record["tools"])) != root / "tools"
        or Path(str(record["bin"])) != root / "bin"
        or Path(str(record["receipt"])) != build.receipt_path(root / "tools")
    ):
        raise BusError("Generation receipt/directories do not share the recorded root")


def verify_generation(record: dict[str, object]) -> dict[str, object]:
    _verify_layout(record)
    wheel = Path(str(record["wheel"]))
    if wheel.is_symlink() or digest(wheel) != record["wheel_sha256"]:
        raise BusError("Accepted wheel missing or changed; hold retained")
    receipt = Path(str(record["receipt"]))
    tool, requirement = build.receipt_requirement(receipt)
    extras = tuple(cast("list[str]", record["extras"]))
    if (
        requirement.get("path") != str(wheel)
        or build.requirement_extras(requirement) != extras
        or tool.get("python") != record["python"]
    ):
        raise BusError("uv receipt wheel/extras/interpreter differs from accepted generation")
    binary = build.receipt_entrypoint(tool)
    expected = Path(str(record["bin"])) / "llm-bus"
    if binary != expected or not binary.resolve().is_relative_to(Path(str(record["root"]))):
        raise BusError("uv receipt entrypoint escapes its generation")
    if record.get("verified") is True and (
        digest(receipt) != record.get("receipt_sha256") or digest(binary) != record.get("sha256")
    ):
        raise BusError("Verified generation receipt or executable changed")
    with tempfile.TemporaryDirectory(prefix="llm-bus-candidate-") as scratch:
        home = Path(scratch)
        environment = {
            **generation_environment(record),
            "HOME": str(home),
            "PATH": build.VERIFY_PATH,
            "LLM_BUS_DB": str(home / "probe.sqlite3"),
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        }
        payloads = _verify_distribution(record, environment)
        python = Path(str(record["tools"])) / "llm-bus/bin/python"
        for argv in ([str(binary), "--help"], [str(python), "-I", "-m", "llm_bus", "--help"]):
            if "usage:" not in build.run(argv, env=environment):
                raise BusError("Candidate CLI/module verification produced no usage")
        if "collab" in extras:
            build.run([str(python), "-I", "-c", "import llm_bus.collab.transport"], env=environment)
        if (home / "probe.sqlite3").exists():
            raise BusError("Candidate help unexpectedly opened a mailbox")
    if _schema(wheel) != record["schema"]:
        raise BusError("Accepted schema identity changed")
    return {
        **record,
        "executable": str(binary),
        "sha256": digest(binary),
        "receipt_sha256": digest(receipt),
        "payloads": payloads,
    }


def _skill_parent(home: Path, requested: Path | None, state: dict[str, object] | None) -> Path:
    if requested is not None:
        parent = requested.expanduser().absolute()
    elif state is not None:
        parent = Path(str(state["skills_dir"]))
    else:
        choices = [
            parent
            for parent in (home / ".agents/skills", home / ".claude/skills")
            if (parent / "llm-bus/SKILL.md").is_file()
            and not parent.is_symlink()
            and not (parent / "llm-bus").is_symlink()
        ]
        if len(choices) != 1:
            raise BusError("Select the real skill parent with --skills-dir DIR")
        parent = choices[0]
    if parent.is_symlink():
        raise BusError("Skill parent must not be a symlink")
    build.skill_destination(parent)
    return parent


def _settings_paths(runtime: gate.Runtime, state: dict[str, object]) -> list[Path]:
    # The bootstrap inventories effective, remembered and explicit host homes.
    # Keep that exact set; do not infer that a running host reloads new settings.
    paths = state.get("settings_paths")
    if paths is None:
        return sorted(gate.host_settings(runtime.home, ()))
    if not isinstance(paths, list) or any(
        not isinstance(p, str) for p in cast("list[object]", paths)
    ):
        raise BusError("Invalid recorded host settings paths")
    return sorted(
        {Path(path) for path in cast("list[str]", paths)} | gate.host_settings(runtime.home, ())
    )


def _write_hooks(paths: list[Path], snapshots: list[dict[str, object]]) -> None:
    for path, snapshot in zip(paths, snapshots, strict=True):
        before = None if snapshot["bytes"] is None else base64.b64decode(str(snapshot["bytes"]))
        settings = files.parse_settings(path, before)
        updated = apply_bus_settings(settings, path, settings_kind(path), install=True)
        if updated != settings:
            files.write_settings(path, updated, before, int(str(snapshot["mode"])))


def _remember_homes(home: Path, paths: list[Path]) -> None:
    previous = os.environ.get(CODEX_HOME_ENV)
    try:
        for path in paths:
            if settings_kind(path) == "codex":
                os.environ[CODEX_HOME_ENV] = str(path.parent)
                remember_codex_home(home)
    finally:
        if previous is None:
            os.environ.pop(CODEX_HOME_ENV, None)
        else:
            os.environ[CODEX_HOME_ENV] = previous


def _original_snapshots(
    state: dict[str, object], snapshots: list[dict[str, object]]
) -> list[dict[str, object]]:
    prior = cast("list[dict[str, object]]", state.get("integration_snapshots", []))
    known = {str(item["path"]) for item in prior}
    return [*prior, *(item for item in snapshots if str(item["path"]) not in known)]


def _copy_skill(source: Path, destination: Path) -> None:
    """Durably retain accepted skill bytes before replacing the real copy."""
    expected = build.skill_identity(source)
    gate.private_directory(destination)
    for name, expected_digest in expected.items():
        target = destination / name
        gate.private_directory(target.parent)
        if target.exists():
            if target.is_symlink() or digest(target) != expected_digest:
                raise BusError("Recorded skill snapshot differs; hold retained")
        else:
            gate.durable_write(
                target, (source / name).read_bytes(), (source / name).stat().st_mode & 0o777
            )
    if build.skill_identity(destination) != expected:
        raise BusError("Recorded skill snapshot has unexpected files")


def _integrations(
    runtime: gate.Runtime, state: dict[str, object], checkout: Path
) -> dict[str, object]:
    state = dict(state)
    paths = _settings_paths(runtime, state)
    snapshots: list[dict[str, object]] = []
    for path in paths:
        content = files.current_bytes(path)
        files.parse_settings(path, content)
        snapshots.append(
            {
                "path": str(path),
                "bytes": None if content is None else base64.b64encode(content).decode(),
                "mode": path.stat().st_mode & 0o777 if content is not None else 0o600,
            }
        )
    state["integration_snapshots"] = _original_snapshots(state, snapshots)
    state["settings_paths"] = [str(path) for path in paths]
    hook_alias = runtime.root / "llm-bus"
    aliases = cast("list[str]", state["aliases"])
    if str(hook_alias) not in aliases:
        aliases.append(str(hook_alias))
    state["created_aliases"] = [str(hook_alias)]
    state["step"] = "integrations-intent"
    runtime.write(state)
    gate.install_aliases(runtime, state)
    skill = Path(str(state["skills_dir"])) / "llm-bus"
    backup = runtime.root / "skill-backups" / str(state["operation"])
    if skill.exists() and not backup.exists():
        build.skill_identity(skill)
        gate.private_directory(backup.parent)
        _copy_skill(skill, backup)
        state["previous_skill"] = str(backup)
        runtime.write(state)
    before_path = os.environ.get("PATH")
    os.environ["PATH"] = str(runtime.root) + os.pathsep + (before_path or "")
    if hook_alias.exists() and hook_alias.read_bytes() != (runtime.root / "launcher").read_bytes():
        raise BusError("Stable hook alias changed")
    gate.durable_write(hook_alias, (runtime.root / "launcher").read_bytes(), 0o700)
    try:
        _write_hooks(paths, snapshots)
        _remember_homes(runtime.home, paths)
        installed = build.sync_skill(
            checkout, Path(str(state["skills_dir"])), revision=str(state["accepted_revision"])
        )
    finally:
        if before_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = before_path
    state["skill_sha256"] = installed["sha256"]
    if build.skill_identity(skill) != state["skill_sha256"]:
        raise BusError("Installed skill differs from accepted source")
    candidate = _object(state["candidate"], "candidate")
    accepted_skill = Path(str(candidate["root"])) / "skill"
    if not accepted_skill.exists():
        _copy_skill(skill, accepted_skill)
    candidate["skill_sha256"] = state["skill_sha256"]
    state["step"] = "integrations-complete"
    runtime.write(state)
    return state


def _verify_agy_settings(path: Path) -> None:
    """Antigravity's settings carry only the allow rule that runs bus commands unsandboxed."""
    settings = files.parse_settings(path, files.current_bytes(path))
    allowed = _object(settings.get("permissions"), "Antigravity permissions").get("allow")
    if not isinstance(allowed, list) or AGY_ALLOW_RULE not in cast("list[object]", allowed):
        raise BusError(f"Missing llm-bus allow rule: {path}")


def _verify_agy_hook_events(path: Path) -> None:
    """Antigravity hooks.json names hooks; the bus-owned one lists handlers per event."""
    settings = files.parse_settings(path, files.current_bytes(path))
    hook = _object(settings.get(AGY_HOOK_NAME), "Antigravity bus hook")
    for event in AGY_EVENTS:
        handlers = hook.get(event)
        if not isinstance(handlers, list) or not any(
            is_bus_handler(handler, "agy") for handler in cast("list[object]", handlers)
        ):
            raise BusError(f"Missing {event} bus handler: {path}")


def _verify_hook_events(path: Path) -> None:
    kind = settings_kind(path)
    if kind == "agy":
        _verify_agy_hook_events(path)
        return
    hooks = _object(
        files.parse_settings(path, files.current_bytes(path)).get("hooks"), "host hooks"
    )
    for event in host_events(kind):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            raise BusError(f"Missing {event} hooks: {path}")
        handlers: list[object] = []
        for group in cast("list[object]", groups):
            configured = _object(group, "hook group")
            values = configured.get("hooks")
            if isinstance(values, list):
                handlers.extend(cast("list[object]", values))
        if not any(is_bus_handler(handler, kind) for handler in handlers):
            raise BusError(f"Missing {event} bus handler: {path}")


def _verify_integrations(runtime: gate.Runtime, state: dict[str, object]) -> None:
    gate.verify_aliases(runtime, state)
    for path in _settings_paths(runtime, state):
        if settings_kind(path) == AGY_SETTINGS:
            _verify_agy_settings(path)
        else:
            aliases = gate.hook_aliases(path)
            if not aliases or aliases != {runtime.root / "llm-bus"}:
                raise BusError(f"Host hooks are not bound to the stable launcher: {path}")
            _verify_hook_events(path)
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
        files.sync_parent_directories(path)
    alias = runtime.root / "llm-bus"
    if alias.is_symlink() or alias.read_bytes() != (runtime.root / "launcher").read_bytes():
        raise BusError("Stable hook launcher changed")
    if build.sync_skill_tree(Path(str(state["skills_dir"])) / "llm-bus") != state.get(
        "skill_sha256"
    ):
        raise BusError("Installed skill differs from accepted source")


def mailbox_schemas(state: dict[str, object]) -> dict[str, int]:
    names = state.get("mailboxes")
    if not isinstance(names, list) or not names:
        raise BusError("Recorded mailbox set required")
    result: dict[str, int] = {}
    for name in cast("list[str]", names):
        path = Path(name)
        if not path.exists() and not path.is_symlink():
            result[name] = 0
            continue
        before = gate.owned(path)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
            if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BusError(f"Mailbox integrity check failed: {path}")
            if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BusError(f"Mailbox foreign-key check failed: {path}")
            result[name] = cast("tuple[int]", database.execute("PRAGMA user_version").fetchone())[0]
        after = gate.owned(path)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise BusError("Mailbox changed during read-only verification; hold retained")
    return result


def _compatible(
    state: dict[str, object], candidate: dict[str, object], evidence_path: Path | None
) -> dict[str, object]:
    state = dict(state)
    schemas = mailbox_schemas(state)
    maximum = int(str(candidate["schema"]))
    if any(schema != maximum for schema in schemas.values()):
        raise BusError(
            "Mailbox schema differs from candidate; owner offline migration/verification required"
        )
    active = state.get("active")
    baseline = state.get("baseline_schemas", schemas if isinstance(active, dict) else None)
    initial_schema = state.get("initial_client_schema")
    if initial_schema is None and isinstance(active, dict):
        initial_schema = cast("dict[str, object]", active).get("schema")
    floor = state.get("compatibility_floor")
    if floor is not None and floor != candidate["revision"]:
        # Unknown/shallow ancestry cannot establish a compatible floor.
        build.git(
            Path(str(state["controller_checkout"])),
            "merge-base",
            "--is-ancestor",
            str(floor),
            str(candidate["revision"]),
        )
    initialized = _fresh_mailboxes(state, candidate, schemas)
    changed = {
        name
        for name, schema in schemas.items()
        if not isinstance(baseline, dict) or cast("dict[str, object]", baseline).get(name) != schema
    }
    fresh_only = (
        isinstance(baseline, dict)
        and set(cast("dict[str, object]", baseline)) == set(schemas)
        and bool(changed)
        and changed.issubset(initialized)
        and (maximum == initial_schema or initialized == set(schemas))
    )
    if (baseline != schemas or maximum != initial_schema) and not fresh_only:
        if evidence_path is None:
            raise BusError("Schema activation requires --evidence FILE; hold retained")
        gate.owned(evidence_path)
        evidence = gate.json_object(evidence_path)
        expected = {
            "operation": state["operation"],
            "wheel_sha256": candidate["wheel_sha256"],
            "mailboxes": schemas,
            "compatible_floor": floor or candidate["revision"],
            "local_schema_verified": True,
            "relay_drained": True,
            "first_deletion_approved": True,
            "retention_preview_verified": True,
            "replay_high_water_verified": True,
        }
        approvals = (
            "local_schema_verified",
            "relay_drained",
            "first_deletion_approved",
            "retention_preview_verified",
            "replay_high_water_verified",
        )
        if evidence != expected or any(evidence.get(key) is not True for key in approvals):
            raise BusError(
                "Cutover evidence does not bind the exact candidate, floor and mailbox set"
            )
        state["compatibility_floor"] = floor or candidate["revision"]
        state["cutover_evidence"] = evidence
    return state


def _mailbox_absent(mailbox: str) -> bool:
    return not any(
        Path(mailbox + suffix).exists() or Path(mailbox + suffix).is_symlink()
        for suffix in ("", "-wal", "-shm", "-journal")
    )


def _fresh_identity(mailbox: str) -> dict[str, object]:
    """Bind fresh output to its owned file, allowing SQLite's empty read-only WAL."""
    path = Path(mailbox)
    info = gate.owned(path)
    for suffix in ("-wal", "-shm", "-journal"):
        companion = Path(mailbox + suffix)
        if companion.exists() or companion.is_symlink():
            companion_info = gate.owned(companion)
            if suffix == "-journal" or (suffix == "-wal" and companion_info.st_size != 0):
                raise BusError("Fresh mailbox has unexplained companions; hold retained")
    result: dict[str, object] = {
        "identity": [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns],
        "sha256": digest(path),
    }
    after = gate.owned(path)
    if result["identity"] != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns]:
        raise BusError("Fresh mailbox changed during inspection; hold retained")
    return result


def _fresh_mailboxes(
    state: dict[str, object], candidate: dict[str, object], schemas: dict[str, int]
) -> set[str]:
    receipts = state.get("fresh_initializations", {})
    if not isinstance(receipts, dict):
        raise BusError("Invalid fresh initialization evidence; hold retained")
    absent = cast("list[str]", state.get("baseline_absent", []))
    baseline = _object(state.get("baseline_schemas", {}), "baseline schemas")
    initialized: set[str] = set()
    for name in schemas:
        receipt = cast("dict[str, object]", receipts).get(name)
        if receipt is None:
            continue
        expected = {
            "operation": state["operation"],
            "wheel_sha256": candidate["wheel_sha256"],
            "source": "absent",
            "schema_range": [candidate["schema"], candidate["schema"]],
            "status": "complete",
            **_fresh_identity(name),
        }
        if (
            name not in absent
            or baseline.get(name) != 0
            or receipt != expected
            or mailbox_schemas({"mailboxes": [name]}) != {name: candidate["schema"]}
            or _fresh_identity(name) != {key: expected[key] for key in ("identity", "sha256")}
        ):
            raise BusError("Fresh initialization identity/evidence changed; hold retained")
        initialized.add(name)
    return initialized


def _fresh_initialization_intent(
    state: dict[str, object], candidate: dict[str, object], mailbox: str
) -> dict[str, object] | None:
    if mailbox not in cast("list[str]", state.get("baseline_absent", [])) or Path(mailbox).exists():
        return None
    if not _mailbox_absent(mailbox):
        raise BusError("Fresh mailbox absence changed; hold retained")
    receipts = _object(state.get("fresh_initializations", {}), "initializations")
    if mailbox in receipts:
        raise BusError("Unfinished fresh initialization; hold retained")
    return {
        "operation": state["operation"],
        "wheel_sha256": candidate["wheel_sha256"],
        "source": "absent",
        "schema_range": [candidate["schema"], candidate["schema"]],
        "status": "intent",
    }


def _complete_fresh_initialization(
    state: dict[str, object], candidate: dict[str, object], mailbox: str, output: str
) -> None:
    identity = _fresh_identity(mailbox)
    if (
        json.loads(output) != cast("list[int]", identity["identity"])[:2]
        or mailbox_schemas({"mailboxes": [mailbox]}) != {mailbox: candidate["schema"]}
        or _fresh_identity(mailbox) != identity
    ):
        raise BusError("Fresh initializer output differs from candidate; hold retained")
    with Path(mailbox).open("rb") as stream:
        os.fsync(stream.fileno())
    gate.sync_directory(Path(mailbox).parent)
    receipt = _object(
        _object(state["fresh_initializations"], "initializations")[mailbox], "initialization"
    )
    receipt.update({"status": "complete", **identity})


def _release(
    runtime: gate.Runtime, state: dict[str, object], evidence_path: Path | None
) -> dict[str, object]:
    if state.get("uninstalling") is True:
        raise BusError("Finish recorded uninstall before starting another operation")
    candidate = verify_generation(_object(state.get("candidate"), "candidate"))
    state = _compatible(state, candidate, evidence_path)
    _verify_integrations(runtime, state)
    state["candidate"] = candidate
    state["phase"] = "verified"
    state["step"] = "publish-intent"
    runtime.write(state)
    with gate.local_lock(runtime.root / "admission.lock", fcntl.LOCK_EX), runtime.drained():
        gate.mailbox_evidence(cast("list[str]", state["mailboxes"]))
        state["active"] = candidate
        state["phase"] = "active"
        state["step"] = "restart-restore-intent"
        runtime.write(state)
    return gate.restore_sources(runtime, state)


def _new_record(
    runtime: gate.Runtime, revision: str, previous: build.InstalledRequirement
) -> dict[str, object]:
    identifier = revision[:8] + "-" + uuid.uuid4().hex[:12]
    root = runtime.root / "generations" / identifier
    return {
        "id": identifier,
        "root": str(root),
        "tools": str(root / "tools"),
        "bin": str(root / "bin"),
        "receipt": str(build.receipt_path(root / "tools")),
        "revision": revision,
        "extras": list(previous.extras),
        "python": str(runtime.read()["python"]),
        "verified": False,
    }


def _install_record(
    uv: str, runtime: gate.Runtime, state: dict[str, object], record: dict[str, object]
) -> dict[str, object]:
    state = dict(state)
    state["step"] = "install-intent"
    runtime.write(state)
    gate.private_directory(Path(str(record["root"])))
    build.run(
        [
            uv,
            "tool",
            "install",
            "--force",
            "--python",
            str(record["python"]),
            build.requirement_arg(
                Path(str(record["wheel"])), tuple(cast("list[str]", record["extras"]))
            ),
        ],
        env=generation_environment(record),
    )
    verified = verify_generation(record)
    verified["verified"] = True
    return verified


def _candidate(
    uv: str,
    runtime: gate.Runtime,
    state: dict[str, object],
    checkout: Path,
    previous: build.InstalledRequirement,
) -> dict[str, object]:
    state = dict(state)
    revision = str(state["accepted_revision"])
    record = _new_record(runtime, revision, previous)
    records = cast("list[dict[str, object]]", state["generations"])
    records.append(record)
    state["candidate"] = record
    state["step"] = "build-intent"
    runtime.write(state)
    root = Path(str(record["root"]))
    gate.private_directory(root)
    wheel = build.build_wheel(uv, checkout, root, root / "artifact", revision=revision)
    record.update({"wheel": str(wheel), "wheel_sha256": digest(wheel), "schema": _schema(wheel)})
    verified = _install_record(uv, runtime, state, record)
    records[-1] = verified
    return verified


def _retain_previous(
    uv: str, runtime: gate.Runtime, state: dict[str, object], previous: build.InstalledRequirement
) -> dict[str, object]:
    state = dict(state)
    records = cast("list[dict[str, object]]", state["generations"])
    active = state.get("active")
    if isinstance(active, dict):
        state["previous_generation"] = cast("dict[str, object]", active)["id"]
        return state
    if records:
        record = next(
            (item for item in records if item.get("id") == state.get("previous_generation")), None
        )
        if record is None:
            raise BusError("Recorded previous generation is missing")
        if record.get("verified") is True:
            verify_generation(record)
            return state
    else:
        record = _new_record(runtime, "previous-" + digest(previous.path), previous)
        record.update(
            {
                "wheel": str(previous.path),
                "wheel_sha256": digest(previous.path),
                "schema": _schema(previous.path),
            }
        )
        records.append(record)
        state["previous_generation"] = record["id"]
    state["step"] = "retain-previous-intent"
    runtime.write(state)
    verified = _install_record(uv, runtime, state, record)
    skill = Path(str(state["skills_dir"])) / "llm-bus"
    verified["skill_sha256"] = None
    if skill.exists():
        verified["skill_sha256"] = build.skill_identity(skill)
        _copy_skill(skill, Path(str(record["root"])) / "skill")
    records[records.index(record)] = verified
    runtime.write(state)
    return state


def _previous_requirement(state: dict[str, object]) -> build.InstalledRequirement:
    active = state.get("active")
    if active is None:
        return build.read_previous(Path(str(state["receipt"])))
    record = _object(active, "active")
    wheel = Path(str(record["wheel"]))
    if wheel.is_symlink() or digest(wheel) != record["wheel_sha256"]:
        raise BusError("Recorded active artifact missing or changed; hold retained")
    extras = record.get("extras")
    if not isinstance(extras, list) or any(
        not isinstance(item, str) for item in cast("list[object]", extras)
    ):
        raise BusError("Recorded active extras are malformed")
    return build.InstalledRequirement(
        wheel, tuple(cast("list[str]", extras)), str(record["python"])
    )


def install_generation(
    start: Path, home: Path, *, skills_dir: Path | None = None, repair: bool = False
) -> dict[str, object]:
    uv = shutil.which("uv")
    if uv is None:
        raise BusError("uv is required to install llm-bus")
    checkout = build.checkout_root(start)
    build.require_clean(checkout)
    runtime = gate.Runtime(home)
    build.require_safe_placement(checkout, (home / build.PACKAGES_DIR, runtime.root))
    initial = runtime.read(verify_active=False) if runtime.exists() else None
    if (
        initial is not None
        and initial.get("uninstalling") is True
        and initial.get("step") != "uninstalled"
    ):
        raise BusError("Finish recorded uninstall before installing another generation")
    skills = _skill_parent(home, skills_dir, initial)
    if initial is None or initial.get("uninstalling") is True:
        receipt = build.receipt_path(Path(build.run([uv, "tool", "dir"])))
        gate.bootstrap_begin(runtime, receipt, skills)
        raise BusError(
            "Stable launcher installed; complete maintenance begin --inventory FILE, then retry"
        )
    with runtime.controller():
        initial = runtime.read(verify_active=False)
        skills = _skill_parent(home, skills_dir, initial)
        gate.hold_begin(runtime, acquire_controller=False)
        state = runtime.read()
        if state["phase"] != "held":
            runtime.prepare(state)
            raise BusError("Concurrent controller changed hold; retry from the retained hold")
        state["skills_dir"] = str(skills)
        state["controller_checkout"] = str(checkout)
        if "baseline_schemas" not in state:
            state["baseline_schemas"] = mailbox_schemas(state)
            state["baseline_absent"] = [
                name for name in cast("list[str]", state["mailboxes"]) if _mailbox_absent(name)
            ]
            state.pop("fresh_initializations", None)
        state["accepted_revision"] = build.git(checkout, "rev-parse", build.HEAD_REF)
        previous = _previous_requirement(state)
        state.setdefault("initial_client_schema", _schema(previous.path))
        state = _retain_previous(uv, runtime, state, previous)
        reusable = next(
            (
                item
                for item in reversed(cast("list[dict[str, object]]", state["generations"]))
                if item.get("revision") == state["accepted_revision"]
                and item.get("verified") is True
            ),
            None,
        )
        if reusable is not None and not repair:
            candidate = verify_generation(reusable)
        else:
            candidate = _candidate(uv, runtime, state, checkout, previous)
        state["candidate"] = candidate
        state["step"] = "candidate-verified"
        runtime.write(state)
        state = _integrations(runtime, state, checkout)
        _verify_integrations(runtime, state)
        if initial["phase"] == "active":
            try:
                state = _release(runtime, state, None)
            except BusError as error:
                state["release_blocker"] = str(error)
                runtime.write(state)
        return {
            "package_id": str(state["accepted_revision"])[:8],
            "reinstalled": repair or reusable is None,
            "wheel": candidate["wheel"],
            "binary": candidate["executable"],
            "phase": state["phase"],
            "uv_tool_dir": candidate["tools"],
            "uv_tool_bin_dir": candidate["bin"],
            "next_step": "Use maintenance resume after compatibility and cutover verification; "
            "hold retained"
            if state["phase"] != "active"
            else (
                "Dispatch resumed; owner restart actions required: "
                + "; ".join(cast("list[str]", state["restart_restore_pending"]))
                if state.get("restart_restore_pending")
                else "Service resumed through stable launchers"
            ),
        }


def resume(home: Path, evidence: Path | None = None) -> dict[str, object]:
    runtime = gate.Runtime(home)
    with runtime.controller():
        state = runtime.read(verify_active=False)
        if state["phase"] == "active" and state.get("step") in {
            "restart-restore-intent",
            "restart-restore-pending",
        }:
            runtime.read()  # Verify the published runtime before retrying restart intents.
            return gate.restore_sources(runtime, state, evidence)
        if state["phase"] == "active" and evidence is not None:
            raise BusError("Restart acknowledgement requires pending restoration")
        gate.hold_begin(runtime, acquire_controller=False)
        return _release(runtime, runtime.read(), evidence)


def candidate_operation(home: Path, action: str, mailbox: str) -> dict[str, object]:
    """Execute only the three fixed offline checks on a recorded mailbox."""
    runtime = gate.Runtime(home)
    with runtime.controller():
        gate.hold_begin(runtime, acquire_controller=False)
        state = runtime.read()
        if mailbox not in cast("list[str]", state["mailboxes"]):
            raise BusError("Candidate operations require an exact recorded mailbox path")
        candidate = verify_generation(_object(state.get("candidate"), "candidate"))
        schemas = mailbox_schemas(state)
        if schemas[mailbox] > int(str(candidate["schema"])):
            raise BusError("Candidate cannot inspect or migrate an activated newer schema")
        initialization = (
            _fresh_initialization_intent(state, candidate, mailbox) if action == "migrate" else None
        )
        if initialization is not None:
            state["step"] = "migrate-parent-intent"
            runtime.write(state)
            Path(mailbox).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Visible directories can survive failed syncs; repeat every ancestor barrier.
            # Failure before intent remains retryable without accepting unknown initializer output.
            for parent in reversed(Path(mailbox).parents):
                gate.sync_directory(parent)
            state["fresh_initializations"] = {
                **_object(state.get("fresh_initializations", {}), "initializations"),
                mailbox: initialization,
            }
        state["step"] = action + "-intent"
        runtime.write(state)
        python = Path(str(candidate["tools"])) / "llm-bus/bin/python"
        if action == "migrate":
            code = (
                "from pathlib import Path; import sys; "
                "from llm_bus.mailbox.store import Store; Store(Path(sys.argv[1]))"
            )
            if initialization is not None:
                code = (
                    "from pathlib import Path; import sys,os,json; p=Path(sys.argv[1]); "
                    "fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); "
                    "s=os.fstat(fd); os.close(fd); "
                    "from llm_bus.mailbox.store import Store; Store(p); "
                    "print(json.dumps([s.st_dev,s.st_ino]))"
                )
        elif action == "preview":
            code = (
                "from pathlib import Path; import sys,json; "
                "from llm_bus.mailbox.preview import preview; "
                "print(json.dumps(preview(Path(sys.argv[1]))))"
            )
        elif action == "verify":
            state["step"] = "verify-complete"
            state["candidate_check"] = {
                "action": action,
                "mailbox": mailbox,
                "wheel_sha256": candidate["wheel_sha256"],
                "schemas": schemas,
            }
            runtime.write(state)
            return {"mailboxes": schemas, "wheel_sha256": candidate["wheel_sha256"]}
        else:
            raise BusError("Unsupported candidate check")
        with tempfile.TemporaryDirectory(prefix="llm-bus-offline-check-") as scratch:
            environment = {
                **generation_environment(candidate),
                "HOME": scratch,
                "PATH": build.VERIFY_PATH,
                "LLM_BUS_DB": mailbox,
                "CODEX_HOME": str(Path(scratch) / ".codex"),
                "CLAUDE_CONFIG_DIR": str(Path(scratch) / ".claude"),
            }
            output = build.run([str(python), "-I", "-c", code, mailbox], env=environment)
        checked_schemas = mailbox_schemas(state)
        if initialization is not None:
            _complete_fresh_initialization(state, candidate, mailbox, output)
        state["step"] = action + "-complete"
        state["candidate_check"] = {
            "action": action,
            "mailbox": mailbox,
            "wheel_sha256": candidate["wheel_sha256"],
            "schemas": checked_schemas,
        }
        runtime.write(state)
        return {"phase": state["phase"], "check": action, "output": output}


def recover(home: Path, generation: str | None = None) -> dict[str, object]:
    """Recheck a recorded compatible generation without opening admission."""
    runtime = gate.Runtime(home)
    if runtime.read(verify_active=False).get("uninstalling") is True:
        uninstall(home)
        return runtime.read(verify_active=False)
    with runtime.controller():
        gate.hold_begin(runtime, acquire_controller=False)
        state = runtime.read()
        records = cast("list[dict[str, object]]", state["generations"])
        record = (
            state.get("candidate")
            if generation is None
            else next((item for item in records if item.get("id") == generation), None)
        )
        candidate = verify_generation(_object(record, "recorded recovery generation"))
        state = _compatible(state, candidate, None)
        if candidate.get("skill_sha256") is None:
            raise BusError(
                "Previous artifact has no verified skill copy; reinstall accepted source"
            )
        source = Path(str(candidate["root"])) / "skill"
        if build.skill_identity(source) != candidate["skill_sha256"]:
            raise BusError("Recorded recovery skill changed")
        destination = build.skill_destination(Path(str(state["skills_dir"])))
        state["step"] = "recovery-integration-intent"
        runtime.write(state)
        with tempfile.TemporaryDirectory(
            dir=destination.parent, prefix=".llm-bus-recovery-"
        ) as scratch:
            fresh = Path(scratch) / "fresh"
            shutil.copytree(source, fresh)
            build.swap_skill(fresh, destination, Path(scratch) / "previous")
        state["skill_sha256"] = candidate["skill_sha256"]
        state["candidate"] = candidate
        state["step"] = "recovery-verified"
        runtime.write(state)
        return state


def _remove_settings(runtime: gate.Runtime, state: dict[str, object]) -> None:
    for path in _settings_paths(runtime, state):
        before = files.current_bytes(path)
        settings = files.parse_settings(path, before)
        updated = _without_bus_settings(path, settings)
        if updated != settings:
            files.write_settings(path, updated, before, path.stat().st_mode & 0o777)


def _without_bus_settings(path: Path, settings: dict[str, object]) -> dict[str, object]:
    return apply_bus_settings(settings, path, settings_kind(path), install=False)


def _unregister(uv: str, runtime: gate.Runtime, state: dict[str, object]) -> None:
    for record in cast("list[dict[str, object]]", state["generations"]):
        _verify_layout(record)
        root = Path(str(record["root"]))
        if root.parent != runtime.root / "generations" or root.is_symlink():
            raise BusError("Refusing removal outside owned generation root")
        if Path(str(record["receipt"])).exists():
            build.run([uv, "tool", "uninstall", "llm-bus"], env=generation_environment(record))
    receipt = Path(str(state["receipt"]))
    if receipt.exists():
        if digest(receipt) != state["receipt_sha256"]:
            raise BusError("Original uv registration changed; refusing removal")
        tool, _ = build.receipt_requirement(receipt)
        binary = build.receipt_entrypoint(tool)
        build.run(
            [uv, "tool", "uninstall", "llm-bus"],
            env={
                **os.environ,
                "UV_TOOL_DIR": str(receipt.parent.parent),
                "UV_TOOL_BIN_DIR": str(binary.parent),
            },
        )


def _remove_aliases(runtime: gate.Runtime, state: dict[str, object]) -> None:
    launcher = (runtime.root / "launcher").read_bytes()
    for name in [*cast("list[str]", state["aliases"]), str(runtime.root / "llm-bus")]:
        path = Path(name)
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or path.read_bytes() != launcher:
            raise BusError(f"Public alias changed; refusing removal: {path}")
        path.unlink()


def _sync_uninstall_integrations(runtime: gate.Runtime, state: dict[str, object]) -> None:
    """Commit verified integration removal on first attempt and every retry."""
    aliases = {
        Path(name) for name in [*cast("list[str]", state["aliases"]), str(runtime.root / "llm-bus")]
    }
    settings_paths = _settings_paths(runtime, state)
    parents = {path.parent for path in [*aliases, *settings_paths]}
    for parent in parents:
        if parent.is_symlink() or any(ancestor.is_symlink() for ancestor in parent.parents):
            raise BusError(f"Symlinked integration parent; refusing completion: {parent}")
    for path in aliases:
        if path.exists() or path.is_symlink():
            raise BusError(f"Public alias recreated during uninstall: {path}")
    for path in settings_paths:
        _sync_removed_settings(path)
    for parent in sorted(parents):
        # An owner may have removed an entire integration directory meanwhile.
        existing = parent
        while not existing.exists():
            existing = existing.parent
        gate.sync_directory(existing)


def _sync_removed_settings(path: Path) -> None:
    before = files.current_bytes(path)
    settings = files.parse_settings(path, before)
    if _without_bus_settings(path, settings) != settings:
        raise BusError(f"Host integrations changed during uninstall: {path}")
    if before is not None:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
        if files.current_bytes(path) != before:
            raise BusError(f"Settings changed during uninstall: {path}")


def uninstall(home: Path) -> dict[str, object]:
    """Remove recorded registrations, retaining a held recovery tombstone."""
    runtime = gate.Runtime(home)
    uv = shutil.which("uv")
    if uv is None:
        raise BusError("uv is required to uninstall llm-bus")
    with runtime.controller():
        if runtime.read(verify_active=False).get("uninstalling") is not True:
            gate.hold_begin(runtime, acquire_controller=False)
        return _uninstall_held(uv, runtime)


def _remove_skill(runtime: gate.Runtime, state: dict[str, object]) -> dict[str, object]:
    """Retire an intact owned tree before starting recoverable recursive removal."""
    state = dict(state)
    skill = Path(str(state["skills_dir"])) / "llm-bus"
    if any(parent.is_symlink() for parent in skill.parents):
        raise BusError("Symlinked skill parent; refusing removal")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise BusError("Refusing skill removal: platform rmtree follows symlinks")
    retired = skill.with_name(f".llm-bus-uninstall-{uuid.UUID(str(state['operation']))}")
    removal = state.get("skill_removal")
    if removal is None and (skill.exists() or skill.is_symlink()):
        if build.skill_identity(skill) != state["skill_sha256"]:
            raise BusError("Skill changed during uninstall; refusing removal")
        if retired.exists() or retired.is_symlink():
            raise BusError("Unrecorded retired skill path; refusing removal")
        info = skill.stat()
        removal = {"path": str(retired), "identity": [info.st_dev, info.st_ino]}
        state["skill_removal"] = removal
        runtime.write(state)
    if removal is not None:
        recorded = _object(removal, "skill removal")
        if recorded.get("path") != str(retired):
            raise BusError("Invalid retired skill path; refusing removal")
        state = _retire_skill(runtime, state, skill, retired, recorded)
        if retired.exists() or retired.is_symlink():
            _verify_retired_skill(retired, recorded)
            shutil.rmtree(retired)
    # An absent tree still needs a durable parent entry before completion.
    gate.sync_directory(skill.parent)
    return state


def _retire_skill(
    runtime: gate.Runtime,
    state: dict[str, object],
    skill: Path,
    retired: Path,
    recorded: dict[str, object],
) -> dict[str, object]:
    if (
        not retired.exists()
        and not retired.is_symlink()
        and recorded.get("retired") is not True
        and (skill.exists() or skill.is_symlink())
    ):
        _verify_retired_skill(skill, recorded)
        if build.skill_identity(skill) != state["skill_sha256"]:
            raise BusError("Skill changed during uninstall; refusing removal")
        skill.rename(retired)
    if retired.exists() or retired.is_symlink():
        _verify_retired_skill(retired, recorded)
        # Persist the ownership transfer before removal can destroy its manifest.
        gate.sync_directory(skill.parent)
        if recorded.get("retired") is not True:
            state = {**state, "skill_removal": {**recorded, "retired": True}}
            runtime.write(state)
    return state


def _verify_retired_skill(path: Path, recorded: dict[str, object]) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != recorded.get("identity"):
        raise BusError("Retired skill identity changed; refusing removal")


def _uninstall_held(uv: str, runtime: gate.Runtime) -> dict[str, object]:
    with runtime.drained():
        state = runtime.read()
        gate.mailbox_evidence(cast("list[str]", state["mailboxes"]))
        if state.get("uninstalling") is not True:
            _verify_integrations(runtime, state)
        state.update({"step": "uninstall-intent", "uninstalling": True})
        runtime.write(state)
        _remove_settings(runtime, state)
        _unregister(uv, runtime, state)
        _remove_aliases(runtime, state)
        state = _remove_skill(runtime, state)
        _sync_uninstall_integrations(runtime, state)
        state["step"] = "uninstalled"
        runtime.write(state)
        return {"installed": False, "phase": state["phase"], "journal": str(runtime.journal)}


def operator(action: str, home_name: str, options_json: str) -> int:
    """Private controller entry, callable only by explicit operator dispatch."""
    home = Path(home_name)
    options = _object(cast("object", json.loads(options_json)), "operator arguments")
    if action not in {
        "upgrade",
        "reinstall",
        "install",
        "resume",
        "recover",
        "uninstall",
        "preview",
        "migrate",
        "verify",
    }:
        raise BusError("Unsupported installation action")
    result: dict[str, object] = {}
    try:
        if action in {"upgrade", "reinstall", "install"}:
            skills = options.get("skills_dir")
            result = install_generation(
                Path.cwd(),
                home,
                skills_dir=Path(skills) if isinstance(skills, str) else None,
                repair=action in {"reinstall", "install"},
            )
        elif action == "resume":
            evidence = options.get("evidence")
            result = resume(home, Path(evidence) if isinstance(evidence, str) else None)
            result = gate.operator_status(result)
        elif action == "recover":
            generation = options.get("generation")
            result = recover(home, generation if isinstance(generation, str) else None)
            result = {key: result[key] for key in ("operation", "phase", "step")}
        elif action == "uninstall":
            result = uninstall(home)
        elif action in {"preview", "migrate", "verify"}:
            result = candidate_operation(home, action, str(options["mailbox"]))
    except (
        BusError,
        gate.MaintenanceError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
        sqlite3.Error,
    ) as error:
        sys.stderr.write(f"llm-bus {action}: {error}; use maintenance status\n")
        return 1
    sys.stdout.write(json.dumps(result) + "\n")
    return 0

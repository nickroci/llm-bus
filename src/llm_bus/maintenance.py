"""Standard-library maintenance admission, also copied outside replaceable uv tools.

This file deliberately imports no application modules. The installed launcher and
the legacy bootstrap execute this same runtime, including its fail-closed gate.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast, final

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

# Boundary diagnostics and journal field names are explicit protocol vocabulary.
# pylint: disable=clean-code-business-policy-literal
# The legacy bootstrap copies this standard-library controller as one file.
# pylint: disable=too-many-lines
OFFLINE_MESSAGE = (
    "llm-bus is temporarily offline for maintenance. Retry in 1 minute, then every minute "
    "while this message appears. Stop retrying once the command responds normally."
)
OFFLINE_EXIT = 75
# Mirrors llm_bus.hosts.kinds.HOST_KINDS; this module imports no application modules.
HOOK_COMMANDS = (["hook", "claude"], ["hook", "codex"], ["hook", "gemini"])
PHASES = frozenset({"preparing", "held", "verified", "active"})
STATE_VERSION = 1
PROCESS_IDENTITY_FIELDS = 8
LAUNCHD_NO_SERVICE = 113


class MaintenanceError(Exception):
    """A local operation cannot safely proceed; ordinary admission stays closed."""


def owned(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    if info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode):
        raise MaintenanceError(f"Expected an owner-controlled non-symlink: {path}")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_mode & 0o077:
        raise MaintenanceError(
            f"Expected an owner-only {'directory' if directory else 'file'}: {path}"
        )
    return info


def private_directory(path: Path) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise MaintenanceError(f"Symlinked maintenance ancestor: {path}")
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700, parents=True)
    owned(path, directory=True)
    sync_entry(path)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_entry(path: Path) -> None:
    """Commit accepted maintenance bytes and ancestors, including after failed writes.

    Visible files and directories may survive a failed fsync. Maintenance writers
    repeat this barrier before certifying them; ordinary admission only validates.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    for parent in path.parents:
        sync_directory(parent)


def durable_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Replace a file without following its final symlink, then sync its parent."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        Path(temporary).replace(path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def local_lock(path: Path, operation: int) -> Generator[IO[bytes]]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        owned(path)
        fcntl.flock(stream.fileno(), operation)
        # Closing the last inherited descriptor releases the lock. An explicit
        # LOCK_UN here would incorrectly release a surviving descendant's lease.
        yield stream


@final
class Runtime:
    """One machine owner's durable journal and admission protocol.

    Controllers hold ``controller()`` for their whole operation. ``prepare()``
    closes admission atomically. ``drained()`` separately tests admitted process
    lifetimes; it makes no claim about legacy or raw database callers.
    """

    def __init__(self, home: Path) -> None:
        self.home = home
        self.root = home / ".local/share/llm-bus/maintenance"
        self.state_root = home / ".local/state/llm-bus/maintenance"
        self.journal = self.state_root / "journal.json"

    def exists(self) -> bool:
        return any(path.exists() or path.is_symlink() for path in (self.root, self.state_root))

    def initialize(self) -> None:
        private_directory(self.state_root)
        private_directory(self.root)

    @contextmanager
    def controller(self) -> Generator[None]:
        self.initialize()
        with ExitStack() as locks:
            try:
                locks.enter_context(
                    local_lock(self.state_root / "controller.lock", fcntl.LOCK_EX | fcntl.LOCK_NB)
                )
            except BlockingIOError as error:
                raise MaintenanceError("Another maintenance controller is running") from error
            yield

    def read(self, *, verify_active: bool = True) -> dict[str, object]:
        owned(self.root, directory=True)
        owned(self.state_root, directory=True)
        owned(self.journal)
        data = cast("object", json.loads(self.journal.read_text("utf-8")))
        if not isinstance(data, dict):
            raise MaintenanceError("Maintenance journal must be an object")
        state = cast("dict[str, object]", data)
        if (
            state.get("version") != STATE_VERSION
            or state.get("phase") not in PHASES
            or not isinstance(state.get("operation"), str)
            or not isinstance(state.get("aliases"), list)
        ):
            raise MaintenanceError("Invalid maintenance journal; use maintenance recover")
        if state["phase"] == "active" and verify_active:
            self.verify_runtime(state)
            if state.get("aliases_installed") is not True:
                raise MaintenanceError("Alias installation is incomplete")
            self.active_executable(state)
        return state

    def verify_runtime(self, state: dict[str, object]) -> None:
        """Validate stable runtime identity before admitting an active generation."""
        for name, key in (("runtime.py", "runtime_sha256"), ("launcher", "launcher_sha256")):
            path = self.root / name
            owned(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != state.get(key):
                raise MaintenanceError(f"Stable runtime missing or changed: {path}")

    def write(self, state: dict[str, object]) -> None:
        durable_write(self.journal, (json.dumps(state, sort_keys=True) + "\n").encode())

    def prepare(self, state: dict[str, object]) -> None:
        """Persist intent under the admission mutex before any executable mutation."""
        with local_lock(self.root / "admission.lock", fcntl.LOCK_EX):
            self.write({**state, "phase": "preparing"})

    @contextmanager
    def admit(self) -> Generator[IO[bytes] | None]:
        """Keep the lifetime lock open until the application and inherited children exit."""
        with ExitStack() as lifetime:
            with local_lock(self.root / "admission.lock", fcntl.LOCK_EX):
                if self.read()["phase"] != "active":
                    yield None
                    return
                stream = lifetime.enter_context(
                    local_lock(self.root / "lifetimes.lock", fcntl.LOCK_SH)
                )
            yield stream

    @contextmanager
    def drained(self) -> Generator[None]:
        """Raise BlockingIOError while an admitted process still holds its lifetime."""
        with local_lock(self.root / "lifetimes.lock", fcntl.LOCK_EX | fcntl.LOCK_NB):
            yield

    def active_executable(self, state: dict[str, object]) -> Path:
        active = state.get("active")
        if not isinstance(active, dict):
            raise MaintenanceError("No verified active generation")
        active = cast("dict[str, object]", active)
        name, digest = active.get("executable"), active.get("sha256")
        if not isinstance(name, str) or not Path(name).is_absolute() or not isinstance(digest, str):
            raise MaintenanceError("Invalid active generation identity")
        path = Path(name)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise MaintenanceError("Active generation missing or changed; use maintenance recover")
        if not {"receipt", "receipt_sha256", "payloads", "root"}.issubset(active):
            raise MaintenanceError("Active generation metadata is incomplete")
        receipt = Path(str(active["receipt"]))
        info = receipt.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise MaintenanceError("Active uv registration is not owner-controlled")
        if hashlib.sha256(receipt.read_bytes()).hexdigest() != active.get("receipt_sha256"):
            raise MaintenanceError(
                "Active uv registration missing or changed; use maintenance recover"
            )
        self.verify_payloads(active)
        aliases = cast("list[str]", state["aliases"])
        if path == self.root / "launcher" or path in [Path(p) for p in aliases]:
            raise MaintenanceError("Active generation points back to a public launcher")
        return path

    def verify_payloads(self, active: dict[str, object]) -> None:
        """Fail closed after interrupted storage or direct generation removal."""
        payloads = active.get("payloads")
        if not isinstance(payloads, dict) or not payloads:
            raise MaintenanceError("Active generation has no verified payload manifest")
        root = Path(str(active.get("root")))
        if root.parent != self.root / "generations":
            raise MaintenanceError("Active generation escapes its managed root")
        for name, expected in cast("dict[str, object]", payloads).items():
            payload = Path(name)
            if (
                not payload.is_relative_to(root)
                or payload.is_symlink()
                or hashlib.sha256(payload.read_bytes()).hexdigest() != expected
            ):
                raise MaintenanceError("Active generation payload missing or changed")


def command_words(arguments: Sequence[str]) -> list[str]:
    """Strip only the documented global output option, never search argument bodies."""
    words = list(arguments)
    while words:
        if words[:2] in (["--format", "json"], ["--format", "agent"]):
            words = words[2:]
        elif words[0] in {"--format=json", "--format=agent"}:
            words = words[1:]
        else:
            break
    return words


def offline(arguments: Sequence[str]) -> int:
    sys.stdout.write(OFFLINE_MESSAGE + "\n")
    return 0 if command_words(arguments) in HOOK_COMMANDS else OFFLINE_EXIT


def entrypoint(arguments: Sequence[str] | None = None) -> int:
    """Gate the public Python entrypoint before importing the application."""
    # pylint: disable=import-outside-toplevel,clean-code-commented-out-code
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    runtime = Runtime(Path.home())
    words = command_words(arguments)
    if words[:1] == ["maintenance"]:
        return operator_main(words[1:], runtime)
    if words[:1] in (["upgrade"], ["reinstall"], ["install"]) or (
        words[:1] == ["uninstall"] and runtime.exists()
    ):
        return installation_main(words, runtime)
    if not runtime.exists():
        from llm_bus.cli.app import main  # noqa: PLC0415 - application imports follow admission

        return main(arguments)
    try:
        with runtime.admit() as admission:
            if admission is None:
                return offline(arguments)
            from llm_bus.cli.app import main  # noqa: PLC0415 - application imports follow admission

            return main(arguments)
    except (MaintenanceError, OSError, ValueError, TypeError):
        return offline(arguments)


def launcher_main(runtime: Runtime, arguments: Sequence[str]) -> int:
    words = command_words(arguments)
    if words[:1] == ["maintenance"]:
        return operator_main(words[1:], runtime)
    if words[:1] in (["upgrade"], ["reinstall"], ["install"], ["uninstall"]):
        return installation_main(words, runtime)
    try:
        with runtime.admit() as admission:
            if admission is None:
                return offline(arguments)
            executable = runtime.active_executable(runtime.read())
            # Keep admission across exec and detached descendants that inherit this descriptor.
            return subprocess.call(  # noqa: S603 - verified generation, no shell
                [str(executable), *arguments], pass_fds=(admission.fileno(),)
            )
    except (MaintenanceError, OSError, ValueError, TypeError):
        return offline(arguments)


def _generation_controller(runtime: Runtime, action: str, options: dict[str, object]) -> int:
    """Load an owner-selected clean checkout only for explicit operator commands."""
    checkout = Path.cwd()
    if (
        action in {"resume", "recover", "uninstall", "preview", "migrate", "verify"}
        and not (checkout / "src/llm_bus/generations.py").is_file()
    ):
        recorded = runtime.read(verify_active=False).get("controller_checkout")
        if isinstance(recorded, str):
            checkout = Path(recorded)
    git = shutil.which("git")
    if git is None:
        raise MaintenanceError("git required for accepted-checkout controller")
    result = subprocess.run(  # noqa: S603 - exact git argv, no shell
        [git, "-C", str(checkout), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    checkout = Path(result.stdout.strip())
    dirty = subprocess.run(  # noqa: S603 - exact git argv, no shell
        [git, "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
    )
    if dirty.stdout:
        raise MaintenanceError("Tracked files have uncommitted changes; commit or stash them first")
    source = checkout / "src/llm_bus/generations.py"
    if not source.is_file():
        raise MaintenanceError("Accepted checkout lacks generation controller; hold retained")
    code = (
        "import sys; sys.path.insert(0,sys.argv[1]); "
        "from llm_bus.generations import operator; "
        "raise SystemExit(operator(sys.argv[2],sys.argv[3],sys.argv[4]))"
    )
    return subprocess.call(  # noqa: S603 - owner-selected clean checkout, private operator entry
        [
            sys.executable,
            "-I",
            "-c",
            code,
            str(checkout / "src"),
            action,
            str(runtime.home),
            json.dumps(options),
        ]
    )


def installation_main(arguments: Sequence[str], runtime: Runtime) -> int:
    """Strict operator grammar, validated before controller loading or mutation."""
    parser = argparse.ArgumentParser(prog="llm-bus", allow_abbrev=False)
    parser.add_argument("action", choices=("upgrade", "reinstall", "install", "uninstall"))
    parser.add_argument("--skills-dir", type=str)
    args = cast("dict[str, object]", vars(parser.parse_args(arguments)))
    if args["action"] == "uninstall" and args["skills_dir"] is not None:
        parser.error("--skills-dir does not apply to uninstall")
    try:
        return _generation_controller(runtime, str(args["action"]), args)
    except (MaintenanceError, OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        sys.stderr.write(f"llm-bus installation: {error}\n")
        return 1


def operator_status(state: dict[str, object]) -> dict[str, object]:
    """Project only body-free operator progress and named owner actions."""
    result = {key: state[key] for key in ("operation", "phase")}
    for key in ("step", "restart_restore_pending"):
        if key in state:
            result[key] = state[key]
    if state.get("step") in {"restart-restore-intent", "restart-restore-pending"}:
        result["next_step"] = (
            "Dispatch reopened; reconcile the named restart sources, then run maintenance resume"
        )
        names = _assisted_names(state)
        if names:
            result["restart_restore_evidence"] = {
                "operation": state["operation"],
                "assisted_restored": names,
            }
            result["next_step"] = (
                "Dispatch reopened; manually restore assisted sources, then acknowledge "
                "with maintenance resume --evidence FILE using restart_restore_evidence"
            )
    for key in ("active", "candidate"):
        item = state.get(key)
        if isinstance(item, dict):
            item = cast("dict[str, object]", item)
            result[key] = {
                name: item.get(name)
                for name in (
                    "id",
                    "revision",
                    "wheel_sha256",
                    "schema",
                    "executable",
                    "receipt",
                    "tools",
                    "bin",
                )
            }
    return result


def operator_main(arguments: Sequence[str], runtime: Runtime) -> int:
    """Body-free pre-import operator dispatch."""
    parser = argparse.ArgumentParser(prog="llm-bus maintenance", allow_abbrev=False)
    parser.add_argument(
        "action", choices=("begin", "status", "resume", "recover", "preview", "migrate", "verify")
    )
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--evidence", type=str)
    parser.add_argument("--generation", type=str)
    parser.add_argument("--mailbox", type=str)
    args = cast("dict[str, object]", vars(parser.parse_args(arguments)))
    inventory = cast("Path | None", args["inventory"])
    if inventory is not None and args["action"] != "begin":
        parser.error("--inventory applies only to begin")
    if args["evidence"] is not None and args["action"] != "resume":
        parser.error("--evidence applies only to resume")
    if args["generation"] is not None and args["action"] != "recover":
        parser.error("--generation applies only to recover")
    if (args["mailbox"] is not None) != (args["action"] in {"preview", "migrate", "verify"}):
        parser.error("preview, migrate and verify require --mailbox; other actions reject it")
    try:
        if args["action"] == "status":
            state = runtime.read()
            result = operator_status(state)
            sys.stdout.write(json.dumps(result) + "\n")
            return 0
        if (
            args["action"] in {"resume", "recover", "preview", "migrate", "verify"}
            and runtime.read(verify_active=False).get("candidate") is not None
        ):
            return _generation_controller(runtime, str(args["action"]), args)
        if args["action"] in {"begin", "recover"}:
            state = hold_begin(runtime, inventory)
            sys.stdout.write(json.dumps({key: state[key] for key in ("operation", "phase")}) + "\n")
            return 0
    except (MaintenanceError, OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        sys.stderr.write(f"llm-bus maintenance: {error}\n")
        return 1
    sys.stderr.write(
        "llm-bus maintenance: Candidate compatibility and integration verification required; "
        "hold retained\n"
    )
    return 1


def _inspect(arguments: Sequence[str], *, missing_ok: bool = False) -> str:
    """Run a fixed native inspection/control operation without a shell or mailbox imports."""
    result = subprocess.run(  # noqa: S603 - fixed native argv, no shell
        arguments, capture_output=True, text=True, check=False, timeout=15
    )
    if result.returncode and not (missing_ok and result.returncode == 1 and not result.stderr):
        raise MaintenanceError(
            f"Native operation failed: {arguments[0]} (exit {result.returncode})"
        )
    if result.stderr:
        raise MaintenanceError(f"Incomplete native inspection: {arguments[0]}")
    return result.stdout


def _control(arguments: Sequence[str]) -> None:
    result = subprocess.run(  # noqa: S603 - typed supervisor argv, no shell
        arguments, capture_output=True, text=True, check=False, timeout=15
    )
    if result.returncode:
        raise MaintenanceError(
            f"Supervisor control failed: {arguments[0]} (exit {result.returncode})"
        )


def is_host_command(words: Sequence[str]) -> bool:
    """Claude Code, Codex, or Gemini CLI, which runs as `node .../gemini` (see hosts.processes)."""
    if not words:
        return False
    name = Path(words[0]).name.lower()
    if name in {"codex", "claude", "gemini"}:
        return True
    script = next((word for word in words[1:] if not word.startswith("-")), "")
    return name in {"node", "nodejs"} and (
        Path(script).name in {"gemini", "gemini.js"} or "/@google/gemini-cli/" in script
    )


def process_inventory() -> list[dict[str, object]]:
    """Snapshot exact owner process identities; hash argv to avoid retaining command secrets."""
    output = _inspect(
        ["/bin/ps", "-ww", "-u", str(os.getuid()), "-o", "pid=,ppid=,lstart=,command="]
    )
    result: list[dict[str, object]] = []
    for line in output.splitlines():
        fields = line.split(None, 7)
        if len(fields) != PROCESS_IDENTITY_FIELDS:
            raise MaintenanceError("Cannot parse complete process identity")
        pid, parent = int(fields[0]), int(fields[1])
        command = fields[7]
        try:
            words = shlex.split(command)
        except ValueError:
            words = []
        is_bus = bool(words) and (
            Path(words[0]).name == "llm-bus"
            or any(words[index : index + 2] == ["-m", "llm_bus"] for index in range(len(words)))
            or any(Path(word).name == "llm-bus" for word in words[1:2])
        )
        result.append(
            {
                "pid": pid,
                "parent": parent,
                "started": " ".join(fields[2:7]),
                "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
                "bus": is_bus,
                "host": is_host_command(words),
            }
        )
    return result


def _process_matches(record: dict[str, object], current: dict[str, object]) -> bool:
    return all(record.get(key) == current.get(key) for key in ("pid", "started", "command_sha256"))


def _validate_mailboxes(runtime: Runtime, mailboxes: object) -> None:
    if not isinstance(mailboxes, list) or not mailboxes:
        raise MaintenanceError("Declare every mailbox, including dormant client overrides")
    for name in cast("list[object]", mailboxes):
        if not isinstance(name, str) or not Path(name).is_absolute():
            raise MaintenanceError("Mailbox identities must be absolute paths")
        if any(part.is_symlink() for part in (Path(name), *Path(name).parents)):
            raise MaintenanceError(f"Retarget symlinked mailbox explicitly: {name}")
    expected = {str(runtime.home / ".local/share/llm-bus/bus.sqlite3")}
    if os.environ.get("LLM_BUS_DB"):
        expected.add(str(Path(os.environ["LLM_BUS_DB"]).expanduser().absolute()))
    if not expected.issubset(set(cast("list[str]", mailboxes))):
        raise MaintenanceError("Inventory omits default mailbox or LLM_BUS_DB override")


def _access_inventory(runtime: Runtime, path: Path) -> dict[str, object]:
    owned(path)
    data = json_object(path)
    _validate_inventory(runtime, data)
    return data


def _validate_inventory(runtime: Runtime, data: dict[str, object]) -> None:
    required = {
        "version",
        "complete",
        "raw_clients_stopped",
        "hosts_gated",
        "mailboxes",
        "processes",
        "restart_sources",
    }
    if set(data) != required or data.get("version") != 1:
        raise MaintenanceError("Invalid access inventory fields/version")
    if any(data.get(key) is not True for key in ("complete", "raw_clients_stopped", "hosts_gated")):
        raise MaintenanceError(
            "Confirm complete inventory, stopped raw clients and gated host paths"
        )
    _validate_mailboxes(runtime, data["mailboxes"])
    for key in ("processes", "restart_sources"):
        if not isinstance(data[key], list) or any(
            not isinstance(item, dict) for item in cast("list[object]", data[key])
        ):
            raise MaintenanceError(f"Invalid {key} inventory")
    for process in cast("list[dict[str, object]]", data["processes"]):
        pid = process.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            raise MaintenanceError("Invalid process PID")
        if (
            set(process) != {"pid", "started", "command_sha256"}
            or not isinstance(process["started"], str)
            or not isinstance(process["command_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", str(process["command_sha256"])) is None
        ):
            raise MaintenanceError("Invalid exact process identity")


def _launchd_job(identity: str) -> dict[str, object]:
    result = subprocess.run(  # noqa: S603 - validated launchd label, no shell
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{identity}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode == LAUNCHD_NO_SERVICE and "Could not find service" in result.stderr:
        return {"loaded": False, "plist": None, "running": False}
    if result.returncode or result.stderr:
        raise MaintenanceError(f"Cannot inspect launchd job: {identity}")
    match = re.search(r"^\s*path = (/.+)$", result.stdout, re.MULTILINE)
    if match is None:
        raise MaintenanceError(
            f"No restorable plist for launchd job: {identity}; use assisted stop"
        )
    return {
        "loaded": True,
        "plist": match[1],
        "running": re.search(r"^\s*state = running$", result.stdout, re.MULTILINE) is not None,
    }


def _source_identity(source: dict[str, object]) -> tuple[str, str]:
    kind, identity = source.get("kind"), source.get("identity")
    if kind not in {"launchd", "systemd-user", "assisted"} or not isinstance(identity, str):
        raise MaintenanceError("Unknown restart source; use explicit assisted stop confirmation")
    if not identity or identity.startswith("-") or re.fullmatch(r"[\w.@:-]+", identity) is None:
        raise MaintenanceError("Restart identity must be a literal label, unit or caller name")
    return str(kind), identity


def _launchd_plist(path: Path) -> bytes:
    info = path.lstat()
    if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise MaintenanceError(f"Expected an owner-controlled launchd plist: {path}")
    return path.read_bytes()


def _source_state(source: dict[str, object]) -> dict[str, object]:
    kind, identity = _source_identity(source)
    if kind == "assisted":
        if (
            set(source) != {"kind", "identity", "stopped", "restart_disabled"}
            or source.get("stopped") is not True
            or source.get("restart_disabled") is not True
        ):
            raise MaintenanceError(f"Operator must stop caller and all restart sources: {identity}")
        return {"confirmed_stopped": True}
    if set(source) != {"kind", "identity"}:
        raise MaintenanceError("Unexpected supervisor fields")
    if kind == "systemd-user" and sys.platform == "linux" and identity.endswith(".service"):
        values = _inspect(
            [
                "systemctl",
                "--user",
                "show",
                identity,
                "--property=LoadState,ActiveState,UnitFileState",
            ]
        )
        state = dict(line.split("=", 1) for line in values.splitlines() if "=" in line)
        if state.get("LoadState") not in {"loaded", "masked"} or not state.get("UnitFileState"):
            raise MaintenanceError(f"Unknown user unit: {identity}")
        return cast("dict[str, object]", state)
    if kind == "launchd" and sys.platform == "darwin":
        target = f"gui/{os.getuid()}"
        disabled = _inspect(["/bin/launchctl", "print-disabled", target])
        match = re.search(r'"' + re.escape(identity) + r'"\s*=>\s*(true|false)', disabled)
        # Missing entries in print-disabled mean the default (enabled).
        state = {"disabled": match is not None and match[1] == "true", **_launchd_job(identity)}
        if state["loaded"] is True:
            plist = Path(str(state["plist"]))
            content = _launchd_plist(plist)
            value = cast("object", plistlib.loads(content))
            if (
                not isinstance(value, dict)
                or cast("dict[str, object]", value).get("Label") != identity
            ):
                raise MaintenanceError(
                    f"Launchd plist label differs from recorded source: {identity}"
                )
            definition = cast("dict[str, object]", value)
            state["plist_sha256"] = hashlib.sha256(content).hexdigest()
            state["starts_on_load"] = bool(
                definition.get("RunAtLoad") or definition.get("KeepAlive")
            )
            if state["disabled"] is True or (
                state["running"] is not True and state["starts_on_load"] is True
            ):
                raise MaintenanceError(
                    f"Use assisted stop for disabled/auto-starting stopped job: {identity}"
                )
        return state
    raise MaintenanceError(f"Unsupported supervisor/platform: {kind}; use assisted stop")


def _suspend_source(source: dict[str, object]) -> None:
    kind, identity = _source_identity(source)
    if kind == "systemd-user":
        _control(["systemctl", "--user", "mask", "--now", identity])
    elif kind == "launchd":
        target = f"gui/{os.getuid()}/{identity}"
        _control(["/bin/launchctl", "disable", target])
        if _launchd_job(identity)["loaded"] is True:
            _control(["/bin/launchctl", "bootout", target])


def _verify_source(source: dict[str, object]) -> None:
    state = _source_state(source)
    kind, identity = _source_identity(source)
    if kind == "systemd-user" and not (
        state.get("UnitFileState") == "masked"
        and state.get("ActiveState") in {"inactive", "failed"}
    ):
        raise MaintenanceError(f"Restart source still active or unmasked: {identity}")
    if kind == "launchd" and (
        state.get("disabled") is not True or state.get("loaded") is not False
    ):
        raise MaintenanceError(f"Restart source remains enabled or loaded: {identity}")


def _restore_actions(record: dict[str, object]) -> list[dict[str, object]]:
    source = cast("dict[str, object]", record["source"])
    prior = cast("dict[str, object]", record["prior"])
    kind, identity = _source_identity(source)
    actions: list[dict[str, object]] = []
    if kind == "assisted":
        raise MaintenanceError(f"Owner must restore assisted restart source: {identity}")
    if kind == "systemd-user":
        if prior.get("UnitFileState") == "masked" and prior.get("ActiveState") == "inactive":
            return actions
        if prior.get("LoadState") != "loaded" or prior.get("ActiveState") not in {
            "active",
            "inactive",
            "failed",
        }:
            raise MaintenanceError(f"Owner must restore unsupported prior unit state: {identity}")
        stopped = {**prior, "ActiveState": "inactive"}
        actions.append({"argv": ["systemctl", "--user", "unmask", identity], "after": stopped})
        if prior["ActiveState"] == "active":
            actions.append({"argv": ["systemctl", "--user", "start", identity], "after": prior})
    else:
        target = f"gui/{os.getuid()}/{identity}"
        if prior.get("disabled") is False:
            actions.append(
                {
                    "argv": ["/bin/launchctl", "enable", target],
                    "after": {"disabled": False, "loaded": False, "plist": None, "running": False},
                }
            )
        if prior.get("loaded") is True:
            if "starts_on_load" not in prior or "plist_sha256" not in prior:
                raise MaintenanceError(
                    f"Missing recorded launchd definition; owner action required: {identity}"
                )
            loaded = {**prior, "running": prior["starts_on_load"]}
            actions.append(
                {
                    "argv": ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", prior["plist"]],
                    "after": loaded,
                    "plist_sha256": prior["plist_sha256"],
                }
            )
            if prior.get("running") is True and prior["starts_on_load"] is False:
                actions.append({"argv": ["/bin/launchctl", "kickstart", target], "after": prior})
    return actions


def _restore_source(
    runtime: Runtime, state: dict[str, object], source_index: int
) -> dict[str, object]:
    state = dict(state)
    records = [dict(item) for item in cast("list[dict[str, object]]", state["restart_sources"])]
    state["restart_sources"] = records
    record = records[source_index]
    source = cast("dict[str, object]", record["source"])
    _, identity = _source_identity(source)
    if "restore_actions" not in record:
        actions = _restore_actions(record)
        _verify_source(source)
        record["restore_actions"] = actions
        record["restore_index"] = 0
        record["restore_expected"] = _source_state(source)
        runtime.write(state)
    actions = cast("list[dict[str, object]]", record["restore_actions"])
    first = int(str(record["restore_index"]))
    for index in range(first, len(actions)):
        action = actions[index]
        current = _source_state(source)
        if current != action["after"]:
            if current != record["restore_expected"]:
                raise MaintenanceError(f"Restart source changed; owner action required: {identity}")
            if "plist_sha256" in action:
                plist = Path(cast("list[str]", action["argv"])[-1])
                if hashlib.sha256(_launchd_plist(plist)).hexdigest() != action["plist_sha256"]:
                    raise MaintenanceError(
                        f"Launchd definition changed; owner action required: {identity}"
                    )
            # The persisted action is the intent; after a crash its exact
            # postcondition is recognized without repeating the control.
            _control(cast("list[str]", action["argv"]))
            if _source_state(source) != action["after"]:
                raise MaintenanceError(f"Restart restoration not verified: {identity}")
        record["restore_expected"] = action["after"]
        record["restore_index"] = index + 1
        runtime.write(state)
    record["restored"] = True
    runtime.write(state)
    return state


def _assisted_names(state: dict[str, object]) -> list[str]:
    names: list[str] = []
    for record in cast("list[dict[str, object]]", state.get("restart_sources", [])):
        kind, identity = _source_identity(cast("dict[str, object]", record["source"]))
        if kind == "assisted" and record.get("restored") is not True:
            names.append(identity)
    if len(names) != len(set(names)):
        raise MaintenanceError("Ambiguous recorded assisted restart sources")
    return sorted(names)


def restore_sources(
    runtime: Runtime, state: dict[str, object], evidence_path: Path | None = None
) -> dict[str, object]:
    """Restore only recorded sources after compatible publication; retry exact intents."""
    if state["phase"] != "active":
        raise MaintenanceError("Cannot restore restart sources before compatible publication")
    state = dict(state)
    if evidence_path is not None:
        owned(evidence_path)
        names = _assisted_names(state)
        evidence = json_object(evidence_path)
        if not names or evidence != {
            "operation": state["operation"],
            "assisted_restored": names,
        }:
            raise MaintenanceError(
                "Restart evidence must bind the exact operation and all pending assisted sources"
            )
        records = [dict(item) for item in cast("list[dict[str, object]]", state["restart_sources"])]
        for record in records:
            kind, identity = _source_identity(cast("dict[str, object]", record["source"]))
            if kind == "assisted" and identity in names:
                record["restored"] = True
        state["restart_sources"] = records
        state["restart_restore_evidence"] = evidence
        runtime.write(state)
    pending: list[str] = []
    for index, record in enumerate(
        cast("list[dict[str, object]]", state.get("restart_sources", []))
    ):
        source = cast("dict[str, object]", record["source"])
        kind, identity = _source_identity(source)
        if record.get("restored") is True:
            continue
        try:
            state = _restore_source(runtime, state, index)
        except MaintenanceError as error:
            state = runtime.read()
            pending.append(f"{kind}/{identity}: {error}")
    state["restart_restore_pending"] = pending
    state["step"] = "restart-restore-pending" if pending else "released"
    runtime.write(state)
    return state


def mailbox_evidence(names: Sequence[str]) -> list[dict[str, object]]:
    executable = next(
        (str(path) for path in (Path("/usr/sbin/lsof"), Path("/usr/bin/lsof")) if path.is_file()),
        shutil.which("lsof"),
    )
    if executable is None or sys.platform not in {"darwin", "linux"}:
        raise MaintenanceError("Complete mailbox handle inspection requires lsof on macOS/Linux")
    evidence: list[dict[str, object]] = []
    for name in names:
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(name + suffix)
            if path.is_symlink():
                raise MaintenanceError(f"Symlinked mailbox companion: {path}")
            if not path.exists():
                evidence.append({"path": str(path), "absent": True})
                continue
            info = path.stat()
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
                raise MaintenanceError(f"Mailbox is not an owner-controlled regular file: {path}")
            output = _inspect([executable, "-nP", "-F", "p", "--", str(path)], missing_ok=True)
            if output.strip():
                raise MaintenanceError(
                    f"Unexplained mailbox accessor: {path} ({output.strip()}); "
                    "stop caller and restart source"
                )
            after = path.stat()
            identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
            if identity != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns]:
                raise MaintenanceError(f"Mailbox changed during inspection: {path}")
            evidence.append({"path": str(path), "identity": identity})
    return evidence


def _stop_processes(records: Sequence[dict[str, object]]) -> None:
    current = {int(str(item["pid"])): item for item in process_inventory()}
    ancestors = {os.getpid()}
    parent = os.getppid()
    while parent in current and parent not in ancestors:
        ancestors.add(parent)
        parent = int(str(current[parent]["parent"]))
    for record in records:
        pid = int(str(record["pid"]))
        found = current.get(pid)
        # Keep historical identities, but never act on an unrelated replacement PID.
        if found is None or not _process_matches(record, found):
            continue
        if pid in ancestors:
            raise MaintenanceError("Refusing to stop controller or an agent-host ancestor")
        if found["host"] is True:
            raise MaintenanceError(
                f"Keep whole agent host running; retarget its bus access: PID {pid}"
            )
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while True:
        live = process_inventory()
        if not any(_process_matches(record, item) for record in records for item in live):
            break
        if time.monotonic() >= deadline:
            raise MaintenanceError("Identified processes have not exited; hold retained")
        time.sleep(0.05)
    if any(item["bus"] is True and item["pid"] != os.getpid() for item in live):
        raise MaintenanceError("Uninventoried bus process remains; stop it and its restart source")


def _extend_inventory(prior: dict[str, object], inventory: dict[str, object]) -> None:
    """Permit added blockers/confirmations while retaining every recorded access path."""
    for key in ("mailboxes", "processes"):
        old = cast("list[object]", prior[key])
        new = cast("list[object]", inventory[key])
        if any(item not in new for item in old):
            raise MaintenanceError(f"Cannot discard recorded {key} during recovery")
    old_sources = cast("list[dict[str, object]]", prior["restart_sources"])
    new_sources = cast("list[dict[str, object]]", inventory["restart_sources"])
    if not {_source_identity(source) for source in old_sources}.issubset(
        {_source_identity(source) for source in new_sources}
    ):
        raise MaintenanceError("Cannot discard recorded restart sources during recovery")


def verify_aliases(runtime: Runtime, state: dict[str, object]) -> None:
    runtime.verify_runtime(state)
    aliases = [Path(str(name)) for name in cast("list[object]", state["aliases"])]
    discovered = inventory_aliases(runtime.home, Path(str(state["receipt"])), aliases, ())
    if {record["path"] for record in discovered} != {str(path) for path in aliases}:
        raise MaintenanceError("Alias coverage changed; retarget or stop newly discovered callers")
    launcher = (runtime.root / "launcher").read_bytes()
    if any(
        path.is_symlink() or path.read_bytes() != launcher or not path.stat().st_mode & stat.S_IXUSR
        for path in aliases
    ):
        raise MaintenanceError("Public alias no longer routes through stable maintenance launcher")


def _suspend_record(
    runtime: Runtime, state: dict[str, object], source_index: int
) -> dict[str, object]:
    state = dict(state)
    records = [dict(item) for item in cast("list[dict[str, object]]", state["restart_sources"])]
    state["restart_sources"] = records
    record = records[source_index]
    source = cast("dict[str, object]", record["source"])
    if "suspended" in record:
        if _source_state(source) != record["suspended"]:
            raise MaintenanceError("Recorded suspended source changed; owner action required")
    else:
        _suspend_source(source)
    _verify_source(source)
    record["suspended"] = _source_state(source)
    runtime.write(state)
    return state


def hold_begin(
    runtime: Runtime, inventory_path: Path | None = None, *, acquire_controller: bool = True
) -> dict[str, object]:
    """Complete only a proven stopped-access barrier; retries never reopen service.

    Generation operations pass acquire_controller=False while holding their
    controller lock across the complete hold, installation and release.
    """
    with runtime.controller() if acquire_controller else nullcontext():
        state = runtime.read(verify_active=False)
        if inventory_path is not None:
            inventory = _access_inventory(runtime, inventory_path)
        else:
            recorded_inventory = state.get("access_inventory")
            if recorded_inventory is not None and not isinstance(recorded_inventory, dict):
                raise MaintenanceError("Malformed recorded access inventory; hold retained")
            inventory = cast("dict[str, object] | None", recorded_inventory)
        if state["phase"] == "active":
            if state.get("step") in {"restart-restore-intent", "restart-restore-pending"}:
                raise MaintenanceError(
                    "Restart restoration unfinished; reconcile owner actions "
                    "and run maintenance resume"
                )
            state.update(
                {"operation": str(uuid.uuid4()), "baseline_absent": [], "fresh_initializations": {}}
            )
            state.pop("baseline_schemas", None)
            state.pop("initial_client_schema", None)
            state.pop("integration_snapshots", None)
            state.pop("restart_sources", None)
            state.pop("restart_restore_pending", None)
            state.pop("restart_restore_evidence", None)
        runtime.prepare(state)
        state = runtime.read()
        if inventory is None:
            raise MaintenanceError("Stopped-access inventory required; use begin --inventory FILE")
        _validate_inventory(runtime, inventory)
        prior = state.get("access_inventory")
        if isinstance(prior, dict):
            _extend_inventory(cast("dict[str, object]", prior), inventory)
        state.update(
            {"access_inventory": inventory, "mailboxes": inventory["mailboxes"], "verification": {}}
        )
        runtime.write(state)
        state = install_aliases(runtime, state)
        verify_aliases(runtime, state)
        sources = cast("list[dict[str, object]]", inventory["restart_sources"])
        restart = cast("list[dict[str, object]]", state.get("restart_sources", []))
        recorded = {_source_identity(cast("dict[str, object]", item["source"])) for item in restart}
        restart: list[dict[str, object]] = [
            *restart,
            *[
                {"source": source, "prior": _source_state(source)}
                for source in sources
                if _source_identity(source) not in recorded
            ],
        ]
        state["restart_sources"] = restart
        runtime.write(state)  # Durable prior restart state precedes every stop/mask.
        for index in range(len(restart)):
            state = _suspend_record(runtime, state, index)
        _stop_processes(cast("list[dict[str, object]]", inventory["processes"]))
        try:
            with runtime.drained():
                evidence = mailbox_evidence(cast("list[str]", inventory["mailboxes"]))
                verify_aliases(runtime, state)
                for source in sources:
                    _verify_source(source)
                state["verification"] = {
                    "aliases": True,
                    "lifetimes_drained": True,
                    "processes_stopped": True,
                    "restart_sources_stopped": True,
                    "mailbox_handles": evidence,
                }
                state["phase"] = "held"
                runtime.write(state)
        except BlockingIOError as error:
            raise MaintenanceError(
                "Admitted command or detached descendant remains alive"
            ) from error
        return state


def _alias_snapshot(path: Path) -> dict[str, object]:
    if not path.is_absolute() or any(parent.is_symlink() for parent in path.parents):
        raise MaintenanceError(f"Alias must have an absolute, non-symlinked parent: {path}")
    info = path.lstat()
    if info.st_uid != os.getuid() or not os.access(path.parent, os.W_OK):
        raise MaintenanceError(f"Alias is not owner-controlled and writable: {path}")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise MaintenanceError(
            f"Hardlinked executable requires explicit caller retargeting: {path}"
        )
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise MaintenanceError(f"Unsupported alias: {path}")
    target = str(path.readlink()) if path.is_symlink() else None
    content = b"" if target is not None else path.read_bytes()
    return {
        "path": str(path),
        "mode": stat.S_IMODE(info.st_mode),
        "link": target,
        "bytes": base64.b64encode(content).decode("ascii"),
        "identity": [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size],
    }


def json_object(path: Path) -> dict[str, object]:
    data = cast("object", json.loads(path.read_text("utf-8")))
    if not isinstance(data, dict):
        raise MaintenanceError(f"Expected JSON object: {path}")
    return cast("dict[str, object]", data)


def _hook_executable(command: str, path: Path) -> Path:
    words = shlex.split(command)
    if not words or Path(words[0]).name != "llm-bus" or words[1:] not in HOOK_COMMANDS:
        raise MaintenanceError(f"Ambiguous bus hook command in {path}")
    executable = Path(words[0])
    if executable.is_absolute():
        return executable
    located = shutil.which(words[0])
    if located is None:
        raise MaintenanceError(f"Unresolved bus hook in {path}")
    return Path(located).absolute()


def hook_aliases(path: Path) -> set[Path]:
    """Read effective/remembered settings without importing host or mailbox modules."""
    if not path.exists():
        return set()
    if path.is_symlink():
        raise MaintenanceError(f"Retarget symlinked host settings explicitly: {path}")
    result: set[Path] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            node = cast("dict[str, object]", value)
            command = node.get("command")
            if isinstance(command, str) and "llm-bus" in command:
                result.add(_hook_executable(command, path))
            for child in node.values():
                visit(child)
        elif isinstance(value, list):
            for child in cast("list[object]", value):
                visit(child)

    visit(json_object(path))
    return result


def _receipt_aliases(receipt: Path) -> set[Path]:
    data = cast("dict[str, object]", tomllib.loads(receipt.read_text("utf-8")))
    tool = data.get("tool")
    if not isinstance(tool, dict):
        raise MaintenanceError("uv receipt has no tool table")
    entries = cast("dict[str, object]", tool).get("entrypoints")
    if not isinstance(entries, list):
        raise MaintenanceError("uv receipt has no entrypoints")
    known: set[Path] = set()
    for entry in cast("list[object]", entries):
        if isinstance(entry, dict):
            entry = cast("dict[str, object]", entry)
            if entry.get("name") == "llm-bus" and isinstance(entry.get("install-path"), str):
                known.add(Path(str(entry["install-path"])))
    if not known:
        raise MaintenanceError("uv receipt has no llm-bus executable")
    return known


def host_settings(home: Path, host_homes: Sequence[Path]) -> set[Path]:
    settings = {
        home / ".codex/hooks.json",
        home / ".claude/settings.json",
        Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "hooks.json",
        Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude"))) / "settings.json",
    }
    # Gemini CLI settings count only once Gemini has made its folder. The bus never makes it.
    gemini = Path(os.environ.get("GEMINI_CLI_HOME", str(home))).expanduser() / ".gemini"
    if gemini.is_dir():
        settings.add(gemini / "settings.json")
    metadata = home / ".local/share/llm-bus/native-hosts.json"
    if metadata.exists():
        homes = json_object(metadata).get("codex_homes")
        if not isinstance(homes, list):
            raise MaintenanceError("Invalid remembered host homes")
        for folder in cast("list[object]", homes):
            if not isinstance(folder, str) or not Path(folder).is_absolute():
                raise MaintenanceError("Invalid remembered host home")
            settings.add(Path(folder) / "hooks.json")
    for folder in host_homes:
        settings.update((folder / "hooks.json", folder / "settings.json"))
    return {path.expanduser().absolute() for path in settings}


def inventory_aliases(
    home: Path, receipt: Path, declared: Sequence[Path], host_homes: Sequence[Path]
) -> list[dict[str, object]]:
    """Inventory exact uv paths, PATH matches, cached hooks, and declared client paths."""
    known = _receipt_aliases(receipt)
    known.update(path.absolute() for path in declared)
    targets = {path.resolve(strict=True) for path in known}
    discovered: set[Path] = set()
    for folder in os.get_exec_path():
        candidate = Path(folder).absolute() / "llm-bus"
        if candidate.exists() or candidate.is_symlink():
            discovered.add(candidate)
    for path in host_settings(home, host_homes):
        discovered.update(hook_aliases(path))
    for candidate in discovered:
        if candidate not in known and candidate.resolve(strict=True) not in targets:
            raise MaintenanceError(
                f"Unregistered copied executable; declare or stop its callers: {candidate}"
            )
    paths = known | targets | discovered
    return [_alias_snapshot(path) for path in sorted(paths)]


def launcher_bytes(runtime: Runtime, python: Path) -> bytes:
    """A POSIX trampoline supports long Python paths and spaces without PATH lookup."""
    return (
        "#!/bin/sh\nexec " + shlex.join([str(python), str(runtime.root / "runtime.py")]) + ' "$@"\n'
    ).encode()


def install_aliases(runtime: Runtime, state: dict[str, object]) -> dict[str, object]:
    """Complete journaled alias replacements idempotently after a partial failure."""
    runtime.verify_runtime(state)
    for name in ("runtime.py", "launcher"):
        sync_entry(runtime.root / name)
    launcher = (runtime.root / "launcher").read_bytes()
    records = cast("list[dict[str, object]]", state["originals"])
    for record in records:
        path = Path(str(record["path"]))
        if path.is_symlink() or path.read_bytes() != launcher:
            if _alias_snapshot(path) != record:
                raise MaintenanceError(f"Alias changed since intent was recorded: {path}")
            durable_write(path, launcher, 0o700)
        owned(path)
        sync_entry(path)
    for name in cast("list[str]", state.get("created_aliases", [])):
        path = Path(name)
        if path != runtime.root / "llm-bus":
            raise MaintenanceError("Unexpected managed hook alias")
        if path.is_symlink() or (path.exists() and path.read_bytes() != launcher):
            raise MaintenanceError("Managed hook alias changed")
        if not path.exists():
            durable_write(path, launcher, 0o700)
        owned(path)
        sync_entry(path)
    installed = {**state, "aliases_installed": True}
    runtime.write(installed)
    return installed


def bootstrap_begin(
    runtime: Runtime,
    receipt: Path,
    skills_dir: Path,
    declared: Sequence[Path] = (),
    host_homes: Sequence[Path] = (),
) -> dict[str, object]:
    """Gate legacy executables. Preparing is explicitly not a completed local hold."""
    python = Path(sys.executable).resolve(strict=True)
    if not python.is_file() or runtime.root in python.parents:
        raise MaintenanceError("Select a Python 3.12 interpreter outside managed generations")
    if sys.version_info[:2] != (3, 12):
        raise MaintenanceError("Legacy bootstrap requires explicitly selected Python 3.12")
    with runtime.controller():
        previous_runtime: dict[str, object] | None = None
        if runtime.journal.exists():
            state = runtime.read()
            if state.get("uninstalling") is True and state.get("step") == "uninstalled":
                runtime.verify_runtime(state)
                previous_runtime = {
                    "runtime_sha256": state["runtime_sha256"],
                    "launcher_sha256": state["launcher_sha256"],
                }
                retired = runtime.root / "retired"
                private_directory(retired)
                operation = str(uuid.UUID(str(state["operation"])))
                durable_write(retired / f"{operation}.json", runtime.journal.read_bytes())
            elif state["phase"] == "active":
                raise MaintenanceError("Managed install already active; use maintenance begin")
            else:
                return _complete_bootstrap(runtime, state)
        elif any((runtime.root / name).exists() for name in ("runtime.py", "launcher")):
            raise MaintenanceError("Managed runtime has no journal; restore the recorded operation")
        if skills_dir.is_symlink() or (skills_dir / "llm-bus").is_symlink():
            raise MaintenanceError("Skill destination must not be a symlink")
        originals = inventory_aliases(runtime.home, receipt, declared, host_homes)
        source = Path(__file__).read_bytes()
        launcher = launcher_bytes(runtime, python)
        state: dict[str, object] = {
            "version": STATE_VERSION,
            "operation": str(uuid.uuid4()),
            "phase": "preparing",
            "aliases": [record["path"] for record in originals],
            "originals": originals,
            "aliases_installed": False,
            "receipt": str(receipt.absolute()),
            "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            "skills_dir": str(skills_dir.absolute()),
            "settings_paths": [
                str(path) for path in sorted(host_settings(runtime.home, host_homes))
            ],
            "python": str(python),
            "runtime_sha256": hashlib.sha256(source).hexdigest(),
            "launcher_sha256": hashlib.sha256(launcher).hexdigest(),
            "active": None,
            "generations": [],
            "mailboxes": [],
            "verification": {},
        }
        if previous_runtime is not None:
            state["previous_runtime"] = previous_runtime
        runtime.prepare(state)
        return _complete_bootstrap(runtime, state)


def _complete_bootstrap(runtime: Runtime, state: dict[str, object]) -> dict[str, object]:
    """Complete only the recorded old-to-new runtime transition, never owner edits."""
    source = Path(__file__).read_bytes()
    launcher = launcher_bytes(runtime, Path(str(state["python"])))
    if hashlib.sha256(source).hexdigest() != state.get("runtime_sha256"):
        raise MaintenanceError("Recovery checkout differs from the recorded bootstrap runtime")
    if hashlib.sha256(launcher).hexdigest() != state.get("launcher_sha256"):
        raise MaintenanceError("Recorded launcher identity is inconsistent")
    previous = state.get("previous_runtime")
    if previous is not None and (
        not isinstance(previous, dict)
        or set(cast("dict[str, object]", previous)) != {"runtime_sha256", "launcher_sha256"}
        or state["phase"] != "preparing"
        or state.get("aliases_installed") is not False
    ):
        raise MaintenanceError("Invalid recorded runtime replacement intent")
    allowed = cast("dict[str, object]", previous or {})
    replacements = (
        (runtime.root / "runtime.py", source, "runtime_sha256", 0o600),
        (runtime.root / "launcher", launcher, "launcher_sha256", 0o700),
    )
    for path, expected, key, _mode in replacements:
        if path.exists() or path.is_symlink():
            owned(path)
            current = path.read_bytes()
            if current != expected and hashlib.sha256(current).hexdigest() != allowed.get(key):
                raise MaintenanceError(
                    f"Recorded runtime changed; refusing recovery overwrite: {path}"
                )
    for path, expected, _key, mode in replacements:
        if not path.exists() or path.read_bytes() != expected:
            durable_write(path, expected, mode)
    state = dict(state)
    state.pop("previous_runtime", None)
    return install_aliases(runtime, state)


def _bootstrap_recover(runtime: Runtime) -> dict[str, object]:
    with runtime.controller():
        state = runtime.read()
        if state["phase"] == "active":
            raise MaintenanceError("Use managed maintenance recover for an active install")
        return _complete_bootstrap(runtime, state)


def bootstrap_main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap local llm-bus maintenance", allow_abbrev=False
    )
    parser.add_argument("action", choices=("begin", "status", "recover"))
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--skills-dir", type=Path)
    parser.add_argument("--alias", type=Path, action="append", default=[])
    parser.add_argument("--host-home", type=Path, action="append", default=[])
    args = cast("dict[str, object]", vars(parser.parse_args(arguments)))
    if args["action"] != "begin" and any(
        args[key] for key in ("receipt", "skills_dir", "alias", "host_home")
    ):
        parser.error("--receipt, --skills-dir, --alias and --host-home apply only to begin")
    runtime = Runtime(Path.home())
    try:
        if args["action"] == "begin":
            receipt, skills_dir = args["receipt"], args["skills_dir"]
            if not isinstance(receipt, Path) or not isinstance(skills_dir, Path):
                parser.error("begin requires --receipt and --skills-dir (the real skill parent)")
            state = bootstrap_begin(
                runtime,
                receipt,
                skills_dir,
                cast("list[Path]", args["alias"]),
                cast("list[Path]", args["host_home"]),
            )
        elif args["action"] == "recover":
            state = _bootstrap_recover(runtime)
        else:
            state = runtime.read()
    except (MaintenanceError, OSError, ValueError, TypeError) as error:
        sys.stderr.write(f"llm-bus maintenance: {error}\n")
        return 1
    sys.stdout.write(json.dumps({key: state[key] for key in ("operation", "phase")}) + "\n")
    return 0


if __name__ == "__main__":
    # The copied file's location binds its home independently of caller arguments.
    raise SystemExit(launcher_main(Runtime(Path(__file__).resolve().parents[4]), sys.argv[1:]))

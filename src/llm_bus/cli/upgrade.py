"""Accepted-checkout wheel and skill helpers for managed generation installation."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from llm_bus.errors import BusError
from llm_bus.maintenance import sync_directory

VERIFY_PATH = "/usr/bin:/bin"
BOOTSTRAP = "install a wheel once as described in README 'Upgrade', then rerun"
RMTREE_UNSAFE = "Refusing to prune: platform rmtree follows symlinks"
STDERR_TAIL_LINES = 20
HEAD_REF = "HEAD"
SKILL_NAME = "llm-bus"
SKILL_SOURCE = f"skills/{SKILL_NAME}"
PACKAGES_DIR = ".local/share/llm-bus/packages"


@dataclass(frozen=True, slots=True)
class InstalledRequirement:
    """Previous uv tool install, restorable from its wheel."""

    path: Path
    extras: tuple[str, ...]
    python: str | None


def run(argv: list[str], *, env: dict[str, str] | None = None, label: str | None = None) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, resolved executables
        argv, capture_output=True, text=True, check=False, env=env
    )
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-STDERR_TAIL_LINES:])
        name = label or f"{Path(argv[0]).name} {' '.join(argv[1:3])}"
        raise BusError(f"{name} failed: {tail}")
    return result.stdout.strip()


def git(checkout: Path, *args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise BusError("git is required to upgrade llm-bus")
    return run([executable, "-C", str(checkout), *args], label=f"git {args[0]}")


def checkout_root(start: Path) -> Path:
    root = Path(git(start, "rev-parse", "--show-toplevel"))
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text("utf-8")).get("project")
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BusError(f"{root} is not an llm-bus checkout") from error
    if not isinstance(project, dict) or cast("dict[str, object]", project).get("name") != "llm-bus":
        raise BusError(f"{root} is not an llm-bus checkout")
    return root


def require_clean(checkout: Path) -> None:
    if git(checkout, "status", "--porcelain", "--untracked-files=no"):
        raise BusError("Tracked files have uncommitted changes; commit or stash them first")


def receipt_path(tools_dir: Path) -> Path:
    return tools_dir / "llm-bus" / "uv-receipt.toml"


def receipt_requirement(receipt: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Return the receipt's [tool] table and its llm-bus requirement."""
    try:
        data = tomllib.loads(receipt.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BusError(f"receipt {receipt} unreadable: {error}") from error
    tool = data.get("tool")
    requirements = (
        cast("dict[str, object]", tool).get("requirements") if isinstance(tool, dict) else None
    )
    if isinstance(requirements, list):
        for item in cast("list[object]", requirements):
            if isinstance(item, dict) and cast("dict[str, object]", item).get("name") == "llm-bus":
                return cast("dict[str, object]", tool), cast("dict[str, object]", item)
    raise BusError(f"receipt {receipt} has no llm-bus requirement")


def requirement_extras(requirement: dict[str, object]) -> tuple[str, ...]:
    extras = requirement.get("extras", [])
    if not isinstance(extras, list):
        raise BusError("receipt extras are malformed")
    return tuple(str(extra) for extra in cast("list[object]", extras))


def read_previous(receipt: Path) -> InstalledRequirement:
    def cannot_restore(reason: str) -> BusError:
        return BusError(
            f"Previous llm-bus install cannot be restored if the upgrade fails ({reason}); "
            f"{BOOTSTRAP}"
        )

    try:
        tool, requirement = receipt_requirement(receipt)
        extras = requirement_extras(requirement)
    except BusError as error:
        raise cannot_restore(str(error)) from error
    raw_path = requirement.get("path")
    if not isinstance(raw_path, str):
        raise cannot_restore("not a wheel install")
    path = Path(raw_path)
    if path.suffix != ".whl" or not path.is_file():
        raise cannot_restore(f"{path} is not an existing wheel")
    python = tool.get("python")
    return InstalledRequirement(path, extras, python if isinstance(python, str) else None)


def build_wheel(
    uv: str, checkout: Path, packages: Path, target: Path, *, revision: str = HEAD_REF
) -> Path:
    for folder in (packages, target):
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            raise BusError(f"Refusing to build into {folder}: not a real directory")
        folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=packages, prefix=".upgrade-") as scratch:
        work = Path(scratch)
        archive = work / "src.tar"
        git(checkout, "archive", "--format=tar", "-o", str(archive), revision)
        source = work / "src"
        with tarfile.open(archive) as tar:
            tar.extractall(source, filter="data")
        dist = work / "dist"
        run([uv, "build", "--wheel", "--out-dir", str(dist), str(source)])
        wheels = list(dist.glob("*.whl"))
        if len(wheels) != 1:
            raise BusError(f"Expected exactly one wheel in {dist}, found {len(wheels)}")
        wheel = target / wheels[0].name
        wheels[0].replace(wheel)
    return wheel


def requirement_arg(wheel: Path, extras: tuple[str, ...]) -> str:
    return f"{wheel}[{','.join(extras)}]" if extras else str(wheel)


def receipt_entrypoint(tool: dict[str, object]) -> Path:
    entrypoints = tool.get("entrypoints")
    if isinstance(entrypoints, list):
        for item in cast("list[object]", entrypoints):
            if isinstance(item, dict) and cast("dict[str, object]", item).get("name") == "llm-bus":
                binary = Path(str(cast("dict[str, object]", item).get("install-path")))
                if binary.exists():
                    return binary
    raise BusError("receipt has no existing llm-bus entrypoint")


def require_safe_placement(checkout: Path, roots: tuple[Path, ...]) -> None:
    for root in roots:
        if checkout.resolve().is_relative_to(root.resolve()):
            raise BusError(
                f"Refusing to upgrade from {checkout}: it is inside {root}, "
                "which managed installation owns"
            )
    if not shutil.rmtree.avoids_symlink_attacks:
        raise BusError(RMTREE_UNSAFE)


def skill_destination(skills_dir: Path) -> Path:
    if not skills_dir.is_dir():
        raise BusError(f"Skills directory {skills_dir} does not exist")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise BusError("Refusing to replace the skill: platform rmtree follows symlinks")
    destination = skills_dir / SKILL_NAME
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise BusError(
            f"Refusing to replace {destination}: not a real directory; "
            "pass the skills directory that holds the real copy"
        )
    return destination


def _extract_skill(checkout: Path, work: Path, revision: str) -> Path:
    archive = work / "skill.tar"
    git(checkout, "archive", "--format=tar", "-o", str(archive), revision, SKILL_SOURCE)
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(work / "src", filter="data")
    except (OSError, tarfile.TarError) as error:
        raise BusError(f"Extracting {SKILL_SOURCE} from HEAD failed: {error}") from error
    fresh = work / "src" / SKILL_SOURCE
    if not (fresh / "SKILL.md").is_file():
        raise BusError(f"HEAD has no {SKILL_SOURCE}/SKILL.md")
    return fresh


def swap_skill(fresh: Path, destination: Path, previous: Path) -> bool:
    """Swap fresh in; on failure restore the old copy, or leave it at previous."""
    sync_skill_tree(fresh)
    replaced = destination.exists()
    try:
        if replaced:
            destination.rename(previous)
            sync_directory(previous.parent)
            sync_directory(destination.parent)
        fresh.rename(destination)
        sync_directory(fresh.parent)
        sync_directory(destination)
        sync_directory(destination.parent)
    except OSError as error:
        failure = f"Installing the skill into {destination} failed: {error}"
        if replaced and previous.exists():
            try:
                previous.rename(destination)
                sync_directory(previous.parent)
                sync_directory(destination.parent)
            except OSError:
                raise BusError(f"{failure}; the previous copy is kept in {previous}") from error
        raise BusError(failure) from error
    return replaced


def sync_skill(checkout: Path, skills_dir: Path, *, revision: str = HEAD_REF) -> dict[str, object]:
    """Replace skills_dir/llm-bus with the skill from HEAD; never write through a link."""
    destination = skill_destination(skills_dir)
    try:
        work = Path(tempfile.mkdtemp(dir=skills_dir, prefix=".llm-bus-upgrade-"))
    except OSError as error:
        raise BusError(f"Cannot create a scratch folder in {skills_dir}: {error}") from error
    previous = work / "previous"
    try:
        fresh = _extract_skill(checkout, work, revision)
        manifest = skill_identity(fresh)
        replaced = swap_skill(fresh, destination, previous)
    except (BusError, OSError):
        if not previous.exists():
            shutil.rmtree(work, ignore_errors=True)
        raise
    shutil.rmtree(work, ignore_errors=True)
    return {"path": str(destination), "replaced": replaced, "sha256": manifest}


def skill_identity(path: Path) -> dict[str, str]:
    """Hash a real skill tree, rejecting links and missing instruction files."""
    if path.is_symlink() or not path.is_dir():
        raise BusError("Real skill copy is missing or symlinked")
    result: dict[str, str] = {}
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise BusError("Skill contains a symlink")
        if item.is_file():
            result[str(item.relative_to(path))] = hashlib.sha256(item.read_bytes()).hexdigest()
    if "SKILL.md" not in result:
        raise BusError("Real skill copy has no SKILL.md")
    return result


def sync_skill_tree(path: Path) -> dict[str, str]:
    """Verify and sync accepted files and directory entries before publication."""
    manifest = skill_identity(path)
    for name in manifest:
        with (path / name).open("rb") as stream:
            os.fsync(stream.fileno())
    for directory in sorted(
        (item for item in path.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        sync_directory(directory)
    sync_directory(path)
    sync_directory(path.parent)
    return manifest

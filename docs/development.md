# Development and contributing

Use Python 3.12 and uv. Application modules live in `src/llm_bus/`; tests live
in `tests/`. Read [AGENTS.md](../AGENTS.md) for repository rules.

## Set up and check a checkout

```sh
sh tools/quality/setup.sh
sh tools/quality/check.sh
```

Run these from the repository root. CI pins uv 0.13.1. To use that version
without replacing your installed uv, prefix uv commands with
`uv tool run --from uv==0.13.1 uv`.

`uv run poe check` runs Ruff formatting and read-only lint, strict BasedPyright,
Pylint, deptry, pytest, and the package check. Formatting, lint, types, and
Pylint cover application code, tests, the Herdr viewer, and maintained Python
tools. Pylint allows fixture literals and exact empty-list assertions in tests;
Ruff owns test formatting. Viewer contract and platform API exceptions stay
local to their declarations.

Use `uv run --frozen poe format` to format or `uv run --frozen poe fix` for
explicit lint fixes. Checks are read-only. Setup installs Git hooks while
preserving existing hook owners: pre-commit checks a staged snapshot's formatting;
pre-push requires a clean checkout and checks the pushed HEAD with `poe check-static`.
That static check also validates uv/Python versions, repository hygiene, and
shell scripts. CI runs the full check, including tests and package isolation.

The scripts adapt the [standard Python profile](https://github.com/wnz99/repo-starters/tree/9c5f517d2ef98af476a4234865376a10ac50e79b/profiles/python).
This repository's Python version, package layout, `pyproject.toml` quality policy,
and isolated package consumer remain authoritative.

## Check the built package

Run `uv run --frozen poe package-check` for the package check alone. It builds
an sdist and a wheel from that sdist, installs the wheel into a fresh temporary
environment outside the checkout, imports every application module under
Python's isolated mode, and checks the installed `llm-bus --help` and
`python -I -m llm_bus --help` entry points.

Core modules are imported before installing the optional collaboration extra;
its runtime modules are imported afterwards. Core has no runtime dependencies,
so installation uses `--no-deps` to catch reliance on development packages.
The temporary environment is removed afterwards. The check installs no host
hooks and does not access the user's mailbox.

## Branches and pull requests

`main` is the only permanent branch. Create feature or fix branches from it and
open pull requests into it. Run `uv run poe check` before pushing a PR. Merge
with a merge commit and delete temporary source branches after merging.

The `Repository / quality` and `Relay / quality` checks run on Linux for PRs
into `main`. Check both before merging: their results are advisory, and GitHub
allows merges with failed or pending results. Merging does not deploy anything.

## Text and local configuration

`.editorconfig` and `.gitattributes` define shared text defaults. Local `.env`
files are ignored. Secret-free `.env.example` and `.env.*.example` templates may
be tracked; never put credentials in them.

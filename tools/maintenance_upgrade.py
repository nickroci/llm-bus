"""Owner-invoked Python 3.12 bootstrap; never import the legacy installed package."""

import runpy
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

runtime = runpy.run_path(str(Path(__file__).resolve().parents[1] / "src/llm_bus/maintenance.py"))
raise SystemExit(cast("Callable[[], int]", runtime["bootstrap_main"])())

"""Native agent hosts whose sessions register on the bus."""

from __future__ import annotations

HOST_KINDS = ("claude", "codex", "gemini", "agy")

# Regular-expression alternation of the kinds, used in address patterns.
KIND_PATTERN = "|".join(HOST_KINDS)

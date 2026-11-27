"""Native agent hosts whose sessions register on the bus."""

from __future__ import annotations

HOST_KINDS = ("claude", "codex", "gemini", "agy")

# Hosts whose own hook reports a wake endpoint, stored as the session's `messaging_socket`:
# Claude's native inbox socket path, or the Herdr pane ID of a Gemini or Antigravity session.
ENDPOINT_KINDS = ("claude", "gemini", "agy")

# Hosts woken by submitting a prompt to their Herdr pane; Herdr reports the same agent names.
HERDR_KINDS = ("gemini", "agy")

# Regular-expression alternation of the kinds, used in address patterns.
KIND_PATTERN = "|".join(HOST_KINDS)

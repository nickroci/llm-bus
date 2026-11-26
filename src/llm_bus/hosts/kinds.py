"""Native agent hosts whose sessions register on the bus."""

from __future__ import annotations

HOST_KINDS = ("claude", "codex", "gemini")

# Hosts whose own hook reports a wake endpoint, stored as the session's `messaging_socket`:
# Claude's native inbox socket path, or the Herdr pane ID of a Gemini session.
ENDPOINT_KINDS = ("claude", "gemini")

# Regular-expression alternation of the kinds, used in address patterns.
KIND_PATTERN = "|".join(HOST_KINDS)

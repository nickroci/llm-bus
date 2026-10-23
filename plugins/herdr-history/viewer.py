"""Read-only Herdr terminal viewer for the llm-bus history JSON contract."""

from __future__ import annotations

import curses
import json
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import cast, final

PAGE_SIZE = 50
MIN_HEIGHT = 4
MIN_WIDTH = 20


@dataclass(frozen=True)
class Message:  # pylint: disable=too-many-instance-attributes
    # Fields mirror the history JSON contract, including nullable legacy wake metadata.
    id: int
    sender: str
    recipient: str
    body: str
    sender_cwd: str | None
    recipient_cwd: str | None
    sent_at: str
    acknowledged_at: str | None
    wake_status: str | None
    wake_via: str | None
    wake_reason: str | None
    wake_checked_at: str | None
    reply_to: int | None = None


def clean(value: str) -> str:
    """Keep terminal control and Unicode formatting characters out of curses."""
    return "".join(char if char.isprintable() else "?" for char in value)


def folder_from_context(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        context = cast("object", json.loads(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(context, dict):
        return None
    fields = cast("dict[str, object]", context)
    for key in ("workspace_cwd", "focused_pane_cwd"):
        value = fields.get(key)
        if isinstance(value, str) and value and Path(value).is_absolute():
            return os.path.normpath(value)
    return None


def parse_messages(raw: str) -> list[Message]:
    # pylint: disable=too-many-locals
    # Validate the full record before constructing a typed message.
    try:
        records = cast("object", json.loads(raw))
    except json.JSONDecodeError as exc:
        raise ValueError("llm-bus returned invalid JSON") from exc
    if not isinstance(records, list):
        raise ValueError("llm-bus history must return a JSON array")  # noqa: TRY004
    messages: list[Message] = []
    for item in cast("list[object]", records):
        if not isinstance(item, dict):
            raise ValueError("llm-bus history contains a non-object message")  # noqa: TRY004
        record = cast("dict[str, object]", item)
        message_id = record.get("id")
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            raise ValueError("llm-bus history message has an invalid ID")
        reply_to = record.get("reply_to")
        if reply_to is not None and (
            isinstance(reply_to, bool) or not isinstance(reply_to, int) or reply_to <= 0
        ):
            raise ValueError("llm-bus history message has an invalid reply ID")
        sender, recipient, body, sent_at = (
            record.get("sender"),
            record.get("recipient"),
            record.get("body"),
            record.get("sent_at"),
        )
        if not all(isinstance(value, str) for value in (sender, recipient, body, sent_at)):
            raise ValueError("llm-bus history message has an invalid text field")
        (
            sender_cwd,
            recipient_cwd,
            acknowledged_at,
            wake_status,
            wake_via,
            wake_reason,
            wake_checked_at,
        ) = (
            record.get("sender_cwd"),
            record.get("recipient_cwd"),
            record.get("acknowledged_at"),
            record.get("wake_status"),
            record.get("wake_via"),
            record.get("wake_reason"),
            record.get("wake_checked_at"),
        )
        if any(
            value is not None and not isinstance(value, str)
            for value in (
                sender_cwd,
                recipient_cwd,
                acknowledged_at,
                wake_status,
                wake_via,
                wake_reason,
                wake_checked_at,
            )
        ):
            raise ValueError("llm-bus history message has an invalid optional field")
        messages.append(
            Message(
                message_id,
                cast("str", sender),
                cast("str", recipient),
                cast("str", body),
                cast("str | None", sender_cwd),
                cast("str | None", recipient_cwd),
                cast("str", sent_at),
                cast("str | None", acknowledged_at),
                cast("str | None", wake_status),
                cast("str | None", wake_via),
                cast("str | None", wake_reason),
                cast("str | None", wake_checked_at),
                reply_to,
            )
        )
    return messages


def fetch_history(binary: str, folder: str | None, before: int | None = None) -> list[Message]:
    command = (
        [binary, "history", "--repository", folder] if folder else [binary, "history", "--all"]
    )
    command.extend(("--limit", str(PAGE_SIZE)))
    if before is not None:
        command.extend(("--before", str(before)))
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Cannot run llm-bus history: {exc}") from exc
    if result.returncode:
        detail = (
            clean(result.stderr.strip().splitlines()[-1]) if result.stderr.strip() else "no details"
        )
        # pylint: disable-next=clean-code-business-policy-literal
        raise ValueError(f"llm-bus history failed ({result.returncode}): {detail}")
    return parse_messages(result.stdout)


def wrap(value: str, width: int) -> list[str]:
    lines: list[str] = []
    for line in value.split("\n"):
        lines.extend(
            textwrap.wrap(
                clean(line), width=max(1, width), replace_whitespace=False, drop_whitespace=False
            )
            or [""]
        )
    return lines


def message_lines(messages: list[Message], width: int) -> list[str]:
    if not messages:
        return ["No messages in this scope."]
    lines: list[str] = []
    for message in messages:
        status = "pending" if message.acknowledged_at is None else f"ack {message.acknowledged_at}"
        lines.extend(wrap(f"#{message.id}  {message.sent_at}  [{status}]", width))
        lines.extend(wrap(f"{message.sender} to {message.recipient}", width))
        sender_folder = message.sender_cwd or "unknown"
        recipient_folder = message.recipient_cwd or "unknown"
        lines.extend(wrap(f"folders: {sender_folder} to {recipient_folder}", width))
        if message.wake_status is None:
            lines.extend(wrap("wake: unrecorded", width))
        else:
            wake = f"wake: {message.wake_status}"
            if message.wake_via:
                wake += f" via {message.wake_via}"
            if message.wake_checked_at:
                wake += f" at {message.wake_checked_at}"
            if message.wake_reason:
                wake += f" ({message.wake_reason})"
            lines.extend(wrap(wake, width))
        for line in message.body.split("\n"):
            lines.extend(wrap(f"  {line}", width))
        lines.append("")
    return lines


@final
class Viewer:
    def __init__(self, binary: str, folder: str | None) -> None:
        self.binary = binary
        self.folder = folder
        self.scope = "folder" if folder else "global"
        self.pages: list[list[Message]] = []
        self.page = 0
        self.offset = 0
        self.error: str | None = None

    def load(self) -> None:
        if not self.binary:
            self.error = "llm-bus executable not found in PATH. Install llm-bus, then press r."
            return
        try:
            messages = fetch_history(self.binary, self.folder if self.scope == "folder" else None)
        except ValueError as exc:
            self.error = str(exc)
            return
        self.pages = [messages]
        self.page = 0
        self.offset = 0
        self.error = None

    def older(self) -> None:
        if self.error or not self.pages:
            return
        if self.page + 1 < len(self.pages):
            self.page += 1
        elif len(self.pages[-1]) == PAGE_SIZE:
            try:
                older = fetch_history(
                    self.binary,
                    self.folder if self.scope == "folder" else None,
                    before=min(message.id for message in self.pages[-1]),
                )
            except ValueError as exc:
                self.error = str(exc)
                return
            if older:
                self.pages.append(older)
                self.page += 1
        self.offset = 0

    def newer(self) -> None:
        if self.page:
            self.page -= 1
            self.offset = 0

    def switch(self, scope: str) -> None:
        if scope == "folder" and self.folder is None:
            self.error = "No workspace folder in Herdr context. Global view available with g."
            return
        self.scope = scope
        self.load()

    def draw(self, screen: curses.window) -> int:
        screen.erase()
        height, width = screen.getmaxyx()
        if height < MIN_HEIGHT or width < MIN_WIDTH:
            screen.addnstr(0, 0, "Enlarge pane", max(1, width - 1))
            screen.refresh()
            return 0
        scope = f"REPOSITORY {self.folder}" if self.scope == "folder" else "ALL FOLDERS"
        screen.addnstr(0, 0, clean(f"llm-bus history | {scope}"), width - 1, curses.A_REVERSE)
        count = len(self.pages[self.page]) if self.pages else 0
        screen.addnstr(1, 0, f"Page {self.page + 1} | {count} messages | newest first", width - 1)
        lines = (
            wrap(self.error, width - 1)
            if self.error
            else message_lines(self.pages[self.page] if self.pages else [], width - 1)
        )
        visible = height - 3
        self.offset = min(self.offset, max(0, len(lines) - visible))
        for row, line in enumerate(lines[self.offset : self.offset + visible], start=2):
            screen.addnstr(row, 0, line, width - 1)
        screen.addnstr(
            height - 1,
            0,
            "j/k scroll  n older  p newer  r refresh  f repository  g all  q quit",
            width - 1,
            curses.A_REVERSE,
        )
        screen.refresh()
        return max(0, len(lines) - visible)

    def handle_key(self, key: int, max_offset: int, height: int) -> bool:  # noqa: C901
        if key in (ord("q"), 27):  # pylint: disable=clean-code-business-policy-literal
            # Escape is also a quit key.
            return False
        if key in (ord("j"), curses.KEY_DOWN):
            self.offset = min(max_offset, self.offset + 1)
        elif key in (ord("k"), curses.KEY_UP):
            self.offset = max(0, self.offset - 1)
        elif key == curses.KEY_NPAGE:
            self.offset = min(max_offset, self.offset + max(1, height - 4))
        elif key == curses.KEY_PPAGE:
            self.offset = max(0, self.offset - max(1, height - 4))
        elif key == ord("n"):
            self.older()
        elif key == ord("p"):
            self.newer()
        elif key == ord("r"):
            self.load()
        elif key == ord("f"):
            self.switch("folder")
        elif key == ord("g"):
            self.switch("global")
        return True

    def run(self, screen: curses.window) -> None:
        curses.curs_set(0)
        # pylint: disable-next=clean-code-boolean-flag-argument
        screen.keypad(True)  # noqa: FBT003 - curses API
        self.load()
        while True:
            max_offset = self.draw(screen)
            if not self.handle_key(screen.getch(), max_offset, screen.getmaxyx()[0]):
                return


def main() -> int:
    binary = shutil.which("llm-bus") or ""
    # Herdr owns this environment-variable name.
    # pylint: disable-next=clean-code-business-policy-literal
    folder = folder_from_context(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON"))
    try:
        curses.wrapper(Viewer(binary, folder).run)
    except curses.error as exc:
        sys.stderr.write(f"Cannot open terminal viewer: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

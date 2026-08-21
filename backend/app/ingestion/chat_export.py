"""M9 - parsing a chat export, with direct messages excluded by construction.

The capability test requires that "the two direct-message records in the export
are excluded from processing entirely". Excluded by construction rather than by
a filter that could be forgotten:

  the parser drops them and never returns them
  `chat_messages` carries CHECK (is_direct_message = 0), so one cannot be
  stored even by a direct INSERT

The count of what was dropped is recorded on the ingestion report, because the
messages themselves leave no trace and "zero DM records in the store" is
otherwise indistinguishable from "the export had no DMs".

A DM is identified two ways: the export's own flag, and a channel name that
looks like a direct thread. Either is enough. Trusting only the flag would mean
a export that omits it silently leaks private conversation into the store,
which is the one mistake here that cannot be walked back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.models.common import StrictModel
from app.models.ingestion import Defect, DefectCode, DefectSeverity

#: Channel names that denote a direct thread whatever the flag says.
_DM_CHANNEL = re.compile(r"^(dm|dms|direct|private)[-_]", re.IGNORECASE)

_ID_KEYS = ("id", "message_id", "msg_id", "ts_id")
_TEXT_KEYS = ("text", "message", "body", "content")
_AUTHOR_KEYS = ("author", "user", "username", "from", "sender")
_CHANNEL_KEYS = ("channel", "channel_name", "conversation", "room")
_TIME_KEYS = ("timestamp", "ts", "time", "sent_at", "created_at")
_THREAD_KEYS = ("thread_id", "thread", "parent_id", "thread_ts")


class ChatMessage(StrictModel):
    """One message from a project channel. A DM never becomes one of these."""

    id: str
    channel: str
    author: str
    ts: str
    text: str
    thread_id: str | None = None


class ChatParseResult(StrictModel):
    messages: list[ChatMessage] = []
    direct_messages_excluded: int = 0
    channels: list[str] = []
    dm_channels: list[str] = []
    defects: list[Defect] = []

    @property
    def ok(self) -> bool:
        return bool(self.messages) and not any(d.blocking for d in self.defects)


def _first(entry: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = entry.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def is_direct_message(entry: dict) -> bool:
    """Either signal is enough.

    The export's flag is authoritative when present. A channel named like a
    direct thread is treated as one regardless, because an export that omits
    the flag would otherwise leak a private conversation into the store, and
    that is not a mistake that can be undone afterwards.
    """
    if bool(entry.get("is_direct_message")) or bool(entry.get("is_dm")):
        return True
    channel = _first(entry, _CHANNEL_KEYS) or ""
    return bool(_DM_CHANNEL.match(channel))


def parse_chat_export(text: str) -> ChatParseResult:
    """Parse an export into project-channel messages only."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return ChatParseResult(
            defects=[
                Defect(
                    code=DefectCode.MALFORMED_STRUCTURE,
                    severity=DefectSeverity.ERROR,
                    detail=f"invalid JSON: {exc.msg}",
                    line_number=exc.lineno,
                )
            ]
        )

    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict):
        entries = next(
            (document[k] for k in ("messages", "items", "records") if isinstance(document.get(k), list)),
            None,
        )
        if entries is None:
            return ChatParseResult(
                defects=[
                    Defect(
                        code=DefectCode.MALFORMED_STRUCTURE,
                        severity=DefectSeverity.ERROR,
                        detail="no list of messages found under messages, items or records",
                    )
                ]
            )
    else:
        return ChatParseResult(
            defects=[
                Defect(
                    code=DefectCode.MALFORMED_STRUCTURE,
                    severity=DefectSeverity.ERROR,
                    detail=f"top level is {type(document).__name__}, expected an object or a list",
                )
            ]
        )

    messages: list[ChatMessage] = []
    defects: list[Defect] = []
    excluded = 0
    channels: set[str] = set()
    dm_channels: set[str] = set()

    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            defects.append(
                Defect(
                    code=DefectCode.MALFORMED_STRUCTURE,
                    severity=DefectSeverity.WARNING,
                    detail=f"entry {position} is {type(entry).__name__}, expected an object",
                    line_number=position,
                )
            )
            continue

        channel = _first(entry, _CHANNEL_KEYS) or "unknown"

        if is_direct_message(entry):
            excluded += 1
            dm_channels.add(channel)
            continue

        body = _first(entry, _TEXT_KEYS)
        identifier = _first(entry, _ID_KEYS)
        if not body or not identifier:
            defects.append(
                Defect(
                    code=DefectCode.EMPTY_SEGMENT_TEXT,
                    severity=DefectSeverity.WARNING,
                    detail=f"entry {position} has no {'id' if body else 'text'}",
                    line_number=position,
                )
            )
            continue

        channels.add(channel)
        messages.append(
            ChatMessage(
                id=identifier,
                channel=channel,
                author=_first(entry, _AUTHOR_KEYS) or "unknown",
                ts=_first(entry, _TIME_KEYS) or "",
                text=" ".join(body.split()),
                thread_id=_first(entry, _THREAD_KEYS),
            )
        )

    if not messages and not any(d.blocking for d in defects):
        defects.append(
            Defect(
                code=DefectCode.NO_PARSEABLE_SEGMENTS,
                severity=DefectSeverity.ERROR,
                detail=f"{len(entries)} entries found, none usable as a project-channel message",
            )
        )

    return ChatParseResult(
        messages=messages,
        direct_messages_excluded=excluded,
        channels=sorted(channels),
        dm_channels=sorted(dm_channels),
        defects=defects,
    )


def read_chat_export(path: Path) -> ChatParseResult:
    from app.ingestion.reader import read_source_text

    read = read_source_text(path)
    if any(d.blocking for d in read.defects):
        return ChatParseResult(defects=read.defects)

    result = parse_chat_export(read.text)
    return result.model_copy(update={"defects": read.defects + result.defects})

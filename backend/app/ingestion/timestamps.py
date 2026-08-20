"""Timestamp parsing shared by every transcript format.

Timestamps must survive parsing, chunking and citation, so there is one
canonical form: `HH:MM:SS`, kept as a string exactly as it will be cited, with
a float seconds value alongside for ordering and audio seeking.
"""

from __future__ import annotations

import re

# 00:00:05  |  00:00:05.250  |  00:00:05,250  |  2:15  |  1:02:03.5
_CLOCK = re.compile(
    r"^\s*(?:(?P<h>\d{1,2}):)?(?P<m>\d{1,2}):(?P<s>\d{1,2})(?:[.,](?P<ms>\d{1,3}))?\s*$"
)


def parse_timestamp(value: str | float | int | None) -> float | None:
    """Return seconds, or None when the value is absent or unparseable.

    A bare number is treated as seconds, which is what most JSON transcript
    exports and speech-to-text libraries emit.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None

    text = str(value).strip()
    if not text:
        return None

    match = _CLOCK.match(text)
    if match is None:
        try:
            seconds = float(text)
        except ValueError:
            return None
        return seconds if seconds >= 0 else None

    hours = int(match.group("h") or 0)
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    millis = int((match.group("ms") or "0").ljust(3, "0"))

    if minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def format_timestamp(seconds: float | None) -> str | None:
    """Canonical `HH:MM:SS`. Sub-second precision is dropped: citations are
    read by humans and a transcript line is never that precise anyway."""
    if seconds is None or seconds < 0:
        return None
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def normalise_timestamp(value: str | float | int | None) -> tuple[str | None, float | None]:
    """Return the canonical string form and the seconds form together."""
    seconds = parse_timestamp(value)
    return format_timestamp(seconds), seconds

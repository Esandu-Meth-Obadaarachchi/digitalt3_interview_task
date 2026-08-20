"""Deciding whether a "Something: text" prefix is a speaker label.

Naively splitting on the first colon turns "Note: this is important" into a
speaker called Note. That would invent a speaker, which the brief's first
behavioural rule forbids outright.

Two signals, in order of confidence:

  1. the candidate matches a participant named in the source metadata
  2. the candidate looks like a name: one to four tokens, each capitalised,
     no sentence punctuation, short enough to be a name

When neither holds, the whole line is treated as unlabelled speech and the
speaker is left None. Leaving it None is always safe. Guessing is not.
"""

from __future__ import annotations

import re

_NAME_SHAPED = re.compile(r"^[A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){0,3}$")
_MAX_LABEL_LENGTH = 48


def _canonical(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def build_speaker_lookup(participants: list[str] | None) -> dict[str, str]:
    """Map canonical forms of known participants back to their stated spelling.

    Both the full name and the first name are registered, because transcripts
    address people by first name. A first name shared by two participants is
    deliberately left out: "Priya" is ambiguous when the room contains Priya
    Sharma and Priya Menon, and resolving it would be a guess.
    """
    lookup: dict[str, str] = {}
    if not participants:
        return lookup

    for participant in participants:
        lookup[_canonical(participant)] = participant

    first_names: dict[str, list[str]] = {}
    for participant in participants:
        first = participant.split()[0] if participant.split() else participant
        first_names.setdefault(_canonical(first), []).append(participant)

    for canonical_first, owners in first_names.items():
        if len(owners) == 1 and canonical_first not in lookup:
            lookup[canonical_first] = owners[0]

    return lookup


def split_speaker(line: str, speaker_lookup: dict[str, str] | None = None) -> tuple[str | None, str]:
    """Split "Speaker: text" into (speaker, text).

    Returns (None, line) when the prefix is not convincingly a speaker label.
    """
    if ":" not in line:
        return None, line.strip()

    candidate, _, rest = line.partition(":")
    candidate = candidate.strip()
    rest = rest.strip()

    if not candidate or not rest or len(candidate) > _MAX_LABEL_LENGTH:
        return None, line.strip()

    if speaker_lookup:
        known = speaker_lookup.get(_canonical(candidate))
        if known is not None:
            return known, rest

    if _NAME_SHAPED.match(candidate) and not candidate.endswith((".", "!", "?")):
        return candidate, rest

    return None, line.strip()

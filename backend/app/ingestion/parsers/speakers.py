"""Deciding whether a "Something: text" prefix is a speaker label.

Naively splitting on the first colon turns "Note: this is important" into a
speaker called Note. That would invent a speaker, which the brief's first
behavioural rule forbids outright.

Two signals, in order of confidence:

  1. the candidate matches a participant named in the source metadata
  2. the candidate is name-shaped: two to four capitalised tokens, no sentence
     punctuation, short enough to be a name, and not a document-structure word

A single capitalised word is accepted only on signal 1. On its own it is too
weak: "Note: the deadline moved" would otherwise produce a speaker called Note.
When metadata names the participants there is ground truth to check against,
and when it does not, leaving the line unattributed is the safe failure.

When neither signal holds, the whole line is treated as unlabelled speech and
the speaker is left None. Leaving it None is always safe. Guessing is not.
"""

from __future__ import annotations

import re

#: Two to four capitalised tokens. One token alone is not enough (see above).
_NAME_SHAPED = re.compile(r"^[A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){1,3}$")
_MAX_LABEL_LENGTH = 48

#: Capitalised phrases that structure a document rather than name a person.
#: "Action Items: ..." is a heading, not somebody speaking.
_NOT_A_NAME = {
    "action", "actions", "agenda", "answer", "attendees", "background", "blocker",
    "blockers", "conclusion", "context", "decision", "decisions", "example", "follow",
    "issue", "issues", "item", "items", "next", "nb", "note", "notes", "outcome",
    "present", "question", "questions", "recap", "reminder", "risk", "risks",
    "status", "summary", "todo", "topic", "update", "updates", "warning",
}


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

    first_token = candidate.split()[0].lower().strip(".,'’-")
    if (
        _NAME_SHAPED.match(candidate)
        and first_token not in _NOT_A_NAME
        and not candidate.endswith((".", "!", "?"))
    ):
        return candidate, rest

    return None, line.strip()

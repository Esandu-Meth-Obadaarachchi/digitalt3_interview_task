"""M13 - deciding which commitments belong to the same person.

The owner of an action is free text lifted from a transcript. One person is
"Priya" in one line, "Priya Sharma" in another and "priya" in a third. Grouping
on the raw string produces three digests for one person, which is not a
per-person view of anything.

So names are grouped on their first name, casefolded. Two consequences, and
both are deliberate:

  * "Priya" and "Priya Sharma" land together, which is the point.
  * "Priya Sharma" and "Priya Menon" also land together, and the sprint
    planning transcript contains exactly that pair on purpose.

The second is a real cost and it is not hidden. Every line carries the owner
string exactly as the transcript gave it, and a digest covering more than one
full name says so in its own text. The alternative, splitting on the full name,
is worse in the common case: the transcript usually says "Priya", so the split
produces a digest for a first name and another for a full name and the person
reads neither in full. Grouping and showing the evidence beats splitting and
showing neither.

`PERSON_IDENTITY=full_name` switches to the strict rule for anyone who wants
it, and the tests cover both.

Not attempted: nicknames (Bob for Robert), initials, or matching by email.
Each needs a source of truth this build does not have.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.models.common import UNSPECIFIED

#: The key used for everything nobody owns. Never a person.
UNASSIGNED = "unassigned"

UNASSIGNED_TITLE = "Assignee unspecified"

#: Titles stripped before a first name is taken, so "Dr Priya" groups with
#: "Priya" rather than forming a person called Dr.
_TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam"}

_PUNCTUATION = re.compile(r"[^\w\s'-]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise(name: str | None) -> str:
    """A name with the noise taken off, casing left alone."""
    if not name:
        return ""
    cleaned = unicodedata.normalize("NFKC", str(name))
    cleaned = _PUNCTUATION.sub(" ", cleaned)
    return _WHITESPACE.sub(" ", cleaned).strip()


def tokens(name: str | None) -> list[str]:
    """The parts of a name, titles dropped."""
    parts = [p for p in normalise(name).split(" ") if p]
    kept = [p for p in parts if p.casefold().strip(".") not in _TITLES]
    return kept or parts


def is_unassigned(name: str | None) -> bool:
    """UNSPECIFIED, blank, or a placeholder standing in for a person."""
    cleaned = normalise(name)
    if not cleaned:
        return True
    flat = cleaned.casefold().replace(" ", "_")
    return flat in {UNSPECIFIED.casefold(), "unassigned", "unknown", "tbd", "n_a", "none"}


def person_key(name: str | None, identity: str = "first_name") -> str:
    """The grouping key for one owner string.

    first_name  everyone sharing a first name is one person
    full_name   only an identical full name is the same person
    """
    if is_unassigned(name):
        return UNASSIGNED
    parts = tokens(name)
    if identity == "full_name":
        return "_".join(p.casefold() for p in parts)
    return parts[0].casefold()


@dataclass
class Person:
    """One grouping key, and every owner string that fell into it."""

    key: str
    display_name: str
    aliases: list[str] = field(default_factory=list)
    unassigned: bool = False

    @property
    def ambiguous(self) -> bool:
        """More than one distinct full name collapsed into this person."""
        return len({a for a in self.aliases if len(tokens(a)) > 1}) > 1


def _display(key: str, aliases: list[str], participants: list[str], identity: str) -> str:
    """The name to put at the top of the digest.

    A single full name is used as it was written. Several full names sharing a
    first name means the first name is the only thing they agree on, so that is
    the heading and the digest lists the full names underneath it.
    """
    full = sorted({a for a in aliases if len(tokens(a)) > 1})
    if len(full) == 1:
        return full[0]
    if not full:
        # Only a bare first name was ever used. If the participant list names
        # exactly one person with it, use their full name: that is a lookup in
        # metadata the meeting supplied, not a guess.
        matches = [p for p in participants if person_key(p, identity) == key]
        if len(matches) == 1:
            return normalise(matches[0])
    first = aliases[0] if aliases else key
    return tokens(first)[0] if tokens(first) else key


def group_owners(
    owners: list[str],
    participants: list[str] | None = None,
    identity: str = "first_name",
) -> list[Person]:
    """Group owner strings into people, unowned work into `unassigned`.

    Order is stable: people alphabetically by key, unassigned last, so a digest
    run twice produces the same list.
    """
    known = [normalise(p) for p in (participants or []) if normalise(p)]
    grouped: dict[str, Person] = {}

    for owner in owners:
        key = person_key(owner, identity)
        stated = normalise(owner) or UNSPECIFIED
        person = grouped.get(key)
        if person is None:
            person = Person(
                key=key,
                display_name=UNASSIGNED_TITLE if key == UNASSIGNED else "",
                unassigned=key == UNASSIGNED,
            )
            grouped[key] = person
        if key == UNASSIGNED:
            if UNSPECIFIED not in person.aliases:
                person.aliases.append(UNSPECIFIED)
            continue
        if stated not in person.aliases:
            person.aliases.append(stated)

    for key, person in grouped.items():
        if not person.unassigned:
            person.display_name = _display(key, person.aliases, known, identity)

    people = sorted(
        (p for p in grouped.values() if not p.unassigned), key=lambda p: p.key
    )
    people += [p for p in grouped.values() if p.unassigned]
    return people

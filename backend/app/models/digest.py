"""M10 - the end-of-day digest.

The shape is specified: "3 items that moved, 2 items that need attention, 1
thing to decide". Three sections, fixed sizes, because a digest that grows with
the day is a digest nobody reads. The point of the format is that it forces a
choice about what matters.

Every line carries a citation. That is the other half of the specification,
"with every line citing its source", and it is what separates this from a
summary: a reader who doubts a line can go and check it.
"""

from __future__ import annotations

from pydantic import Field

from app.models.common import UNSPECIFIED, Citation, StrictModel


class DigestLine(StrictModel):
    """One line of a digest, and the thing it came from."""

    text: str
    citation: Citation
    extraction_id: str
    extraction_type: str
    #: Why this line is in this section, in the digest's own words. Kept so a
    #: reader can disagree with the selection rather than only the wording.
    because: str


class Digest(StrictModel):
    """One digest, for one channel, for one day."""

    id: str
    scope_type: str            # channel
    scope_key: str             # a chat channel, or a meeting source id
    scope_title: str
    digest_date: str
    generated_at: str
    trigger: str               # scheduler | manual | clock_override

    moved: list[DigestLine] = Field(default_factory=list, max_length=3)
    attention: list[DigestLine] = Field(default_factory=list, max_length=2)
    to_decide: list[DigestLine] = Field(default_factory=list, max_length=1)

    #: How many approved items existed in scope, so a thin digest is
    #: distinguishable from a quiet day.
    considered: int = 0

    @property
    def lines(self) -> list[DigestLine]:
        return [*self.moved, *self.attention, *self.to_decide]

    @property
    def empty(self) -> bool:
        return not self.lines

    def render(self) -> str:
        """Markdown, because a digest is read by a person.

        Every line ends with its citation inline rather than as a footnote, so
        checking one does not require scrolling.
        """
        out = [f"# {self.scope_title} — {self.digest_date}", ""]

        if self.empty:
            out += [
                "Nothing approved in scope for this day.",
                "",
                f"_{self.considered} approved item(s) exist for this scope in total._",
            ]
            return "\n".join(out)

        for title, lines, blank in (
            ("Moved", self.moved, "Nothing moved."),
            ("Needs attention", self.attention, "Nothing outstanding."),
            ("To decide", self.to_decide, "Nothing waiting on a decision."),
        ):
            out.append(f"## {title}")
            if not lines:
                out += ["", f"_{blank}_", ""]
                continue
            out.append("")
            for line in lines:
                citation = line.citation
                where = f"{citation.source_title or citation.source_id}"
                when = f" at {citation.timestamp}" if citation.timestamp else ""
                who = f"{citation.speaker}" if citation.speaker else "unattributed"
                out.append(f"- {line.text}")
                out.append(f'  > "{citation.quote}"')
                out.append(f"  — {who}, {where}{when} · {line.because}")
            out.append("")

        out.append(f"_Built from {self.considered} approved item(s). "
                   f"Nothing unapproved appears in this digest._")
        return "\n".join(out)


# =============================================================================
# M13 - the per-person digest
# =============================================================================
# "Per-person view of their commitments. Person with no commitments gets no
# digest."
#
# The shape is deliberately not the 3/2/1 of the channel digest. That format
# exists to force a choice about what matters across a whole channel. A person
# opening their own digest wants every commitment standing against their name,
# and a cap would silently drop the fourth one.
#
# Two rules from the specification are structural rather than cosmetic. A
# person with nothing approved never gets a digest at all, which is enforced
# where digests are emitted rather than by rendering an empty file. And an
# action nobody owns still has to be visible, so it goes to a digest of its own
# where every line states the task and says the assignee is unspecified.


class PersonDigestLine(StrictModel):
    """One commitment, as it stands against a person's name."""

    text: str
    citation: Citation
    extraction_id: str
    #: The owner string exactly as the transcript gave it. Kept per line
    #: because grouping happens on the first name: when two people share one,
    #: this is what tells them apart.
    owner_as_stated: str
    #: The date as the transcript stated it, or UNSPECIFIED. Never resolved
    #: into a guess here.
    due_date: str
    approved_on: str | None = None

    @property
    def dated(self) -> bool:
        return bool(self.due_date) and self.due_date != UNSPECIFIED


class PersonDigest(StrictModel):
    """One person's commitments, for one day."""

    id: str
    #: The grouping key. A normalised first name, or 'unassigned'.
    person_key: str
    display_name: str
    #: Every distinct owner string collapsed into this key. More than one means
    #: the digest says so out loud rather than merging two people silently.
    aliases: list[str] = Field(default_factory=list)
    unassigned: bool = False

    digest_date: str
    generated_at: str
    trigger: str

    commitments: list[PersonDigestLine] = Field(default_factory=list)
    #: Approved actions considered for this person, so a thin digest is
    #: distinguishable from a quiet day.
    considered: int = 0

    @property
    def empty(self) -> bool:
        return not self.commitments

    @property
    def ambiguous(self) -> bool:
        """More than one full name collapsed into this person."""
        return len({a for a in self.aliases if a != UNSPECIFIED}) > 1

    def render(self) -> str:
        """Markdown, same as the channel digest, read by one person."""
        heading = "Assignee unspecified" if self.unassigned else self.display_name
        out = [f"# {heading} — {self.digest_date}", ""]

        if self.empty:
            # Never written for a real person: M13 says somebody with no
            # commitments gets no digest, and emit_person_digests skips them.
            # Rendered anyway so a preview of an empty person says why.
            out += ["No approved commitments stand against this name."]
            return "\n".join(out)

        count = len(self.commitments)
        if self.unassigned:
            out += [
                f"{count} approved commitment(s) with nobody named.",
                "",
                "_Every line states the task, and the assignee is unspecified. "
                "Nothing here was assigned by guessing._",
                "",
            ]
        else:
            out += [f"{count} approved commitment(s).", ""]
            if self.ambiguous:
                names = ", ".join(sorted({a for a in self.aliases if a != UNSPECIFIED}))
                out += [
                    f"_Grouped by first name, so this covers {names}. Each line names the "
                    f"owner exactly as the transcript stated it._",
                    "",
                ]

        dated = [line for line in self.commitments if line.dated]
        undated = [line for line in self.commitments if not line.dated]

        for title, lines, blank in (
            ("With a date", dated, "Nothing here has a date."),
            ("No date stated", undated, "Everything here has a date."),
        ):
            if not lines:
                continue
            out += [f"## {title}", ""]
            for line in lines:
                citation = line.citation
                where = citation.source_title or citation.source_id
                when = f" at {citation.timestamp}" if citation.timestamp else ""
                who = citation.speaker or "unattributed"
                assignee = (
                    "assignee UNSPECIFIED, nobody was named"
                    if self.unassigned
                    else f"owner as stated: {line.owner_as_stated}"
                )
                due = (
                    f"due {line.due_date}" if line.dated
                    else "due UNSPECIFIED, no date was stated"
                )
                out.append(f"- {line.text}")
                out.append(f"  {due} · {assignee}")
                out.append(f'  > "{citation.quote}"')
                out.append(f"  — {who}, {where}{when}")
            out.append("")

        out.append(
            f"_Built from {self.considered} approved item(s). "
            f"Nothing unapproved appears in this digest._"
        )
        return "\n".join(out)

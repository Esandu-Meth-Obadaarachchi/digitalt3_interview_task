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

from app.models.common import Citation, StrictModel


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

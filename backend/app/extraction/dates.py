"""Relative date resolution (golden case 4).

The brief: "Assert that no action has a concrete due date unless the transcript
states one or states a resolvable relative date... Where a relative date is
resolved, the resolution rule must be documented."

Two rules govern everything here:

1. **Resolution is anchored to the meeting date, never to today.** Re-running
   the eval harness next month must produce the same answer as running it today.

2. **What cannot be resolved by a stated rule is not resolved.** "Early
   October", "soon", "next sprint" and "after the audit" stay UNSPECIFIED. An
   approximate date presented as a real one is precisely the invented-date
   failure golden case 4 probes for.

Every resolution records which rule produced it, so any date in the store can
be traced back to the words that caused it.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from app.models.common import UNSPECIFIED, DateType
from app.models.extraction import ResolvedDate

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name})

#: Numbers written as words, as they appear when a date is spoken aloud.
ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "twenty-first": 21, "twenty first": 21,
    "twenty-second": 22, "twenty second": 22, "twenty-third": 23,
    "twenty third": 23, "twenty-fourth": 24, "twenty fourth": 24,
    "twenty-fifth": 25, "twenty fifth": 25, "twenty-sixth": 26,
    "twenty sixth": 26, "twenty-seventh": 27, "twenty seventh": 27,
    "twenty-eighth": 28, "twenty eighth": 28, "twenty-ninth": 29,
    "twenty ninth": 29, "thirtieth": 30, "thirty-first": 31, "thirty first": 31,
}

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NUMERIC_DAY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\b")
_IN_N_UNITS = re.compile(r"\bin\s+(\d+|a|one|two|three|four)\s+(day|week|month)s?\b")

_WORD_NUMBERS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4}

#: Phrases that sound like a date and are not one. Listed so that failing to
#: resolve them is a deliberate decision rather than an accident.
UNRESOLVABLE = (
    "soon", "shortly", "asap", "as soon as possible", "later", "eventually",
    "early", "mid", "late", "next sprint", "this sprint", "next quarter",
    "end of quarter", "before the audit", "after the audit", "next release",
)


def _friday_of_week(anchor: date, weeks_ahead: int) -> date:
    """Friday of the week containing `anchor`, shifted by whole weeks.

    Weeks run Monday to Sunday, which is what "this week" and "next week" mean
    in a working context.
    """
    monday = anchor - timedelta(days=anchor.weekday())
    return monday + timedelta(days=4 + 7 * weeks_ahead)


def _next_weekday(anchor: date, weekday: int) -> date:
    """The first occurrence of `weekday` strictly after `anchor`.

    Strictly after, because "by Friday" said in a Friday meeting means the
    following Friday, not the meeting that is already happening.
    """
    delta = (weekday - anchor.weekday()) % 7
    return anchor + timedelta(days=delta or 7)


def resolve_due_date(stated: str, meeting_date: str | None) -> ResolvedDate:
    """Turn what the transcript said into a concrete date where a rule allows.

    Returns UNSPECIFIED when nothing was stated, and also when something was
    stated that no rule resolves. The stated words are always kept.
    """
    raw = (stated or "").strip()

    if not raw or raw == UNSPECIFIED:
        return ResolvedDate(value=UNSPECIFIED, date_type=DateType.UNSPECIFIED)

    lowered = raw.lower().strip(" .,")

    if meeting_date is None:
        return ResolvedDate(
            value=UNSPECIFIED,
            date_type=DateType.UNSPECIFIED,
            stated_text=raw,
            rule="not resolved: the source has no meeting date to anchor against",
        )

    anchor = date.fromisoformat(meeting_date)

    # --- an ISO date, already concrete -------------------------------------
    iso = _ISO.search(lowered)
    if iso:
        return ResolvedDate(
            value=iso.group(0),
            date_type=DateType.ABSOLUTE,
            stated_text=raw,
            rule="the transcript stated an ISO date",
        )

    # --- "October fifteenth", "15 November", "November 15th" ---------------
    month = next((number for name, number in MONTHS.items() if re.search(rf"\b{name}\b", lowered)), None)
    if month:
        day = None
        for word, number in ORDINAL_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                day = number
                break
        if day is None:
            numeric = _NUMERIC_DAY.search(re.sub(r"\b(19|20)\d{2}\b", "", lowered))
            if numeric:
                day = int(numeric.group(1))
        if day and 1 <= day <= 31:
            # The transcript names a month and a day but rarely a year. Take the
            # first occurrence on or after the meeting date: a due date that has
            # already passed is never the intended reading.
            for year in (anchor.year, anchor.year + 1):
                try:
                    named = date(year, month, day)
                except ValueError:
                    break
                if named >= anchor:
                    return ResolvedDate(
                        value=named.isoformat(),
                        date_type=DateType.ABSOLUTE,
                        stated_text=raw,
                        rule=(
                            f"the transcript named a calendar date with no year; taken as "
                            f"{year}, the first occurrence on or after the meeting date "
                            f"{meeting_date}"
                        ),
                    )

    # --- phrases that sound like dates and are not --------------------------
    if any(re.search(rf"\b{re.escape(phrase)}\b", lowered) for phrase in UNRESOLVABLE):
        return ResolvedDate(
            value=UNSPECIFIED,
            date_type=DateType.UNSPECIFIED,
            stated_text=raw,
            rule=(
                "not resolved: the phrase is approximate and no rule maps it to a "
                "single date. Inventing one would be an invented date."
            ),
        )

    # --- today / tomorrow ---------------------------------------------------
    if "tomorrow" in lowered:
        return ResolvedDate(
            value=(anchor + timedelta(days=1)).isoformat(),
            date_type=DateType.RELATIVE_RESOLVED,
            stated_text=raw,
            rule=f"tomorrow = the day after the meeting date {meeting_date}",
        )
    if re.search(r"\btoday\b|\bend of (the )?day\b|\beod\b", lowered):
        return ResolvedDate(
            value=anchor.isoformat(),
            date_type=DateType.RELATIVE_RESOLVED,
            stated_text=raw,
            rule=f"today = the meeting date {meeting_date}",
        )

    # --- end of next week / next week / this week ---------------------------
    if re.search(r"\bnext week\b", lowered):
        phrase = "end of next week" if "end of" in lowered else "next week"
        return ResolvedDate(
            value=_friday_of_week(anchor, 1).isoformat(),
            date_type=DateType.RELATIVE_RESOLVED,
            stated_text=raw,
            rule=(
                f"{phrase} = Friday of the week following the meeting week; weeks run "
                f"Monday to Sunday, anchored to the meeting date {meeting_date}. A bare "
                f"'next week' is read as its end, since a commitment due 'next week' is "
                f"not late until the week is over."
            ),
        )
    if re.search(r"\b(this|the) week\b|\bend of week\b", lowered):
        return ResolvedDate(
            value=_friday_of_week(anchor, 0).isoformat(),
            date_type=DateType.RELATIVE_RESOLVED,
            stated_text=raw,
            rule=f"end of this week = Friday of the meeting week, anchored to {meeting_date}",
        )

    # --- end of the month ---------------------------------------------------
    if re.search(r"\bend of (the )?month\b", lowered):
        last = calendar.monthrange(anchor.year, anchor.month)[1]
        return ResolvedDate(
            value=date(anchor.year, anchor.month, last).isoformat(),
            date_type=DateType.RELATIVE_RESOLVED,
            stated_text=raw,
            rule=f"end of the month = the last day of the meeting month, anchored to {meeting_date}",
        )

    # --- in N days / weeks / months -----------------------------------------
    span = _IN_N_UNITS.search(lowered)
    if span:
        token, unit = span.group(1), span.group(2)
        count = _WORD_NUMBERS.get(token, None)
        if count is None:
            count = int(token)
        days = {"day": 1, "week": 7, "month": 30}[unit] * count
        return ResolvedDate(
            value=(anchor + timedelta(days=days)).isoformat(),
            date_type=DateType.RELATIVE_RESOLVED,
            stated_text=raw,
            rule=(
                f"in {count} {unit}(s) = {days} days after the meeting date {meeting_date}"
                + (", counting a month as 30 days" if unit == "month" else "")
            ),
        )

    # --- a bare weekday -----------------------------------------------------
    for name, index in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", lowered):
            weeks = 1 if "next" in lowered else 0
            resolved = _next_weekday(anchor, index) + timedelta(days=7 * weeks)
            return ResolvedDate(
                value=resolved.isoformat(),
                date_type=DateType.RELATIVE_RESOLVED,
                stated_text=raw,
                rule=(
                    f"{name} = the first {name} strictly after the meeting date {meeting_date}"
                    + (", shifted one week for 'next'" if weeks else "")
                ),
            )

    # --- nothing matched ----------------------------------------------------
    return ResolvedDate(
        value=UNSPECIFIED,
        date_type=DateType.UNSPECIFIED,
        stated_text=raw,
        rule="not resolved: no documented rule maps this phrasing to a single date",
    )

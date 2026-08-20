"""Quote verification (golden case 2) and date discipline (golden case 4)."""

from __future__ import annotations

import pytest

from app.extraction.dates import resolve_due_date
from app.extraction.quote_verifier import locate_quote, rejection_message, verify_quote
from app.models.common import UNSPECIFIED, DateType
from app.models.source import Segment

SOURCE = (
    "Can you get the full pipeline set up by end of next week? "
    "Yeah, end of next week should be doable. "
    "I can have the refactor done with tests by Friday."
)


# --- quote verification ------------------------------------------------------


def test_a_literal_quote_verifies():
    assert verify_quote("I can have the refactor done with tests by Friday", SOURCE)


def test_whitespace_differences_do_not_break_a_quote():
    """A source file wraps lines. A quote copied across a wrap must still match."""
    assert verify_quote("I can have the refactor\n   done with tests   by Friday", SOURCE)


def test_a_fabricated_quote_fails():
    assert not verify_quote("I will definitely finish everything by Friday", SOURCE)


def test_a_tidied_quote_fails_because_it_is_no_longer_verbatim():
    """Only whitespace is relaxed. Rewording, even harmlessly, is not verbatim."""
    assert not verify_quote("I can have the refactor done with tests by friday", SOURCE)
    assert not verify_quote("I can have the refactor done with the tests by Friday", SOURCE)


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_an_empty_quote_never_verifies(empty):
    assert not verify_quote(empty, SOURCE)


def test_locating_a_quote_gives_a_span_that_resolves():
    location = locate_quote("end of next week should be doable", SOURCE)
    assert location is not None
    assert SOURCE[location.char_start : location.char_end] == "end of next week should be doable"


def test_locating_a_fabricated_quote_returns_nothing():
    assert locate_quote("something never said", SOURCE) is None


def test_a_quote_is_attributed_to_the_segment_it_starts_in():
    segments = [
        Segment(id="seg0", source_id="s", segment_index=0, text="A" * 10, char_start=0, char_end=10),
        Segment(id="seg1", source_id="s", segment_index=1, text="B" * 10, char_start=11, char_end=21),
    ]
    location = locate_quote("BBB", "A" * 10 + " " + "B" * 10, segments)
    assert location.segment_id == "seg1"


def test_the_rejection_message_says_where_the_quote_started_to_drift():
    """The model is told how far it got, not merely that it was wrong."""
    message = rejection_message("I can have the refactor done with tests by Monday", SOURCE)
    assert "not a literal substring" in message
    assert "first" in message and "characters match" in message


def test_a_quote_absent_entirely_gets_a_different_message():
    message = rejection_message("nothing like this was ever said", SOURCE)
    assert "does not appear in the transcript at all" in message
    assert "without the timestamp or the speaker prefix" in message


# --- date discipline ---------------------------------------------------------

MEETING = "2024-11-18"  # a Monday


@pytest.mark.parametrize(
    ("stated", "expected", "kind"),
    [
        ("Friday", "2024-11-22", DateType.RELATIVE_RESOLVED),
        ("Wednesday", "2024-11-20", DateType.RELATIVE_RESOLVED),
        ("end of next week", "2024-11-29", DateType.RELATIVE_RESOLVED),
        ("tomorrow", "2024-11-19", DateType.RELATIVE_RESOLVED),
        ("end of the month", "2024-11-30", DateType.RELATIVE_RESOLVED),
        ("in two weeks", "2024-12-02", DateType.RELATIVE_RESOLVED),
        ("2024-12-01", "2024-12-01", DateType.ABSOLUTE),
        ("December third", "2024-12-03", DateType.ABSOLUTE),
    ],
)
def test_a_stated_date_resolves_by_a_documented_rule(stated, expected, kind):
    resolved = resolve_due_date(stated, MEETING)
    assert resolved.value == expected
    assert resolved.date_type is kind
    assert resolved.stated_text == stated
    assert resolved.rule, "every resolution must name the rule that produced it"


@pytest.mark.parametrize(
    "stated", ["early October", "soon", "next sprint", "after the audit", "ASAP", "later"]
)
def test_an_approximate_phrase_is_not_turned_into_a_date(stated):
    """An approximate date presented as a real one is the invented-date failure
    golden case 4 probes for."""
    resolved = resolve_due_date(stated, MEETING)
    assert resolved.value == UNSPECIFIED
    assert resolved.stated_text == stated
    assert "not resolved" in resolved.rule


@pytest.mark.parametrize("stated", [UNSPECIFIED, "", "   "])
def test_nothing_stated_stays_unspecified(stated):
    resolved = resolve_due_date(stated, MEETING)
    assert resolved.value == UNSPECIFIED
    assert resolved.date_type is DateType.UNSPECIFIED


def test_resolution_is_anchored_to_the_meeting_not_to_today():
    """Re-running the harness next month must give the same answer."""
    assert resolve_due_date("Friday", "2024-11-18").value == "2024-11-22"
    assert resolve_due_date("Friday", "2025-03-10").value == "2025-03-14"


def test_a_source_with_no_meeting_date_cannot_resolve_anything():
    resolved = resolve_due_date("Friday", None)
    assert resolved.value == UNSPECIFIED
    assert "no meeting date to anchor against" in resolved.rule


def test_a_bare_weekday_means_the_next_one_not_today():
    """"by Friday" said in a Friday meeting means the following Friday."""
    assert resolve_due_date("Friday", "2024-11-22").value == "2024-11-29"


def test_a_named_date_without_a_year_never_resolves_into_the_past():
    assert resolve_due_date("August 5th", "2024-08-19").value == "2025-08-05"
    assert resolve_due_date("October fifteenth", "2024-08-19").value == "2024-10-15"

"""M13 - which commitments belong to the same person.

The rule is stated rather than assumed: names group on their first name,
casefolded. The cost of the rule is that two people sharing a first name share
a digest, and the tests below pin both halves - the grouping happens, and the
evidence needed to tell them apart survives it.
"""

from __future__ import annotations

import pytest

from app.models.common import UNSPECIFIED
from app.people.identity import (
    UNASSIGNED,
    group_owners,
    is_unassigned,
    normalise,
    person_key,
    tokens,
)


# --- normalising --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  Priya   Sharma ", "Priya Sharma"),
        ("Priya Sharma,", "Priya Sharma"),
        ("(Marcus)", "Marcus"),
        ("Sarah  \n Chen", "Sarah Chen"),
        ("O'Brien", "O'Brien"),
        ("Jean-Luc", "Jean-Luc"),
        (None, ""),
    ],
)
def test_normalise_takes_the_noise_off_and_leaves_the_name(raw, expected):
    assert normalise(raw) == expected


def test_a_title_is_not_a_first_name():
    """Without this, "Dr Priya" becomes a person called Dr."""
    assert tokens("Dr Priya Sharma") == ["Priya", "Sharma"]
    assert person_key("Dr Priya Sharma") == person_key("Priya Sharma")


# --- the grouping rule --------------------------------------------------------


def test_a_first_name_and_a_full_name_are_one_person():
    assert person_key("Priya") == person_key("Priya Sharma") == "priya"


def test_case_and_spacing_do_not_make_a_second_person():
    assert person_key("  priya  ") == person_key("Priya")


def test_two_people_sharing_a_first_name_are_grouped():
    """The deliberate consequence of the rule, tested rather than discovered."""
    people = group_owners(["Priya Sharma", "Priya Menon"])

    assert len(people) == 1
    assert people[0].key == "priya"
    assert sorted(people[0].aliases) == ["Priya Menon", "Priya Sharma"]


def test_a_grouped_pair_reports_itself_as_ambiguous():
    """The grouping is visible to the caller, so the digest can say so."""
    people = group_owners(["Priya Sharma", "Priya Menon"])
    assert people[0].ambiguous is True
    assert people[0].display_name == "Priya", "the only thing the two names agree on"


def test_one_person_written_two_ways_is_not_ambiguous():
    people = group_owners(["Priya", "Priya Sharma"])
    assert people[0].ambiguous is False
    assert people[0].display_name == "Priya Sharma", "the fuller of the two forms"


def test_full_name_identity_keeps_them_apart():
    people = group_owners(["Priya Sharma", "Priya Menon"], identity="full_name")
    assert {p.key for p in people} == {"priya_sharma", "priya_menon"}


# --- the participant list is metadata, not a guess ----------------------------


def test_a_bare_first_name_takes_the_full_name_from_the_participant_list():
    people = group_owners(["Priya"], participants=["Priya Sharma", "Marcus Webb"])
    assert people[0].display_name == "Priya Sharma"


def test_it_refuses_to_choose_when_the_participant_list_is_ambiguous():
    """Two candidates and no way to choose, so it does not choose."""
    people = group_owners(["Priya"], participants=["Priya Sharma", "Priya Menon"])
    assert people[0].display_name == "Priya"


# --- nobody ------------------------------------------------------------------


@pytest.mark.parametrize("raw", [UNSPECIFIED, "", "   ", None, "unassigned", "TBD", "unknown"])
def test_nobody_is_nobody(raw):
    assert is_unassigned(raw) is True
    assert person_key(raw) == UNASSIGNED


def test_unowned_work_is_one_bucket_and_is_never_a_person():
    people = group_owners(["Priya", UNSPECIFIED, "", "TBD"])

    unassigned = [p for p in people if p.unassigned]
    assert len(unassigned) == 1
    assert unassigned[0].key == UNASSIGNED
    assert unassigned[0].display_name == "Assignee unspecified"
    assert unassigned[0].aliases == [UNSPECIFIED], "no placeholder is promoted to a name"


def test_unassigned_sorts_last_and_the_order_is_stable():
    first = group_owners(["Zoe", UNSPECIFIED, "Marcus", "Priya"])
    second = group_owners(["Priya", "Marcus", UNSPECIFIED, "Zoe"])

    assert [p.key for p in first] == [p.key for p in second]
    assert first[-1].key == UNASSIGNED

"""The sheet contract, pinned: the columns are the agent's or hers, the
formula view reads the right columns and cannot drift, a legacy header is
told apart from an edited one, and a pasted reference resolves to one
spreadsheet id or to nothing."""

from __future__ import annotations

import re
from datetime import date, datetime

import pytest

from inbox_triage_agent import sheet_layout as sl
from inbox_triage_agent.triage import CATEGORIES, NEEDS_REVIEW, html_to_text


def test_the_columns_and_their_indexes():
    assert sl.HEADERS == (
        "Date", "From", "Subject", "Category", "Summary", "Action", "Deadline", "Link",
        "Message ID", "Status", "Notes",
    )
    assert sl.AGENT_COLUMNS.index("Action") == sl.AGENT_COLUMNS.index("Summary") + 1
    assert (sl.COL_ACTION, sl.COL_DEADLINE, sl.COL_MESSAGE_ID, sl.COL_STATUS) == (6, 7, 9, 10)  # F G I J
    assert sl.AGENT_RANGE.format(row=7) == "A7:I7"
    assert sl.MESSAGE_ID_RANGE == "Inbox!I:I"
    assert sl.LEGACY_AGENT_COLUMNS + ("Status", "Notes") == (
        "Date", "From", "Subject", "Category", "Summary", "Deadline", "Link", "Message ID",
        "Status", "Notes",
    ), "the first release's layout, which the migration recognises"
    inserted = list(sl.LEGACY_AGENT_COLUMNS)
    for index, name in sl.MIGRATION_INSERTS:
        inserted.insert(index, name)
    assert tuple(inserted) == sl.AGENT_COLUMNS, "the inserts turn the legacy layout into the current one"


def test_the_upcoming_formula_cannot_drift_and_shows_what_is_needed_to_act():
    f = sl.UPCOMING_FORMULA
    # No row-numbered Inbox reference: Sheets shifts those on every append.
    assert not re.search(r"Inbox![A-Z]+\d", f), f
    assert "ROW(Inbox!A:A)>1" in f, "the header row is dropped by ROW(), not by A2"
    assert 'Inbox!G:G<>""' in f, "a deadline"
    assert 'Inbox!J:J<>"Done"' in f, "not Done"
    assert "CHOOSECOLS(Inbox!A:K, 7, 6, 2, 3, 5, 1, 10, 8)" in f
    assert f.endswith('"No open deadlines"))')
    assert sl.UPCOMING_HEADERS == (
        "Due", "Deadline", "Action", "From", "Subject", "Summary", "Received", "Status", "Link",
    )
    picked = [sl.HEADERS[col - 1] for _, col in sl.UPCOMING_COLUMNS]
    assert picked == ["Deadline", "Action", "From", "Subject", "Summary", "Date", "Status", "Link"]


def test_overdue_deadlines_are_kept_marked_and_listed_after_the_open_ones():
    f = sl.UPCOMING_FORMULA
    assert 'IF(Inbox!G:G<TEXT(TODAY(), "yyyy-mm-dd"), "Overdue", "Upcoming")' in f
    assert "), 1, FALSE, 2, TRUE)" in f, "Due descending, then Deadline ascending"
    # The sort on Due only works because of how the two words order.
    assert sorted([sl.DUE_OVERDUE, sl.DUE_UPCOMING], reverse=True) == ["Upcoming", "Overdue"]
    assert f.startswith("=ARRAYFORMULA("), "the IF over a whole column is array-evaluated"


def test_same_formula_ignores_spacing_and_case_only():
    assert sl.same_formula(sl.UPCOMING_FORMULA.replace(", ", ",").lower())
    assert not sl.same_formula("")
    assert not sl.same_formula(sl.UPCOMING_FORMULA.replace("Inbox!G:G", "Inbox!G85:G"))


@pytest.mark.parametrize("head, state", [
    ([], sl.HEADER_EMPTY),
    (["", ""], sl.HEADER_EMPTY),
    (list(sl.HEADERS), sl.HEADER_CURRENT),
    (list(sl.AGENT_COLUMNS), sl.HEADER_CURRENT),
    (list(sl.LEGACY_AGENT_COLUMNS) + ["Status", "Notes"], sl.HEADER_LEGACY),
    (["Date", "Sender", "Subject", "Category", "Summary", "Deadline", "Link", "Msg", "State"], sl.HEADER_LEGACY),
    (["Date", "From", "Subject", "Category", "Summary", "To do", "Deadline", "Link", "Message ID"], sl.HEADER_REPAIR),
    (["Date", "From", "Subject", "Category", "Summary", "Action", "Deadline", "Link", "Msg"], sl.HEADER_REPAIR),
    (["Name", "Phone", "City"], sl.HEADER_REPAIR),
])
def test_header_state(head, state):
    assert sl.header_state(head) == state


def test_every_category_and_the_review_marker_have_a_label():
    assert set(sl.CATEGORY_LABELS) == set(CATEGORIES) | {NEEDS_REVIEW}
    assert len(set(sl.CATEGORY_LABELS.values())) == len(sl.CATEGORY_LABELS)


def _facts(**kw):
    base = dict(
        message_id="18f3a2b1c0d9e8f7",
        received_at=datetime(2026, 9, 16, 9, 5),
        sender="Priya Nair <priya@ourfirm.com>",
        subject="Tuesday?",
        category="meeting",
        summary="Priya asks whether Tuesday 3pm still works for the interview.",
        deadline=date(2026, 9, 18),
        action="Reply to Priya confirming Tuesday's 3pm interview slot.",
    )
    base.update(kw)
    return sl.RowFacts(**base)


def test_agent_values_are_strings_in_column_order():
    assert sl.agent_values(_facts()) == [
        "2026-09-16 09:05",
        "Priya Nair <priya@ourfirm.com>",
        "Tuesday?",
        "Meeting",
        "Priya asks whether Tuesday 3pm still works for the interview.",
        "Reply to Priya confirming Tuesday's 3pm interview slot.",
        "2026-09-18",
        "https://mail.google.com/mail/u/0/#inbox/18f3a2b1c0d9e8f7",
        "18f3a2b1c0d9e8f7",
    ]


def test_no_action_is_stamped_by_code_not_left_blank():
    values = sl.agent_values(_facts(category="fyi", action=None, deadline=None))
    assert values[sl.COL_ACTION - 1] == sl.NO_ACTION == "No action — FYI"
    assert values[sl.COL_CATEGORY - 1] == "FYI / update"


def test_a_needs_review_row_carries_the_facts_and_blank_model_fields():
    facts = sl.RowFacts(
        message_id="abc", received_at=datetime(2026, 9, 16, 9, 5), sender="x@y.z",
        subject="", category="needs_review", summary="", deadline=None,
    )
    values = sl.agent_values(facts)
    assert values[sl.COL_SUBJECT - 1] == "(no subject)"
    assert values[sl.COL_CATEGORY - 1] == "Needs review"
    assert values[sl.COL_SUMMARY - 1] == "" and values[sl.COL_ACTION - 1] == ""
    assert values[sl.COL_DEADLINE - 1] == ""
    assert values[sl.COL_LINK - 1].endswith("/abc")


@pytest.mark.parametrize(
    "ref, expected",
    [
        ("https://docs.google.com/spreadsheets/d/1bYObEifoIh7zbJsLh9sPJDSkLe3oMvKixv-jdA4Tfg0/edit#gid=0",
         "1bYObEifoIh7zbJsLh9sPJDSkLe3oMvKixv-jdA4Tfg0"),
        ("  1bYObEifoIh7zbJsLh9sPJDSkLe3oMvKixv-jdA4Tfg0  ", "1bYObEifoIh7zbJsLh9sPJDSkLe3oMvKixv-jdA4Tfg0"),
        ("https://docs.google.com/document/d/1bYObEifoIh7zbJsLh9sPJDSkLe3oMvKixv-jdA4Tfg0/edit", None),
        ("https://drive.google.com/drive/folders/1bYObEifoIh7zbJsLh9sPJDSkLe3oMvKixv", None),
        ("short", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_sheet_ref(ref, expected):
    assert sl.parse_sheet_ref(ref) == expected


def test_sheet_url():
    assert sl.sheet_url("abc123") == "https://docs.google.com/spreadsheets/d/abc123/edit"


# --------------------------------------------------------------------------- #
# HTML → text (lives in triage, tested beside the sheet because both are
# "what the reader ends up reading")
# --------------------------------------------------------------------------- #


def test_html_blocks_become_lines_and_noise_is_dropped():
    html = (
        "<html><head><title>x</title><style>p{color:red}</style></head><body>"
        "<p>We need <b>two</b> paralegals&nbsp;in Pune.</p>"
        "<script>alert(1)</script>"
        "<div>Send us a shortlist by Friday.</div>"
        "<table><tr><td>Budget</td><td>12&ndash;15 LPA</td></tr></table>"
        "<img alt='' src='https://t.example/pixel.gif'>"
        "</body></html>"
    )
    assert html_to_text(html) == (
        "We need two paralegals in Pune.\n"
        "Send us a shortlist by Friday.\n"
        "Budget 12–15 LPA"
    )


def test_plain_text_passes_through_html_to_text_unchanged_in_substance():
    assert html_to_text("Just a line.\nAnd another.") == "Just a line.\nAnd another."
    assert html_to_text("") == ""

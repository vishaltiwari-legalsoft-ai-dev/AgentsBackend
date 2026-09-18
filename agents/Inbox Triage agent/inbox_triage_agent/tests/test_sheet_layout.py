"""The sheet contract, pinned: the columns are the agent's or hers, the
formula view reads the right columns, and a pasted reference resolves to
one spreadsheet id or to nothing."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from inbox_triage_agent import sheet_layout as sl
from inbox_triage_agent.triage import html_to_text


def test_the_columns_and_their_indexes():
    assert sl.HEADERS == (
        "Date", "From", "Subject", "Category", "Summary", "Deadline", "Link", "Message ID",
        "Status", "Notes",
    )
    assert sl.COL_DEADLINE == 6  # F
    assert sl.COL_MESSAGE_ID == 8  # H
    assert sl.COL_STATUS == 9  # I
    assert sl.AGENT_RANGE.format(row=7) == "A7:H7"
    assert sl.MESSAGE_ID_RANGE == "Inbox!H:H"


def test_the_upcoming_formula_reads_deadline_and_status_and_sorts_by_deadline():
    f = sl.UPCOMING_FORMULA
    assert f.startswith("=IFERROR(SORT(FILTER(Inbox!A2:J")
    assert 'Inbox!F2:F<>""' in f
    assert 'Inbox!I2:I<>"Done"' in f
    assert "), 6, TRUE)" in f
    assert f.endswith('"No open deadlines")')


def test_agent_values_are_eight_strings_in_column_order():
    facts = sl.RowFacts(
        message_id="18f3a2b1c0d9e8f7",
        received_at=datetime(2026, 9, 16, 9, 5),
        sender="gm@rathorelegal.in",
        subject="Paralegal search",
        category="role_to_fill",
        summary="The sender asks for a shortlist of two paralegals.",
        deadline=date(2026, 9, 18),
    )
    assert sl.agent_values(facts) == [
        "2026-09-16 09:05",
        "gm@rathorelegal.in",
        "Paralegal search",
        "role_to_fill",
        "The sender asks for a shortlist of two paralegals.",
        "2026-09-18",
        "https://mail.google.com/mail/u/0/#inbox/18f3a2b1c0d9e8f7",
        "18f3a2b1c0d9e8f7",
    ]


def test_a_needs_review_row_carries_the_facts_and_blank_model_fields():
    facts = sl.RowFacts(
        message_id="abc", received_at=datetime(2026, 9, 16, 9, 5), sender="x@y.z",
        subject="", category="needs_review", summary="", deadline=None,
    )
    values = sl.agent_values(facts)
    assert values[2] == "(no subject)"
    assert values[3] == "needs_review"
    assert values[4] == "" and values[5] == ""
    assert values[6].endswith("/abc")


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
# "what the recruiter ends up reading")
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

"""The sheet contract, pinned: the columns are the agent's or hers, the
formula view reads the right columns and cannot drift, a legacy header is
told apart from an edited one, and a pasted reference resolves to one
spreadsheet id or to nothing."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest

from inbox_triage_agent import sheet_layout as sl
from inbox_triage_agent import triage as sl_triage
from inbox_triage_agent import worktree as wt
from inbox_triage_agent.triage import CATEGORIES, NEEDS_REVIEW, html_to_text


def test_the_columns_and_their_indexes():
    assert sl.HEADERS == (
        "Date", "From", "Subject", "Category", "Summary", "Action", "Deadline", "Link",
        "Message ID", "Status", "Notes",
    )
    assert sl.AGENT_COLUMNS.index("Action") == sl.AGENT_COLUMNS.index("Summary") + 1
    assert (sl.COL_ACTION, sl.COL_DEADLINE, sl.COL_MESSAGE_ID, sl.COL_STATUS) == (6, 7, 9, 10)  # F G I J
    assert sl.MESSAGE_ID_RANGE == "Inbox!I:I" and sl.DATE_RANGE == "Inbox!A:A"
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


# --------------------------------------------------------------------------- #
# Worktree — the rules that turn the mail log into open work. Pure, so every
# rule below is asserted on its own, with no sheet and no store in the way.
# --------------------------------------------------------------------------- #

WT_TODAY = date(2026, 9, 21)
HERS = "her@firm.com"


def _msg(
    message_id="m1", *, thread="t1", days_ago=0, subject="Paralegal search",
    category="action_required", action="Send the shortlist.", deadline=None,
    from_me=False, status="",
):
    received = datetime(2026, 9, 21, 10, 0, tzinfo=sl_triage.TEAM_TIMEZONE) - timedelta(days=days_ago)
    return wt.ThreadMessage(
        message_id=message_id, thread_id=thread, received_at=received, subject=subject,
        category=category, action=action,
        deadline=date.fromisoformat(deadline) if deadline else None,
        from_me=from_me, user_status=status,
    )


def _one(*messages) -> wt.Process:
    tree = wt.build(list(messages), today=WT_TODAY)
    assert len(tree.processes) == 1, tree.processes
    return tree.processes[0]


@pytest.mark.parametrize("subject,title", [
    ("Re: Office lease", "Office lease"),
    ("RE: Fwd: RE[2]: Office lease", "Office lease"),
    ("FW:  Office lease ", "Office lease"),
    ("AW: Antwort: Buerolease", "Buerolease"),
    ("Office lease", "Office lease"),
    ("Re:", ""),
])
def test_a_title_is_the_subject_with_every_reply_prefix_stripped(subject, title):
    assert wt.strip_reply_prefixes(subject) == title


def test_the_title_is_the_threads_oldest_subject_the_way_gmail_names_it():
    process = _one(
        _msg("a", days_ago=4, subject="Office lease renewal"),
        _msg("b", days_ago=1, subject="Re: Office lease renewal - revised draft"),
    )
    assert process.title == "Office lease renewal"


def test_a_thread_with_nothing_but_blank_subjects_is_named_honestly():
    assert _one(_msg("a", subject=""), _msg("b", subject="Re:")).title == "(no subject)"


@pytest.mark.parametrize("address,expected", [
    ("her@firm.com", "her@firm.com"),
    ("Her Name <Her@Firm.com>", "her@firm.com"),
    ("  <her@firm.com> ", "her@firm.com"),
    ("Her Name", ""),
    ("", ""),
])
def test_an_address_is_read_out_of_a_from_header_case_folded(address, expected):
    assert wt.email_address(address) == expected


def test_direction_is_address_only_a_display_name_is_not_identity():
    assert wt.sent_by("Her Name <HER@firm.com>", HERS) is True
    assert wt.sent_by("Her Name <impostor@elsewhere.com>", HERS) is False
    assert wt.sent_by("her@firm.com", "") is False, "no connected address is not a yes"


def test_the_type_is_the_most_demanding_category_in_the_thread():
    process = _one(
        _msg("a", category="fyi"), _msg("b", category="notification"),
        _msg("c", category="action_required"),
    )
    assert process.type_label == sl.CATEGORY_LABELS["action_required"]


def test_needs_review_is_the_type_only_when_nothing_else_was_read():
    unread = _one(_msg("a", category=NEEDS_REVIEW, action=None))
    assert unread.type_label == sl.CATEGORY_LABELS[NEEDS_REVIEW]
    mixed = _one(_msg("a", category=NEEDS_REVIEW, action=None), _msg("b", category="fyi"))
    assert mixed.type_label == sl.CATEGORY_LABELS["fyi"]


def test_status_is_decided_by_who_sent_the_last_message():
    """The rule, pinned: the connected mailbox's own address on the LAST
    message means the ball is with the other side; anyone else's means it is
    with her."""
    theirs = _one(_msg("a", days_ago=2, from_me=True), _msg("b", days_ago=1, from_me=False))
    assert theirs.status == wt.STATUS_WAITING_ON_US
    ours = _one(_msg("a", days_ago=2, from_me=False), _msg("b", days_ago=1, from_me=True))
    assert ours.status == wt.STATUS_WAITING_ON_THEM


def test_a_thread_waiting_on_them_becomes_a_chase_at_exactly_three_days():
    assert wt.CHASE_SILENCE_DAYS == 3
    assert _one(_msg(days_ago=2, from_me=True)).status == wt.STATUS_WAITING_ON_THEM
    chasing = _one(_msg(days_ago=3, from_me=True))
    assert chasing.status == wt.STATUS_CHASING and chasing.chasing is True


def test_a_reply_we_owe_and_have_not_sent_for_three_days_is_its_own_alarm():
    """The other half of the chase, and the one the live inboxes will raise:
    they wrote, we never answered."""
    assert _one(_msg(days_ago=2, from_me=False)).status == wt.STATUS_WAITING_ON_US
    overdue = _one(_msg(days_ago=3, from_me=False))
    assert overdue.status == wt.STATUS_OVERDUE_REPLY and overdue.chasing is True
    assert _one(_msg(days_ago=20, from_me=False)).status == wt.STATUS_OVERDUE_REPLY


def test_a_thread_she_has_already_dealt_with_is_not_nagged_about():
    """One row marked Done or Ignore means she has engaged with it; the
    thread is still open, but it is not something she forgot."""
    for marked in ("Done", "Ignore"):
        process = _one(
            _msg("a", days_ago=9, from_me=False, status=marked),
            _msg("b", days_ago=5, from_me=False),
        )
        assert process.status == wt.STATUS_WAITING_ON_US and process.chasing is False
    # A chase on THEM is not exempted the same way: they still owe the reply.
    assert _one(
        _msg("a", days_ago=9, from_me=False, status="Done"),
        _msg("b", days_ago=5, from_me=True),
    ).status == wt.STATUS_CHASING


def test_both_alarms_share_the_chase_bucket():
    assert wt.CHASE_STATUSES == {wt.STATUS_CHASING, wt.STATUS_OVERDUE_REPLY}


def test_the_next_action_is_the_newest_one_she_has_not_closed():
    process = _one(
        _msg("a", days_ago=5, action="Send the engagement letter."),
        _msg("b", days_ago=2, action="Send the revised letter."),
        _msg("c", days_ago=1, action=None),
    )
    assert process.next_action == "Send the revised letter."
    closed = _one(
        _msg("a", days_ago=5, action="Send the engagement letter."),
        _msg("b", days_ago=2, action="Send the revised letter.", status="Done"),
    )
    assert closed.next_action == "Send the engagement letter."


def test_the_stamped_no_action_wording_is_not_an_action():
    assert _one(_msg(action=sl.NO_ACTION)).next_action == ""


def test_due_is_the_earliest_open_deadline_and_a_done_rows_deadline_is_not_open():
    process = _one(
        _msg("a", days_ago=3, deadline="2026-09-30"),
        _msg("b", days_ago=2, deadline="2026-09-24"),
    )
    assert process.due == date(2026, 9, 24)
    closed = _one(
        _msg("a", days_ago=3, deadline="2026-09-30"),
        _msg("b", days_ago=2, deadline="2026-09-24", status="Done"),
    )
    assert closed.due == date(2026, 9, 30)


def test_an_overdue_deadline_is_still_due_it_is_not_hidden():
    assert _one(_msg(days_ago=2, deadline="2026-09-10")).due == date(2026, 9, 10)


def test_waiting_since_counts_days_and_reads_as_plain_language():
    assert _one(_msg(days_ago=0)).waiting_since == "today"
    assert _one(_msg(days_ago=1)).waiting_since == "1 day"
    assert _one(_msg(days_ago=12)).waiting_since == "12 days"


def test_mails_counts_the_thread_and_latest_links_the_newest_message():
    process = _one(_msg("a", days_ago=3), _msg("b", days_ago=1), _msg("c", days_ago=2))
    assert process.mails == 3 and process.latest_link == sl.message_link("b")


def test_a_thread_of_nothing_but_noise_never_becomes_a_process():
    for category in ("newsletter_promo", "notification"):
        assert wt.build([_msg(category=category, action=None)], today=WT_TODAY).processes == []
    # One automated alert inside a real conversation does not hide it.
    mixed = wt.build(
        [_msg("a", category="notification", action=None), _msg("b", category="reply_needed")],
        today=WT_TODAY,
    )
    assert len(mixed.processes) == 1


def test_a_thread_is_closed_when_every_one_of_its_rows_is_done_or_ignore():
    assert wt.build(
        [_msg("a", status="Done"), _msg("b", status="Ignore")], today=WT_TODAY
    ).processes == []
    assert len(wt.build(
        [_msg("a", status="Done"), _msg("b", status="In progress")], today=WT_TODAY
    ).processes) == 1


def test_a_thread_silent_for_a_month_is_closed_whatever_its_rows_say():
    assert wt.CLOSED_AFTER_SILENT_DAYS == 30
    assert len(wt.build([_msg(days_ago=29)], today=WT_TODAY).processes) == 1
    assert wt.build([_msg(days_ago=30)], today=WT_TODAY).processes == []


def test_messages_with_no_thread_id_yet_are_counted_not_guessed_into_a_process():
    tree = wt.build([_msg("a", thread=""), _msg("b", thread="t1")], today=WT_TODAY)
    assert tree.untagged == 1 and [p.thread_id for p in tree.processes] == ["t1"]


def test_what_needs_chasing_sorts_to_the_top_then_deadlines_then_the_longest_ignored():
    tree = wt.build(
        [
            _msg("q", thread="quiet", days_ago=9, deadline=None, subject="Quiet one"),
            _msg("d", thread="due-late", days_ago=1, deadline="2026-10-10", subject="Later"),
            _msg("o", thread="overdue", days_ago=1, deadline="2026-09-01", subject="Overdue"),
            _msg("c", thread="chase", days_ago=6, from_me=True, subject="Chase me"),
            _msg("n", thread="newer-quiet", days_ago=2, deadline=None, subject="Newer quiet"),
        ],
        today=WT_TODAY,
    )
    assert [p.title for p in tree.processes] == [
        # Both alarms first, longest-silent of them at the very top, then the
        # deadlines earliest-first, then whatever is merely waiting.
        "Quiet one", "Chase me", "Overdue", "Later", "Newer quiet",
    ]
    assert [p.status for p in tree.processes[:2]] == [
        wt.STATUS_OVERDUE_REPLY, wt.STATUS_CHASING,
    ]


def test_the_cap_is_two_hundred_rows():
    assert wt.MAX_PROCESSES == 200


def test_the_rows_are_capped_and_the_true_total_is_still_reported():
    messages = [_msg(f"m{i}", thread=f"t{i}", subject=f"S{i}") for i in range(wt.MAX_PROCESSES + 5)]
    tree = wt.build(messages, today=WT_TODAY)
    assert len(tree.processes) == wt.MAX_PROCESSES and tree.total == wt.MAX_PROCESSES + 5
    assert len({p.thread_id for p in tree.processes}) == wt.MAX_PROCESSES, "no thread twice"


def test_a_worktree_row_is_cells_in_header_order():
    process = _one(_msg("a", days_ago=2, deadline="2026-09-24", from_me=True))
    assert dict(zip(sl.WORKTREE_HEADERS, wt.values(process))) == {
        "Process": "Paralegal search",
        "Type": sl.CATEGORY_LABELS["action_required"],
        "Status": wt.STATUS_WAITING_ON_THEM,
        "Next action": "Send the shortlist.",
        "Waiting since": "2 days",
        "Due": "2026-09-24",
        "Mails": "1",
        "Latest": sl.message_link("a"),
    }


def test_an_inbox_row_is_read_back_into_the_facts_a_process_is_made_of():
    facts = sl.RowFacts(
        message_id="m9", received_at=datetime(2026, 9, 19, 14, 30, tzinfo=sl_triage.TEAM_TIMEZONE),
        sender="Her Name <HER@firm.com>", subject="Re: Lease", category="reply_needed",
        summary="She asks about the lease.", deadline=date(2026, 9, 25),
        action="Answer the landlord by 25 Sep.",
    )
    cells = sl.agent_values(facts) + ["In progress", "chase Friday"]
    message = wt.from_row(cells, connected_address=HERS, thread_ids={"m9": "t-9"})
    assert message.message_id == "m9" and message.thread_id == "t-9"
    assert message.received_at == facts.received_at
    assert message.subject == "Re: Lease" and message.category == "reply_needed"
    assert message.deadline == date(2026, 9, 25)
    assert message.action == "Answer the landlord by 25 Sep."
    assert message.from_me is True and message.user_status == "In progress"


def test_a_row_with_no_message_id_is_not_a_message_and_unreadable_cells_are_not_guesses():
    assert wt.from_row([""] * len(sl.HEADERS), connected_address=HERS, thread_ids={}) is None
    ragged = ["not a date", "x@y.com", "Subject"]  # she deleted the columns to the right
    message = wt.from_row(
        ragged + [""] * (sl.COL_MESSAGE_ID - 4) + ["m1"],
        connected_address=HERS, thread_ids={},
    )
    assert message.received_at is None and message.deadline is None
    assert message.category == "" and message.thread_id == ""


# --------------------------------------------------------------------------- #
# Sent markers — mail she sent, known only by id, thread and time
# --------------------------------------------------------------------------- #

def _marker(message_id="s1", *, thread="t1", days_ago=0):
    received = datetime(2026, 9, 21, 10, 0, tzinfo=sl_triage.TEAM_TIMEZONE) - timedelta(days=days_ago)
    return wt.ThreadMessage(
        message_id=message_id, thread_id=thread, received_at=received, subject="",
        category="", action=None, deadline=None, from_me=True, on_sheet=False,
    )


def test_a_marker_is_read_off_its_document_and_carries_nothing_but_a_time_and_a_side():
    marker = wt.from_marker({
        "message_id": "s1", "thread_id": "t1", "kind": "sent", "from_me": True,
        "received_at": "2026-09-20T11:00:00+05:30",
    })
    assert marker.from_me is True and marker.on_sheet is False
    assert marker.received_at == datetime(2026, 9, 20, 11, 0, tzinfo=sl_triage.TEAM_TIMEZONE)
    assert (marker.subject, marker.category, marker.action, marker.deadline) == ("", "", None, None)


@pytest.mark.parametrize("doc", [
    {"message_id": "s1", "thread_id": "t1"},                       # no stamp yet
    {"message_id": "s1", "thread_id": "t1", "received_at": None},
    {"message_id": "s1", "thread_id": "t1", "received_at": "not a time"},
    {"message_id": "s1", "received_at": "2026-09-20T11:00:00+05:30"},  # no thread
    {"thread_id": "t1", "received_at": "2026-09-20T11:00:00+05:30"},   # no id
])
def test_a_marker_with_no_usable_time_is_not_a_message_at_all(doc):
    assert wt.from_marker(doc) is None


def test_an_unstamped_marker_leaves_the_thread_reading_exactly_as_it_did():
    """The half-ingested state must be the old answer, never a wrong one."""
    before = _one(_msg("a", days_ago=5, from_me=False))
    assert before.status == wt.STATUS_OVERDUE_REPLY and before.mails == 1
    assert wt.from_marker({"message_id": "s1", "thread_id": "t1"}) is None


def test_her_own_reply_flips_the_thread_to_waiting_on_them():
    process = _one(_msg("a", days_ago=5, from_me=False), _marker("s1", days_ago=1))
    assert process.status == wt.STATUS_WAITING_ON_THEM
    assert process.mails == 2, "a marker is a message in the thread, and is counted"
    assert process.waiting_since == "1 day", "silence is measured from HER reply"


def test_a_reply_she_sent_and_they_never_answered_becomes_a_chase():
    process = _one(_msg("a", days_ago=9, from_me=False), _marker("s1", days_ago=4))
    assert process.status == wt.STATUS_CHASING


def test_latest_links_a_message_on_the_sheet_even_when_her_reply_is_newer():
    """A marker has no row and no subject, so there is nothing of it to open;
    Gmail shows the whole conversation from any message in it anyway."""
    process = _one(_msg("a", days_ago=5), _marker("s1", days_ago=1))
    assert process.latest_link == sl.message_link("a")


def test_a_thread_of_nothing_but_her_own_mail_is_not_a_process():
    """She wrote into a conversation the agent never ingested: no subject to
    name it, no triage to describe it. Counting it would be a blank row."""
    assert wt.build([_marker("s1", thread="t9")], today=WT_TODAY).processes == []


def test_answering_a_newsletter_makes_it_a_conversation():
    only_noise = [_msg("a", category="newsletter_promo", action=None)]
    assert wt.build(only_noise, today=WT_TODAY).processes == []
    answered = only_noise + [_marker("s1", days_ago=1)]
    assert len(wt.build(answered, today=WT_TODAY).processes) == 1


def test_done_is_judged_on_the_rows_because_that_is_where_her_status_lives():
    """She marked the row Done; that she also replied does not reopen it."""
    thread = [_msg("a", days_ago=2, status="Done"), _marker("s1", days_ago=1)]
    assert wt.build(thread, today=WT_TODAY).processes == []


def test_the_build_reports_how_many_markers_it_actually_used():
    tree = wt.build(
        [_msg("a", days_ago=2), _marker("s1", days_ago=1),
         _marker("s9", thread="orphan", days_ago=1)],
        today=WT_TODAY,
    )
    assert tree.sent_used == 1, "the orphan thread is not a process, so its marker is not used"


# --------------------------------------------------------------------------- #
# Pinned 2026-09-29: a thread with fresh mail is on the tab however many
# alarms there are. One live inbox had 453 open processes; the 200 rows were
# all alarms two to four weeks old, and nothing that began today could appear.
# --------------------------------------------------------------------------- #

def _alarms(count: int) -> list:
    """Threads we owe a reply on, silent for 5 to 25 days: every one an alarm."""
    return [
        _msg(f"old{i}", thread=f"alarm-{i:03d}", subject=f"Alarm {i:03d}", days_ago=5 + i % 21)
        for i in range(count)
    ]


def test_a_thread_with_fresh_mail_is_on_the_tab_when_there_are_more_alarms_than_rows():
    fresh = _msg("new", thread="fresh", subject="Started today", days_ago=0, deadline=None)
    tree = wt.build(_alarms(453) + [fresh], today=WT_TODAY)

    assert tree.total == 454 and len(tree.processes) == wt.MAX_PROCESSES
    shown = {p.thread_id: p for p in tree.processes}
    assert "fresh" in shown and shown["fresh"].status == wt.STATUS_WAITING_ON_US
    assert shown["fresh"].waiting_since == "today"
    assert sum(1 for p in tree.processes if p.chasing) >= wt.MAX_PROCESSES - wt.RECENT_RESERVED, \
        "and the tab is still mostly what is most urgent"


def test_the_reserve_is_for_the_fifty_threads_with_the_most_recent_mail():
    assert wt.RECENT_RESERVED == 50
    recent = [
        _msg(f"r{i}", thread=f"recent-{i:02d}", subject=f"Recent {i:02d}", days_ago=i // 30)
        for i in range(60)
    ]  # thirty from today, thirty from yesterday
    # Give the threads of one day different times, newest first by number.
    recent = [
        wt.ThreadMessage(**{**m.__dict__, "received_at": m.received_at - timedelta(minutes=i)})
        for i, m in enumerate(recent)
    ]
    tree = wt.build(_alarms(300) + recent, today=WT_TODAY)

    kept = {p.thread_id for p in tree.processes if p.thread_id.startswith("recent-")}
    assert kept == {f"recent-{i:02d}" for i in range(50)}, "the fifty most recent, not any fifty"
    assert len(tree.processes) == wt.MAX_PROCESSES
    alarms = [p for p in tree.processes if p.chasing]
    assert len(alarms) == 150 and min(p.waiting_days for p in alarms) >= 11, \
        "the other rows go to the longest-ignored alarms, as before"


def test_the_order_on_the_tab_is_the_same_whichever_rule_kept_a_row():
    tree = wt.build(_alarms(300) + [_msg("new", thread="fresh", days_ago=0)], today=WT_TODAY)
    assert tree.processes == sorted(tree.processes, key=wt.Process.sort_key)
    assert tree.processes[-1].thread_id == "fresh", "alarms first; what is merely waiting after"
    again = wt.build(list(reversed(_alarms(300))) + [_msg("new", thread="fresh", days_ago=0)],
                     today=WT_TODAY)
    assert again.processes == tree.processes, "the same facts always choose the same rows"


def test_when_everything_fits_nothing_about_the_tab_changes():
    messages = _alarms(120) + [
        _msg(f"r{i}", thread=f"recent-{i}", subject=f"Recent {i}", days_ago=0) for i in range(80)
    ]
    tree = wt.build(messages, today=WT_TODAY)
    assert len(tree.processes) == tree.total == 200
    assert [p.chasing for p in tree.processes] == [True] * 120 + [False] * 80


def test_a_thread_whose_date_could_not_be_read_never_takes_a_reserved_row():
    unread = wt.ThreadMessage(
        message_id="x", thread_id="undated", received_at=None, subject="Undated",
        category="fyi", action=None, deadline=None, from_me=False,
    )
    fresh = [_msg(f"r{i}", thread=f"recent-{i:02d}", subject=f"Recent {i:02d}") for i in range(50)]
    tree = wt.build(_alarms(300) + fresh + [unread], today=WT_TODAY)
    assert "undated" not in {p.thread_id for p in tree.processes}


# --------------------------------------------------------------------------- #
# Pinned 2026-09-29: where a new row goes
# --------------------------------------------------------------------------- #

SHEET_DATES = ["Date", "2026-09-20 09:00", "2026-09-18 09:00", "2026-09-16 09:00"]


def test_a_row_goes_above_the_first_row_as_old_as_it_or_older():
    place = lambda *new: sl.place(SHEET_DATES, list(new), end=4)  # noqa: E731
    assert place("2026-09-29 10:00") == [sl.Block(at=1, rows=(0,))], "new mail: the top"
    assert place("2026-09-19 10:00") == [sl.Block(at=2, rows=(0,))], "in between"
    assert place("2026-09-18 09:00") == [sl.Block(at=2, rows=(0,))], "a tie goes above"
    assert place("2026-06-01 10:00") == [sl.Block(at=4, rows=(0,))], "older than all: the bottom"
    assert sl.place(["Date"], ["2026-09-29 10:00"], end=1) == [sl.Block(at=1, rows=(0,))]


def test_rows_for_one_place_are_one_block_newest_first_whatever_order_they_came_in():
    blocks = sl.place(
        SHEET_DATES,
        ["2026-09-29 08:00", "2026-06-01 10:00", "2026-09-29 17:00", "2026-09-17 12:00"], end=4)
    assert blocks == [
        sl.Block(at=1, rows=(2, 0)), sl.Block(at=3, rows=(3,)), sl.Block(at=4, rows=(1,)),
    ]
    assert sl.landed(blocks, 4) == [3, 8, 2, 6]
    # The rows that were there, by 0-based index as read -> where they are now.
    assert [sl.pushed(i, blocks) for i in (1, 2, 3)] == [3, 4, 6]


def test_a_cell_that_is_not_a_date_the_agent_wrote_is_passed_over():
    dates = ["Date", "call the landlord", "", "2026-09-20 09:00", "20/09/2026", "2026-09-16 09:00"]
    assert sl.place(dates, ["2026-09-29 10:00"], end=6) == [sl.Block(at=3, rows=(0,))]
    assert sl.place(dates, ["2026-09-17 10:00"], end=6) == [sl.Block(at=5, rows=(0,))]
    assert [sl.is_date_cell(c) for c in dates] == [False, False, False, True, False, True]


def test_the_blocks_are_sent_bottom_first_so_every_index_is_the_one_that_was_read():
    rows = [["2026-09-29 08:00"] + ["x"] * 8, ["2026-06-01 10:00"] + ["y"] * 8]
    blocks = sl.place(SHEET_DATES, [row[0] for row in rows], end=4)
    requests = sl.insert_requests(5, blocks, rows)
    assert [name for r in requests for name in r] == [
        "insertDimension", "updateCells",                    # the bottom block, at index 4
        "insertDimension", "moveDimension", "updateCells",   # then the top one
    ]
    assert requests[0]["insertDimension"]["range"] == {
        "sheetId": 5, "dimension": "ROWS", "startIndex": 4, "endIndex": 5}
    assert requests[1]["updateCells"]["start"]["rowIndex"] == 4
    assert requests[4]["updateCells"]["start"]["rowIndex"] == 1
    assert all(r["insertDimension"]["inheritFromBefore"] for r in requests if "insertDimension" in r)


def test_the_order_marker_is_read_by_key_and_version():
    marked = {"developerMetadata": [
        {"metadataId": 3, "metadataKey": sl.ORDER_MARKER_KEY, "metadataValue": sl.ORDER_VERSION}]}
    assert sl.is_newest_first(marked)
    assert not sl.is_newest_first({})
    assert not sl.is_newest_first({"developerMetadata": [
        {"metadataKey": "agentos.a12.format", "metadataValue": "2"}]})
    stale = {"developerMetadata": [
        {"metadataId": 9, "metadataKey": sl.ORDER_MARKER_KEY, "metadataValue": "something-older"}]}
    assert not sl.is_newest_first(stale)
    assert {"deleteDeveloperMetadata": {"dataFilter": {"developerMetadataLookup": {"metadataId": 9}}}} \
        in sl.order_marker_requests(stale), "a marker of another version is replaced, not added to"


# --------------------------------------------------------------------------- #
# Pinned 2026-09-30: what the independent verification of the newest-first
# change found. A date is read from what Sheets STORES; the sort is only
# marked done when the tab was read back in order; a batch that moves rows
# carries a stamp that lets only one of two batches land.
# --------------------------------------------------------------------------- #

def _serial(when: str) -> float:
    """The number Sheets stores for a real date."""
    from datetime import datetime

    moment = datetime.strptime(when, "%Y-%m-%d %H:%M")
    return (moment - datetime(1899, 12, 30)).total_seconds() / 86400


def test_a_date_is_read_from_what_sheets_stores_whatever_the_column_shows():
    from datetime import datetime

    ten = datetime(2026, 9, 10, 10, 0)
    assert sl.date_key("2026-09-10 10:00") == ten, "the agent's own text"
    assert sl.date_key(" 2026-09-10 10:00 ") == ten
    assert sl.date_key(46275.416666666664) == ten, "a real date: days since 1899-12-30"
    assert sl.date_key(_serial("2026-09-10 10:00")) == ten
    assert sl.date_key(46275) == datetime(2026, 9, 10), "a real date with no time of day"
    for not_a_date in ("", None, "call the landlord", "9/10/2026 10:00:00", "20/09/2026",
                       "2026-02-31 09:00", True, False, 12, 3.5, 1e15):
        assert sl.date_key(not_a_date) is None, not_a_date
    assert sl.is_real_date(46275.5) and not sl.is_real_date("2026-09-10 10:00")


def test_new_mail_is_the_first_data_row_when_the_date_column_is_shown_in_another_format():
    """Seen live (W4b): the cells are real dates shown as m/d/yyyy h:mm:ss,
    and read as they are SHOWN every one was passed over, so new mail went
    under the last row - the very thing the change exists to stop. Read as
    they are stored they are dates like any other."""
    dates = ["Date", _serial("2026-09-10 10:00"), _serial("2026-09-08 10:00"),
             _serial("2026-09-07 10:00")]
    blocks = sl.place(dates, ["2026-09-12 09:00"], end=len(dates))
    assert sl.landed(blocks, 1) == [2], f"today's mail was put on row {sl.landed(blocks, 1)[0]}"
    assert sl.landed(sl.place(dates, ["2026-09-08 10:00"], end=4), 1) == [3], "a tie goes above"
    assert sl.landed(sl.place(dates, ["2026-06-01 10:00"], end=4), 1) == [5], "the oldest: the bottom"


def test_text_dates_real_dates_and_a_mix_are_placed_and_checked_by_one_rule():
    text = ["Date", "2026-09-20 09:00", "2026-09-18 09:00", "2026-09-16 09:00"]
    real = ["Date"] + [_serial(cell) for cell in text[1:]]
    mixed = ["Date", text[1], real[2], text[3]]
    for dates in (text, real, mixed):
        assert sl.first_out_of_order(dates) is None
        assert sl.place(dates, ["2026-09-29 10:00"], end=4) == [sl.Block(at=1, rows=(0,))]
        assert sl.place(dates, ["2026-09-19 10:00"], end=4) == [sl.Block(at=2, rows=(0,))]
        assert sl.place(dates, ["2026-09-17 10:00"], end=4) == [sl.Block(at=3, rows=(0,))]
        assert sl.place(dates, ["2026-06-01 10:00"], end=4) == [sl.Block(at=4, rows=(0,))]
    # What the placement calls in order, the check calls in order - and what
    # it would put a row above, the check calls out of order.
    assert sl.first_out_of_order(["Date", text[3], real[1]]) == 3
    assert sl.first_out_of_order(["Date", real[3], text[1]]) == 3


def test_the_newest_first_check_names_the_first_row_out_of_order_and_passes_over_her_rows():
    assert sl.first_out_of_order(["Date"]) is None and sl.first_out_of_order([]) is None
    assert sl.first_out_of_order(["Date", "2026-09-20 09:00"]) is None
    assert sl.first_out_of_order(
        ["Date", "2026-09-20 09:00", "2026-09-20 09:00", "2026-09-01 09:00"]) is None, "a tie is in order"
    assert sl.first_out_of_order(
        ["Date", "CALL BACK Anil", "2026-09-20 09:00", "", "2026-09-18 09:00", ""]) is None
    assert sl.first_out_of_order(
        ["Date", "2026-09-18 09:00", "", "her line", "2026-09-20 09:00"]) == 5
    # The live sheets as they stand: the backfill newest first, then what
    # came since oldest first underneath.
    split = ["Date", "2026-09-18 09:00", "2026-09-17 09:00", "2026-09-19 08:00", "2026-09-20 08:00"]
    assert sl.first_out_of_order(split) == 4


def test_the_sort_is_the_sort_and_the_marker_is_a_batch_of_its_own():
    requests = sl.sort_requests(7)
    assert [name for r in requests for name in r] == ["sortRange"], \
        "no marker rides with the sort: that it was applied does not say the tab is in order"
    sort = requests[0]["sortRange"]
    assert sort["range"] == {"sheetId": 7, "startRowIndex": 1}, \
        "below the header, to the end, and NO column bounds: a row moves with every cell in it"
    assert sort["sortSpecs"] == [
        {"dimensionIndex": 0, "sortOrder": "DESCENDING"},                      # Date
        {"dimensionIndex": sl.COL_MESSAGE_ID - 1, "sortOrder": "DESCENDING"},  # a tie inside a minute
    ]
    marker = sl.order_marker_requests({})
    assert [name for r in marker for name in r] == ["createDeveloperMetadata"]
    entry = marker[0]["createDeveloperMetadata"]["developerMetadata"]
    assert (entry["metadataKey"], entry["metadataValue"]) == (sl.ORDER_MARKER_KEY, sl.ORDER_VERSION)
    assert entry["location"] == {"spreadsheet": True}


MERGE_K3_K4 = {"sheetId": 1, "startRowIndex": 2, "endRowIndex": 4,
               "startColumnIndex": 10, "endColumnIndex": 11}
SPLIT = ["Date", "2026-09-18 09:00", "2026-09-17 09:00", "2026-09-19 08:00"]


def _blocker(dates=None, **kw):
    given = {"merges": [], "hidden_by_filter": [], "hidden_by_user": [], **kw}
    return sl.sort_blocker(SPLIT if dates is None else dates, **given)


def test_a_tab_the_sort_would_put_right_has_no_blocker():
    assert _blocker() is None
    assert _blocker(["Date"] + [_serial(cell) for cell in SPLIT[1:]]) is None, "all real dates sort"
    assert _blocker(["Date", "her line", "", *SPLIT[1:]]) is None
    wide = {"sheetId": 1, "startRowIndex": 6, "endRowIndex": 7,
            "startColumnIndex": 0, "endColumnIndex": 11}
    assert _blocker(merges=[wide]) is None, "a merge across ONE row sorts"
    header = {"sheetId": 1, "startRowIndex": 0, "endRowIndex": 1,
              "startColumnIndex": 9, "endColumnIndex": 11}
    assert _blocker(merges=[header]) is None


def test_a_blocker_names_what_to_change_and_where():
    merged = _blocker(merges=[MERGE_K3_K4])
    assert "merged cells at K3:K4" in merged and "Unmerge" in merged
    assert sl.a1_range(MERGE_K3_K4) == "K3:K4"
    two = _blocker(merges=[MERGE_K3_K4, {**MERGE_K3_K4, "startColumnIndex": 12,
                                         "endColumnIndex": 13, "startRowIndex": 5, "endRowIndex": 8}])
    assert "K3:K4, M6:M8" in two

    filtered = _blocker(hidden_by_filter=[3, 4, 6, 9])
    assert "a filter is hiding 4 rows (rows 3-4, 6, 9)" in filtered and "Remove filter" in filtered
    assert "a filter is hiding 1 row (rows 7)" in _blocker(hidden_by_filter=[7])

    hidden = _blocker(hidden_by_user=[4, 5])
    assert "rows 4-5 are hidden" in hidden and "Unhide" in hidden

    mixed = _blocker(["Date", "2026-09-18 09:00", _serial("2026-09-17 09:00"),
                      "2026-09-19 08:00", _serial("2026-09-20 08:00"), "2026-09-21 08:00"])
    assert "2 cells are real dates: A3, A5" in mixed and "Plain text" in mixed
    mostly_real = _blocker(["Date", _serial("2026-09-18 09:00"), _serial("2026-09-17 09:00"),
                            "2026-09-19 08:00"])
    assert "1 cell is text: A4" in mostly_real

    # What Sheets refuses outright is said before what it would merely do wrongly.
    assert "merged cells" in _blocker(merges=[MERGE_K3_K4], hidden_by_filter=[3])


def test_the_note_says_what_is_wrong_that_mail_still_lands_and_when_it_is_tried_again():
    note = sl.order_note(_blocker(merges=[MERGE_K3_K4]), retry="every hour")
    assert note == (
        "The Inbox tab has not been put in newest-first order yet: it has merged cells at "
        "K3:K4, and Google Sheets cannot sort rows that are merged together. Unmerge them "
        "(Format > Merge cells > Unmerge). New mail is still added at the top. The agent "
        "tries the sort again every hour."
    )
    unrecorded = sl.unrecorded_note("HTTP 400", retry="every hour")
    assert unrecorded.startswith("The Inbox tab is in newest-first order, but")
    assert "New mail is still added at the top" in unrecorded and "every hour" in unrecorded


def _stamp_of(requests: list[dict]) -> dict:
    return requests[0]["createDeveloperMetadata"]["developerMetadata"]


def _on_sheet(requests: list[dict]) -> dict:
    """The metadata a sheet holds once a batch carrying this stamp landed."""
    return dict(_stamp_of(requests))


def test_the_stamp_is_first_in_the_batch_and_its_id_is_the_next_in_the_sequence():
    first = sl.rows_stamp({}, token="aaa", now_epoch=1000)
    assert [name for r in first for name in r] == ["createDeveloperMetadata"]
    stamp = _stamp_of(first)
    assert stamp["metadataId"] == sl.ROWS_STAMP_BASE + 1
    assert stamp["metadataKey"] == sl.ROWS_STAMP_KEY and stamp["metadataValue"] == "1@1000@aaa"
    assert stamp["location"] == {"spreadsheet": True}

    meta = {"developerMetadata": [_on_sheet(first)]}
    assert sl.rows_sequence({}) == 0 and sl.rows_sequence(meta) == 1
    assert sl.rows_stamped_by(meta, "aaa") and not sl.rows_stamped_by(meta, "bbb")
    assert not sl.rows_stamped_by(meta, ""), "no token is no batch"
    assert _stamp_of(sl.rows_stamp(meta, token="bbb", now_epoch=1010))["metadataId"] == \
        sl.ROWS_STAMP_BASE + 2


def test_two_batches_planned_from_one_reading_ask_for_the_same_id():
    """Which is the whole of it: Sheets creates an ID once, and refuses the
    batch that asks for it second - another fire's, or this one's own sent
    again."""
    meta = {"developerMetadata": [_on_sheet(sl.rows_stamp({}, token="aaa", now_epoch=1000))]}
    mine = _stamp_of(sl.rows_stamp(meta, token="mine", now_epoch=1500))
    theirs = _stamp_of(sl.rows_stamp(meta, token="theirs", now_epoch=1501))
    assert mine["metadataId"] == theirs["metadataId"]
    assert mine["metadataValue"] != theirs["metadataValue"], "and the sheet says whose landed"


def test_an_id_some_other_metadata_holds_is_stepped_over_by_every_writer_alike():
    taken = {"developerMetadata": [
        {"metadataId": sl.ROWS_STAMP_BASE + 1, "metadataKey": "someone.elses", "metadataValue": "x"},
    ]}
    for token in ("mine", "theirs"):
        assert _stamp_of(sl.rows_stamp(taken, token=token, now_epoch=1))["metadataId"] == \
            sl.ROWS_STAMP_BASE + 2


def test_stamps_are_dropped_after_an_hour_and_the_latest_never_is():
    hour = sl.ROWS_STAMP_KEEP_SECONDS
    meta = {"developerMetadata": [
        {"metadataId": sl.ROWS_STAMP_BASE + 1, "metadataKey": sl.ROWS_STAMP_KEY,
         "metadataValue": "1@1000@a"},
        {"metadataId": sl.ROWS_STAMP_BASE + 2, "metadataKey": sl.ROWS_STAMP_KEY,
         "metadataValue": "2@5000@b"},
        {"metadataId": sl.ROWS_STAMP_BASE + 3, "metadataKey": sl.ROWS_STAMP_KEY,
         "metadataValue": "3@5100@c"},
        {"metadataId": 44, "metadataKey": sl.ORDER_MARKER_KEY, "metadataValue": sl.ORDER_VERSION},
    ]}

    def dropped(now_epoch: int) -> list[int]:
        requests = sl.rows_stamp(meta, token="d", now_epoch=now_epoch)
        assert "createDeveloperMetadata" in requests[0], "the stamp is always the first request"
        return [r["deleteDeveloperMetadata"]["dataFilter"]["developerMetadataLookup"]["metadataId"]
                for r in requests[1:]]

    assert dropped(5200) == [sl.ROWS_STAMP_BASE + 1], "only the one older than an hour"
    assert dropped(5000 + hour) == [sl.ROWS_STAMP_BASE + 1], "an hour to the second is kept"
    assert dropped(5000 + hour + 1) == [sl.ROWS_STAMP_BASE + 1, sl.ROWS_STAMP_BASE + 2]
    assert dropped(10 ** 9) == [sl.ROWS_STAMP_BASE + 1, sl.ROWS_STAMP_BASE + 2], \
        "the latest stays however old: it is what the next batch is numbered from"
    assert _stamp_of(sl.rows_stamp(meta, token="d", now_epoch=1))["metadataId"] == \
        sl.ROWS_STAMP_BASE + 4


def test_a_stamp_whose_value_cannot_be_read_is_numbered_from_its_id():
    meta = {"developerMetadata": [
        {"metadataId": sl.ROWS_STAMP_BASE + 6, "metadataKey": sl.ROWS_STAMP_KEY,
         "metadataValue": "she edited this"},
    ]}
    assert sl.rows_sequence(meta) == 6


NEW_ROWS = [["2026-09-29 08:00"] + ["x"] * 8, ["2026-09-29 07:00"] + ["y"] * 8]


def test_rows_go_in_above_a_first_row_that_is_merged_with_the_row_below_it():
    """Seen live (W3a): with K2:K3 merged, moving row 2 beneath the new rows
    is refused - "not possible to move a row to a position that crosses a
    merged cell" - and the whole write with it. Tried live (E4): inserting AT
    row 2 is taken, and the merge moves down whole."""
    merge = {"sheetId": 5, "startRowIndex": 1, "endRowIndex": 3,
             "startColumnIndex": 10, "endColumnIndex": 11}
    blocks = sl.place(SHEET_DATES, [row[0] for row in NEW_ROWS], end=4)
    requests = sl.insert_requests(5, blocks, NEW_ROWS, grid_rows=1000, merges=[merge])
    assert [name for r in requests for name in r] == ["insertDimension", "updateCells"], \
        "no row is moved"
    insert = requests[0]["insertDimension"]
    assert insert["range"] == {"sheetId": 5, "dimension": "ROWS", "startIndex": 1, "endIndex": 3}
    assert insert["inheritFromBefore"] is False, "a data row's look, never the header's"
    assert requests[1]["updateCells"]["start"]["rowIndex"] == 1

    lower = {**merge, "startRowIndex": 2, "endRowIndex": 4}  # K3:K4: row 2 is free to move
    usual = sl.insert_requests(5, blocks, NEW_ROWS, grid_rows=1000, merges=[lower])
    assert [name for r in usual for name in r] == ["insertDimension", "moveDimension", "updateCells"]
    across = {**merge, "endRowIndex": 2, "endColumnIndex": 12}  # K2:L2, one row
    usual = sl.insert_requests(5, blocks, NEW_ROWS, grid_rows=1000, merges=[across])
    assert [name for r in usual for name in r] == ["insertDimension", "moveDimension", "updateCells"]


def test_rows_go_in_after_the_header_when_the_grid_ends_there():
    """Seen live (W8c): she deleted every empty row of an empty sheet, and
    an insert below row 2 is "larger than current grid size (1)". Tried live
    (E3): after the last row Sheets takes only an insert that inherits from
    the row before - the header - so the look it takes from it is given back."""
    blocks = sl.place(["Date"], [row[0] for row in NEW_ROWS], end=1)
    requests = sl.insert_requests(5, blocks, NEW_ROWS, grid_rows=1, merges=[])
    assert [name for r in requests for name in r] == ["insertDimension", "repeatCell", "updateCells"]
    insert = requests[0]["insertDimension"]
    assert insert["range"] == {"sheetId": 5, "dimension": "ROWS", "startIndex": 1, "endIndex": 3}
    assert insert["inheritFromBefore"] is True
    plain = requests[1]["repeatCell"]
    assert plain["range"] == {"sheetId": 5, "startRowIndex": 1, "endRowIndex": 3}
    assert plain["cell"] == {"userEnteredFormat": {}} and plain["fields"] == "userEnteredFormat"
    assert requests[2]["updateCells"]["start"]["rowIndex"] == 1

    roomy = sl.insert_requests(5, blocks, NEW_ROWS, grid_rows=2, merges=[])
    assert [name for r in roomy for name in r] == ["insertDimension", "moveDimension", "updateCells"]

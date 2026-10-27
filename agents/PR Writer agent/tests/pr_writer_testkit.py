"""Shared PR Writer test helpers: the brand-card and brief fixtures, a golden
draft, and a scripted fake model.

A uniquely named module, imported as ``from pr_writer_testkit import ...``,
so it resolves the same way whichever other suites share the pytest run. A
bare ``from conftest import`` would pick up whichever ``conftest`` module
pytest imported first (backend/tests has one too).
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_AGENTS = _HERE.parents[1]
for _root in (_AGENTS / "PR Writer agent", _AGENTS / "SEO GEO agent"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from pr_writer_agent import llm as pr_llm  # noqa: E402
from pr_writer_agent import render  # noqa: E402

FIXTURES = _HERE / "fixtures"


def load_card() -> dict:
    return json.loads((FIXTURES / "brand_card_legalsoft.json").read_text(encoding="utf-8"))


def load_briefs() -> list[dict]:
    return json.loads((FIXTURES / "briefs.json").read_text(encoding="utf-8"))["briefs"]


EVIDENCE = [
    {"id": "br-1", "origin": "brief", "claim": "Legal Soft opens its virtual records assistant program",
     "quote": "", "source_name": "intake brief", "url": "", "date": "", "third_party": False,
     "owner": "requester", "provenance": "stated"},
    {"id": "br-2", "origin": "brief", "claim": "Program opens Nov. 12, 2026", "quote": "",
     "source_name": "intake brief", "url": "", "date": "", "third_party": False,
     "owner": "requester", "provenance": "stated"},
    {"id": "bc-1", "origin": "brand_card", "claim": "Legal Soft runs a HIPAA-compliant virtual assistant program",
     "quote": "", "source_name": "legalsoft.com/partners", "url": "https://legalsoft.com/partners",
     "date": "2026-09-15", "third_party": False, "owner": "Comms Lead (fixture)", "provenance": "stated"},
    {"id": "ev-1", "origin": "web", "claim": "62% of small firms plan to add remote staff",
     "quote": "62% of small firms plan to add remote staff", "source_name": "Example Bar Survey",
     "url": "https://example.org/survey", "date": "2025-06-01", "third_party": True,
     "owner": "comms lead", "provenance": "extracted"},
    {"id": "ev-2", "origin": "web", "claim": "Old 2019 statistic", "quote": "old", "source_name": "Old report",
     "url": "https://example.org/old", "date": "2019-01-01", "third_party": True, "owner": "comms lead",
     "provenance": "extracted"},
]

SPEAKERS = [
    {"id": "sp-1", "name": "Hamid Kohan", "title": "Founder", "organization": "Legal Soft",
     "talking_points": "", "provenance": "stated", "approval_contact": "Comms Lead (fixture)"},
]

_DETAIL_SENTENCE = (
    "Each assistant follows the firm's own intake checklist, logs every request in the case file "
    "and flags gaps for the supervising attorney to review."
)
QUOTE_TEXT = (
    "Personal injury attorneys should spend their time on clients and strategy, not on chasing "
    "paperwork. Our assistants take on the records work under attorney supervision, so firms can "
    "grow without adding overhead or cutting corners on privacy."
)


def golden_press_release() -> dict:
    """A press release that passes every QA check — tests mutate a copy."""
    card = load_card()
    return {
        "content_type": "press_release",
        "brand_short_name": "Legal Soft",
        "release_line": "FOR IMMEDIATE RELEASE",
        "headline": "Legal Soft Opens Virtual Records Assistant Program to Personal Injury Firms Nationwide",
        "subhead": ("Trained assistants handle medical records requests under attorney supervision "
                    "through the company's HIPAA-compliant virtual assistant program"),
        "dateline": render.dateline(card, "2026-11-12"),
        "blocks": [
            {"id": "b1", "kind": "lede", "speaker_id": None, "history": [],
             "text": ("Legal Soft today opened its virtual records assistant program to personal injury "
                      "firms nationwide, giving attorneys trained support for medical records requests "
                      "starting Nov. 12, 2026 [br-2].")},
            {"id": "b2", "kind": "context", "speaker_id": None, "history": [],
             "text": ("Records requests slow many injury cases, and firms often lack staff to chase them. "
                      "A recent survey found 62% of small firms plan to add remote staff [ev-1].")},
            {"id": "b3", "kind": "quote", "speaker_id": "sp-1", "history": [], "text": QUOTE_TEXT},
            {"id": "b4", "kind": "detail", "speaker_id": None, "history": [],
             "text": ("The assistants work within Legal Soft's HIPAA-compliant virtual assistant program [bc-1]. "
                      + " ".join([_DETAIL_SENTENCE] * 13))},
            {"id": "b5", "kind": "cta", "speaker_id": None, "history": [],
             "text": "Firms can learn more at https://legalsoft.com/partners."},
        ],
        "speakers": copy.deepcopy(SPEAKERS),
        "boilerplates": render.boilerplates(card, None, 1),
        "media_contact": render.media_contact(card),
        "quote_status": {"b3": "pending"},
    }


class FakeInvoke:
    """Scripted replies; records every call so tests can count them."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, system, user, **kw):
        self.calls.append({"system": system, "user": user, **kw})
        if not self.replies:
            raise AssertionError("unexpected extra model call")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, pr_llm.Reply):
            return r
        text = r if isinstance(r, str) else json.dumps(r)
        return pr_llm.Reply(text=text, finish_reason="stop", model="fake", input_tokens=10, output_tokens=5)


"""Offline harness for the PR Writer (a13) router suites.

The real router, service and store run end to end over the in-memory
Firestore (``pr_memory_firestore``). The only fakes are the edges that would
cost money or reach the network: the model (``FakeModel`` answers each step's
schema by recognising its task in the system prompt), web search and page
fetch. Nothing here talks to a real database, model or website.
"""
from __future__ import annotations

import json
from typing import Any

from app.config import settings
from app.routers.tests import pr_memory_firestore
from app.services import pr_writer as svc
from pr_writer_agent import llm as pr_llm
from seo_geo_agent import insights

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com", "enabled": True}
NEWS = "Legal Soft launches virtual paralegals inside practice management systems on Nov. 12, 2026."


def person(uid: str, email: str, *, admin: bool = False) -> dict:
    return {"id": uid, "email": email, "is_admin": admin, "is_creator": False, "is_geo_editor": False,
            "is_geo_only": False, "session_id": "", "timezone": "UTC"}


OWNER = person("owner0000000000000000000000000001", "owner@legalsoft.com")
COMMS = person("comms0000000000000000000000000002", "comms@legalsoft.com")
BRANDOWNER = person("brand0000000000000000000000000003", "brandowner@legalsoft.com")
LEGAL = person("legal0000000000000000000000000004", "legal@legalsoft.com")
STRANGER = person("strgr0000000000000000000000000005", "stranger@legalsoft.com")
ADMIN = person("admin0000000000000000000000000006", "admin@legalsoft.com", admin=True)
EXEC = person("exec00000000000000000000000000007", "exec@legalsoft.com")
EVERYONE = (OWNER, COMMS, BRANDOWNER, LEGAL, STRANGER, ADMIN, EXEC)

PAGE_TEXT = ("Industry survey: 62% of small firms plan to add remote staff in 2026, according to the "
             "Example Bar Association's annual technology report.")

QUOTE = ("Firms should spend their time on clients, not paperwork. Our paralegals take on that work "
         "under attorney supervision, so firms can grow without adding overhead.")


def _field(value=None, excerpt: str = "", source: str = "sentence") -> dict:
    if value is None:
        return {"value": None, "from": "none", "excerpt": ""}
    return {"value": value, "from": source, "excerpt": excerpt}


def _items(rows: list[dict] | None = None) -> dict:
    return {"items": rows or []}


class FakeModel:
    """Answers every PR Writer model call with a schema-valid payload.

    ``complete`` makes intake find every required field (no clarify card);
    ``escalate`` makes the screen escalate; ``fail`` maps a step name to the
    exception that call raises; ``calls`` records step names in order."""

    def __init__(self) -> None:
        self.complete = True
        self.escalate = False
        self.fail: dict[str, BaseException] = {}
        self.calls: list[str] = []
        self.revise_text: str | None = None
        #: Provider cost each reply reports (``usage.cost``); ``None`` = not reported.
        self.cost_usd: float | None = None

    _STEPS = (
        ("Intake extraction.", "intake"), ("Escalation screen.", "screen"),
        ("Evidence extraction", "research_extract"), ("News angle.", "angle"),
        ("Draft. Write the full draft", "draft"), ("Line edit against QA", "qa_fix"),
        ("Fact-check extraction", "fact_check"), ("SEO block.", "seo"), ("Distribution plan.", "distribution"),
        ("Social and pitch snippets", "snippets"), ("Reviewer notes for the communications lead", "reviewer_notes"),
        ("Revise one block", "revise_block"), ("You repair malformed JSON", "repair"),
    )

    def step_of(self, system: str) -> str:
        for marker, name in self._STEPS:
            if marker in system:
                return name
        raise AssertionError("unrecognised PR Writer prompt")

    def __call__(self, system: str, user: str, *, slot: str, timeout_s: float, max_tokens: int,
                 temperature: float) -> pr_llm.Reply:
        step = self.step_of(system)
        self.calls.append(step)
        if step in self.fail:
            raise self.fail[step]
        payload = getattr(self, f"_{step}")(user)
        return pr_llm.Reply(text=json.dumps(payload), finish_reason="stop", model="fake",
                            cost_usd=self.cost_usd)

    def _intake(self, user: str) -> dict:
        full = self.complete
        return {"fields": {
            "brand": _field("Legal Soft", "Legal Soft"),
            "content_type": _field("press_release", "launches"),
            "news": _field(NEWS, NEWS),
            "why_now": _field("Live Nov. 12, 2026", "on Nov. 12, 2026") if full else _field(),
            "audience": _field("Law firm owners", "Legal Soft") if full else _field(),
            "key_facts": _items([{"fact": "virtual paralegals inside practice management systems",
                                  "source": "requester", "from": "sentence",
                                  "excerpt": "virtual paralegals inside practice management systems"}] if full else []),
            "spokespeople": _items([{"name": "Hamid Kohan", "title": "Founder", "organization": "Legal Soft",
                                     "talking_points": "", "from": "sentence", "excerpt": "Legal Soft"}]
                                   if full else []),
            "release_timing": _field({"release_date": "2026-11-12", "embargo": None}, "Nov. 12, 2026")
            if full else _field(),
            "partner_details": _field(),
            "cta": _field(),
            "target_publications": _items(), "keywords": _items(), "assets": _items(), "sensitivities": _items(),
        }, "injection_notes": []}

    def _screen(self, user: str) -> dict:
        if self.escalate:
            return {"escalate": True, "categories": ["litigation"], "reason": "Touches pending litigation.",
                    "injection_notes": []}
        return {"escalate": False, "categories": [], "reason": "A product story.", "injection_notes": []}

    def _research_extract(self, user: str) -> dict:
        return {"evidence": [{"page_id": "pg-1", "claim": "62% of small firms plan to add remote staff",
                              "quote": "62% of small firms plan to add remote staff", "date": "2026",
                              "credibility": "bar association survey"}], "injection_notes": []}

    def _angle(self, user: str) -> dict:
        return {"why_news": "New capability", "why_now": "Live Nov. 12", "who_cares": "Firm owners",
                "angle_paragraph": "Firms get paralegal capacity where they already work.",
                "audience": "legal_buyers", "newsworthiness": "strong", "weak_news_reason": None,
                "primary_keyword": "virtual paralegals", "secondary_keywords": ["legal staffing"],
                "injection_notes": []}

    def _draft(self, user: str) -> dict:
        return {
            "release_line": "FOR IMMEDIATE RELEASE",
            "headline_options": [
                {"text": "Legal Soft Puts Virtual Paralegals Inside the Practice Management Systems Firms Use",
                 "rationale": "news first"},
                {"text": "Legal Soft Brings Virtual Paralegals Into Practice Management Software", "rationale": "short"},
                {"text": "Virtual Paralegals From Legal Soft Now Work Inside Firm Systems", "rationale": "alt"}],
            "subhead_options": [{"text": "The integration goes live Nov. 12, 2026, for firms of every size."},
                                {"text": "Paralegals work in the firm's own system under attorney supervision."}],
            "blocks": [
                {"kind": "lede", "text": "Legal Soft launches virtual paralegals inside practice management "
                                         "systems on Nov. 12, 2026 [br-1].", "speaker_id": None},
                {"kind": "context", "text": "62% of small firms plan to add remote staff [ev-1].", "speaker_id": None},
                {"kind": "quote", "text": QUOTE, "speaker_id": "sp-1"},
                {"kind": "detail", "text": "The service already supports [VERIFY: customer count from sales] firms.",
                 "speaker_id": None},
                {"kind": "cta", "text": "Learn more at https://legalsoft.com.", "speaker_id": None},
            ],
            "injection_notes": [],
        }

    def _qa_fix(self, user: str) -> dict:
        return {"headline": None, "subhead": None, "blocks": [], "unfixable": [], "injection_notes": []}

    def _fact_check(self, user: str) -> dict:
        return {"claims": [
            {"block_id": "b1", "claim": "on Nov. 12, 2026", "category": "date", "evidence_ids": ["br-1"]},
            {"block_id": "b2", "claim": "62% of small firms plan to add remote staff", "category": "industry_stat",
             "evidence_ids": ["ev-1"]},
        ], "injection_notes": []}

    def _seo(self, user: str) -> dict:
        return {"seo_title": "Legal Soft virtual paralegals", "meta_description": "x" * 155,
                "slug": "legal-soft-virtual-paralegals", "internal_links": [], "external_link": None,
                "images": [], "hashtags": [], "social_summary": "Legal Soft launches virtual paralegals.",
                "injection_notes": []}

    def _distribution(self, user: str) -> dict:
        return {"channels": [{"channel": "newsroom", "why": "canonical", "outlet_examples": [],
                              "timing": "Nov. 12", "pitch_angle": "capacity", "reporter_beat": "legal tech"}],
                "wire": {"recommended": False, "circuit": "", "timing": "", "notes": ""},
                "embargo_notes": "No embargo.", "injection_notes": []}

    def _snippets(self, user: str) -> dict:
        return {"linkedin_post": "Legal Soft launches virtual paralegals.", "social_summary": "Launch.",
                "reporter_pitch": "Paralegal capacity inside firm systems.", "injection_notes": []}

    def _reviewer_notes(self, user: str) -> dict:
        return {"assumptions": ["Launch date is final"], "open_questions": ["Customer count?"], "risks": []}

    def _revise_block(self, user: str) -> dict:
        text = self.revise_text or "Legal Soft launches virtual paralegals on Nov. 12, 2026 [br-1]."
        return {"text": text, "change_summary": "Tightened the block", "new_facts_from_comment": [],
                "declined": None, "injection_notes": []}


def search(query: str) -> dict:
    return {"organic": [{"link": "https://example.org/survey"}]}


def fetch(url: str) -> dict:
    return {"url": url, "title": "Example Bar survey", "text": PAGE_TEXT}


def install(monkeypatch, *, card_reviewers: dict | None = None) -> tuple[Any, FakeModel]:
    """Memory Firestore, the users, the brand, the fakes. Returns ``(db, model)``."""
    db = pr_memory_firestore.install(monkeypatch)
    for u in EVERYONE:
        db.docs[f"users/{u['id']}"] = {"email": u["email"], "name": u["email"].split("@")[0].title(),
                                       "created_at": "2026-01-01"}
    monkeypatch.setattr(insights, "list_brands", lambda: [dict(BRAND)])
    monkeypatch.setattr(settings, "allowed_email_domains", "legalsoft.com", raising=False)
    monkeypatch.setattr(settings, "allowed_emails", "", raising=False)
    monkeypatch.setattr(settings, "admin_emails", ADMIN["email"], raising=False)
    monkeypatch.setattr(settings, "creator_emails", "", raising=False)
    monkeypatch.setattr(settings, "geo_only_emails", "", raising=False)
    model = FakeModel()
    monkeypatch.setattr(svc, "INVOKE_BASE", model)
    monkeypatch.setattr(svc, "SEARCH", search)
    monkeypatch.setattr(svc, "FETCH", fetch)
    if card_reviewers is not None:
        from app.services import firestore_repo as repo

        roles = {"comms_lead": None, "brand_owner": None, "legal": None} | card_reviewers
        repo.save_brand_card("legalsoft", {
            "reviewer_roles": roles, "reviewers": [v for v in roles.values() if v],
            "identity": {"legal_name": "Legal Soft LLC", "short_name": "Legal Soft", "website": "https://legalsoft.com",
                         "newsroom_url": "", "hq_city": "Woodland Hills", "hq_state": "California",
                         "dateline": "WOODLAND HILLS, Calif."},
            "boilerplate": {"text": "Legal Soft helps law firms grow.", "approved_by": "Comms", "approved_on": "2026-10-01"},
            "media_contact": {"name": "Priya", "title": "Comms", "email": "press@legalsoft.com", "phone": "555"},
        }, expected_revision=0, actor_id=ADMIN["id"])
    return db, model


REVIEWERS = {"comms_lead": COMMS["id"], "brand_owner": BRANDOWNER["id"], "legal": LEGAL["id"]}

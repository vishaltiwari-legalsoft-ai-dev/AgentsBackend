"""PR Writer (a13) router — the contract in docs/pr-writer-api.md, end to end.

Real router, service and store over the in-memory Firestore; only the model,
search and fetch are fakes (``pr_writer_fakes``). Covers: run creation (intake
+ screen), the step machine (order, idempotency, leases, honest failures),
clarify and escalation, versions and edits, review, sign-off and approval,
export, and the architecture rule that the agent package cannot send anything.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.routers.tests.conftest import client
from app.routers.tests.pr_writer_fakes import (
    ADMIN, BRANDOWNER, COMMS, EXEC, LEGAL, NEWS, OWNER, REVIEWERS, STRANGER, install,
)
from app.services import firestore_repo as repo
from app.services.pr_writer import RUN_ID_PREFIX
from pr_writer_agent import llm as pr_llm
from seo_geo_agent.sources import SearchUnavailable


@pytest.fixture()
def world(monkeypatch):
    return install(monkeypatch, card_reviewers=REVIEWERS)


def _as(as_caller, who):
    as_caller(dict(who))


def _create(as_caller, who=OWNER, news=NEWS, facts=""):
    _as(as_caller, who)
    r = client.post("/api/pr/runs", json={"brand_id": "legalsoft", "news": news, "facts": facts})
    assert r.status_code == 201, r.text
    return r.json()


def _drive(run: dict) -> dict:
    """What the owner's browser does: POST next_step until there is none."""
    for _ in range(10):
        if not run["next_step"]:
            return run
        r = client.post(f"/api/pr/runs/{run['id']}/steps/{run['next_step']}")
        assert r.status_code == 200, r.text
        run = r.json()
    raise AssertionError("pipeline did not settle")


def _detail(r) -> dict:
    return r.json()["detail"]


# --------------------------------------------------------------------------- #
# Creation and the step machine
# --------------------------------------------------------------------------- #

def test_create_runs_intake_and_screen_and_hands_back_research(world, as_caller):
    db, model = world
    run = _create(as_caller)
    assert run["id"].startswith("pr-")
    assert model.calls == ["intake", "screen"]
    assert [s["state"] for s in run["steps"]][:2] == ["done", "done"]
    assert run["next_step"] == "research" and run["status"] == "writing"
    assert run["viewer_role"] == "owner" and run["can"]["run_steps"]
    assert run["reviewers"]["legal"]["id"] == LEGAL["id"] and run["fallback_admins"] == []
    assert {p["user"]["id"] for p in run["participants"]} == {OWNER["id"], *REVIEWERS.values()}


def test_the_full_pipeline_builds_version_0_1_with_package_and_notes(world, as_caller):
    db, model = world
    run = _drive(_create(as_caller))
    assert run["status"] == "draft" and run["current_version"] == "0.1"
    assert [s["state"] for s in run["steps"]] == ["done"] * 6
    assert run["package"]["sections"]["seo"]["state"] == "done"
    assert run["package"]["reviewer_notes"][0]["kind"] == "assumption"
    draft = run["draft"]
    assert draft["headlines"][0]["recommended"]
    assert any(b["kind"] == "quote" and "PENDING APPROVAL" in b["text"] for b in draft["blocks"])
    assert run["markers"]["verify"] >= 1 and run["markers"]["pending_quotes"] == 1
    assert any(m["kind"] == "verify" for m in draft["markers"])
    # No request made more than two sequential generation calls: the model
    # log groups as create (2), research (2), draft (≤2), package (1 round), notes (1).
    assert model.calls[:2] == ["intake", "screen"]
    assert model.calls.count("reviewer_notes") == 1


def test_a_finished_step_replays_its_saved_result_and_never_rebills(world, as_caller):
    db, model = world
    run = _drive(_create(as_caller))
    before = len(model.calls)
    for step in ("research", "draft", "package", "notes"):
        r = client.post(f"/api/pr/runs/{run['id']}/steps/{step}")
        assert r.status_code == 200, r.text
    assert len(model.calls) == before


def test_steps_out_of_order_are_refused_with_the_next_step(world, as_caller):
    run = _create(as_caller)
    r = client.post(f"/api/pr/runs/{run['id']}/steps/package")
    assert r.status_code == 409 and _detail(r)["code"] == "step_out_of_order"
    assert _detail(r)["next_step"] == "research"
    r = client.post(f"/api/pr/runs/{run['id']}/steps/nonsense")
    assert r.status_code == 404 and _detail(r)["code"] == "step_not_found"


def test_a_running_step_turns_a_second_request_away_with_retry_after(world, as_caller):
    run = _create(as_caller)
    repo.lease_pr_step(run["id"][3:], "research", user_id=OWNER["id"], lease_s=290)
    r = client.post(f"/api/pr/runs/{run['id']}/steps/research")
    assert r.status_code == 409 and _detail(r)["code"] == "step_running"
    assert int(r.headers["Retry-After"]) >= 1
    assert client.get(f"/api/pr/runs/{run['id']}").json()["steps"][2]["state"] == "running"


def test_search_out_of_credit_is_an_honest_424_saved_on_the_step(world, as_caller, monkeypatch):
    from app.services import pr_writer as svc

    def dry(_q):
        raise SearchUnavailable("serper search failed: 402", why="the search service is out of credits")
    monkeypatch.setattr(svc, "SEARCH", dry)
    run = _create(as_caller)
    r = client.post(f"/api/pr/runs/{run['id']}/steps/research")
    assert r.status_code == 424 and _detail(r)["code"] == "search_unavailable"
    assert _detail(r)["step"] == "research"
    again = client.get(f"/api/pr/runs/{run['id']}").json()
    assert again["status"] == "step_failed" and again["steps"][2]["error"]["code"] == "search_unavailable"
    assert again["draft"] is None


@pytest.mark.parametrize("exc, status, code", [
    (pr_llm.LLMUnavailable("model call failed (main slot): APIError: boom"), 424, "model_unavailable"),
    (pr_llm.LLMTruncated("cut off"), 502, "model_truncated"),
    (pr_llm.LLMRefused("declined"), 502, "model_refused"),
])
def test_model_failures_map_to_their_contract_codes_and_keep_the_research(world, as_caller, exc, status, code):
    db, model = world
    run = _create(as_caller)
    run = client.post(f"/api/pr/runs/{run['id']}/steps/research").json()
    model.fail["draft"] = exc
    r = client.post(f"/api/pr/runs/{run['id']}/steps/draft")
    assert r.status_code == status and _detail(r)["code"] == code, r.text
    assert "boom" not in r.text
    model.fail.clear()
    calls_before = model.calls.count("research_extract")
    assert client.post(f"/api/pr/runs/{run['id']}/steps/draft").status_code == 200
    assert model.calls.count("research_extract") == calls_before  # research was not re-run


def test_a_failed_screen_never_counts_as_safe(world, as_caller):
    db, model = world
    model.fail["screen"] = pr_llm.LLMOutputInvalid("garbage")
    run = _create(as_caller)
    assert run["status"] == "step_failed" and run["next_step"] == "screen"
    assert run["steps"][1]["error"]["code"] == "screen_failed"
    assert client.post(f"/api/pr/runs/{run['id']}/steps/research").status_code == 409
    model.fail.clear()
    assert client.post(f"/api/pr/runs/{run['id']}/steps/screen").json()["next_step"] == "research"


def test_a_missing_model_key_is_424_and_nothing_is_filed(world, as_caller, monkeypatch):
    from app.services import pr_writer as svc

    monkeypatch.setattr(svc, "INVOKE_BASE", None)  # the real adapter; conftest blanked the key
    _as(as_caller, OWNER)
    r = client.post("/api/pr/runs", json={"brand_id": "legalsoft", "news": NEWS})
    assert r.status_code == 424 and _detail(r)["code"] == "credential_missing"
    assert client.get("/api/pr/runs").json()["runs"] == []


def test_empty_news_and_unknown_brand_are_coded(world, as_caller):
    _as(as_caller, OWNER)
    r = client.post("/api/pr/runs", json={"brand_id": "legalsoft", "news": "  "})
    assert r.status_code == 422 and _detail(r)["code"] == "news_required"
    r = client.post("/api/pr/runs", json={"brand_id": "nope", "news": NEWS})
    assert r.status_code == 404 and _detail(r)["code"] == "brand_not_found"


def test_the_facts_box_is_the_requesters_stated_input(world, as_caller):
    db, model = world
    run = _create(as_caller, facts="We support 1,200 firms today, per the sales dashboard.")
    stored = repo.get_pr_run(run["id"][3:], OWNER["id"])
    assert all(v.get("source") != "extracted" for v in stored["brief"].values())


# --------------------------------------------------------------------------- #
# Clarify and escalation
# --------------------------------------------------------------------------- #

def test_clarify_is_asked_once_then_drafting_proceeds(world, as_caller):
    db, model = world
    model.complete = False
    run = _create(as_caller)
    assert run["status"] == "needs_details" and run["needs_you"] == "details" and run["next_step"] is None
    keys = [f["key"] for f in run["clarify"]["fields"]]
    assert "why_now" in keys and run["clarify"]["content_type"]["value"] == "press_release"
    assert client.post(f"/api/pr/runs/{run['id']}/steps/research").status_code == 409
    _as(as_caller, COMMS)
    r = client.post(f"/api/pr/runs/{run['id']}/clarify", json={"mode": "placeholders"})
    assert r.status_code == 403 and _detail(r)["code"] == "owner_only"
    _as(as_caller, OWNER)
    r = client.post(f"/api/pr/runs/{run['id']}/clarify",
                    json={"mode": "answers", "answers": {"why_now": "Live Nov. 12"}, "content_type": "product_launch"})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["clarify"]["state"] == "answered" and run["next_step"] == "research"
    assert run["content_type"] == "product_launch"
    stored = db.doc(f"pr_runs/{run['id'].removeprefix(RUN_ID_PREFIX)}")
    assert stored["content_type"] == stored["flow"]["content_type"] == "product_launch", (
        "clarify's choice lands on the run doc in the same write as the flow")
    again = client.post(f"/api/pr/runs/{run['id']}/clarify", json={"mode": "placeholders"})
    assert again.status_code == 409 and _detail(again)["code"] == "clarify_closed"


def test_an_escalation_pauses_until_the_designated_clearer_clears_it(world, as_caller):
    db, model = world
    model.escalate = True
    run = _create(as_caller)  # the classifier escalates (model.escalate)
    assert run["status"] == "escalated" and run["next_step"] is None
    assert run["escalation"]["category"] == "litigation" and run["escalation"]["excerpt"]
    assert run["escalation"]["clearer"]["user"]["id"] == LEGAL["id"]
    r = client.post(f"/api/pr/runs/{run['id']}/steps/research")
    assert r.status_code == 409 and _detail(r)["code"] == "run_paused"
    assert client.post(f"/api/pr/runs/{run['id']}/escalation/notes", json={"note": "Legal saw it"}).status_code == 200
    _as(as_caller, COMMS)  # the card names legal, so comms may pause/close but not clear (Rule 1)
    r = client.post(f"/api/pr/runs/{run['id']}/escalation/clear", json={"note": "fine"})
    assert r.status_code == 403 and _detail(r)["code"] == "not_the_clearer"
    assert client.post(f"/api/pr/runs/{run['id']}/escalation/keep-paused", json={"note": "Wait"}).status_code == 200
    _as(as_caller, LEGAL)
    r = client.post(f"/api/pr/runs/{run['id']}/escalation/clear", json={"note": ""})
    assert r.status_code == 422 and _detail(r)["code"] == "note_required"
    assert client.get(f"/api/pr/runs/{run['id']}").json()["needs_you"] == "clearance"
    r = client.post(f"/api/pr/runs/{run['id']}/escalation/clear", json={"note": "Legal reviewed the wording."})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["escalation"]["state"] == "cleared" and run["next_step"] == "research"
    assert [n["kind"] for n in run["escalation"]["notes"]] == ["requester", "keep_paused", "clear"]


def test_close_and_withdraw_are_terminal(world, as_caller):
    db, model = world
    model.escalate = True
    run = _create(as_caller)
    _as(as_caller, COMMS)
    r = client.post(f"/api/pr/runs/{run['id']}/escalation/close", json={"note": "Not this quarter."})
    assert r.status_code == 200 and r.json()["status"] == "closed"
    _as(as_caller, OWNER)
    r = client.post(f"/api/pr/runs/{run['id']}/steps/research")
    assert r.status_code == 409 and _detail(r)["code"] == "run_closed"
    second = _create(as_caller)
    r = client.post(f"/api/pr/runs/{second['id']}/escalation/withdraw", json={})
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"


# --------------------------------------------------------------------------- #
# Versions, edits, review, approval
# --------------------------------------------------------------------------- #

def _placeholder(run):
    return next(c for c in run["draft"]["claims"] if c["placeholder"])


def test_revise_writes_a_new_minor_version_and_refuses_a_stale_one(world, as_caller):
    run = _drive(_create(as_caller))
    r = client.post(f"/api/pr/runs/{run['id']}/blocks/b1/revise", json={"comment": "Tighten", "version": "0.1"})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["current_version"] == "0.2" and run["versions"][-1]["what_changed"] == "Tightened the block"
    r = client.post(f"/api/pr/runs/{run['id']}/blocks/b1/revise", json={"comment": "Again", "version": "0.1"})
    assert r.status_code == 409 and _detail(r)["code"] == "stale_version" and _detail(r)["current_version"] == "0.2"
    r = client.post(f"/api/pr/runs/{run['id']}/blocks/headline/revise", json={"comment": "x", "version": "0.2"})
    assert r.status_code == 422 and _detail(r)["code"] == "block_not_revisable"
    old = client.get(f"/api/pr/runs/{run['id']}/versions/0.1")
    assert old.status_code == 200 and old.json()["version"] == "0.1"


def test_editing_a_quote_resets_its_approval(world, as_caller):
    db, model = world
    run = _drive(_create(as_caller))
    q = run["draft"]["quotes"][0]
    _as(as_caller, COMMS)
    r = client.post(f"/api/pr/runs/{run['id']}/quotes/{q['id']}/approval",
                    json={"evidence": "Approved by email, Oct. 28", "text_hash": q["text_hash"]})
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["quotes"][0]["approval"]["state"] == "approved"
    model.revise_text = "A different quote entirely, about growth."
    r = client.post(f"/api/pr/runs/{run['id']}/blocks/{q['id']}/revise", json={"comment": "warmer", "version": "0.1"})
    run = r.json()
    assert run["versions"][-1]["quote_resets"][0]["quote_id"] == q["id"]
    assert run["draft"]["quotes"][0]["approval"]["state"] == "pending"
    assert run["draft"]["quotes"][0]["reset_in_version"] == "0.2"


def test_verify_fills_a_placeholder_and_writes_a_version(world, as_caller):
    run = _drive(_create(as_caller))
    ph = _placeholder(run)
    r = client.post(f"/api/pr/runs/{run['id']}/claims/{ph['id']}/verify", json={"version": "0.1", "note": "Sales"})
    assert r.status_code == 422 and _detail(r)["code"] == "value_required"
    r = client.post(f"/api/pr/runs/{run['id']}/claims/{ph['id']}/verify",
                    json={"version": "0.1", "note": "Sales dashboard, Oct. 28", "value": "1,200"})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["current_version"] == "0.2" and "1,200" in " ".join(b["text"] for b in run["draft"]["blocks"])
    row = next(c for c in run["draft"]["claims"] if c["text"] == "1,200")
    assert row["status"] == "verified" and row["verified"]["by"]["id"] == OWNER["id"]


def _clean_run(as_caller):
    """A run with every [VERIFY] filled, so only sign-offs stand in the way."""
    run = _drive(_create(as_caller))
    while True:
        open_rows = [c for c in run["draft"]["claims"] if c["status"] == "needs_confirmation"]
        if not open_rows:
            return run
        c = open_rows[0]
        body = {"version": run["current_version"], "note": "Confirmed by comms"}
        if c["placeholder"]:
            body["value"] = "Confirmed value"
        r = client.post(f"/api/pr/runs/{run['id']}/claims/{c['id']}/verify", json=body)
        assert r.status_code == 200, r.text
        run = r.json()


def test_review_signoff_quote_approval_and_human_approval(world, as_caller):
    run = _clean_run(as_caller)
    label = run["current_version"]
    r = client.post(f"/api/pr/runs/{run['id']}/review", json={"version": label})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "in_review" and run["current_version"].endswith(".0")
    locked = run["current_version"]
    replay = client.post(f"/api/pr/runs/{run['id']}/review", json={"version": label})
    assert replay.status_code == 200 and replay.json()["current_version"] == locked
    r = client.post(f"/api/pr/runs/{run['id']}/signoffs/legal/sign-off", json={"version": locked})
    assert r.status_code == 403 and _detail(r)["code"] == "not_your_row"
    _as(as_caller, LEGAL)
    assert client.get(f"/api/pr/runs/{run['id']}").json()["needs_you"] == "sign_off"
    r = client.post(f"/api/pr/runs/{run['id']}/signoffs/legal/ask-changes", json={"version": locked, "note": "Fix"})
    assert r.json()["status"] == "changes_asked"
    assert client.post(f"/api/pr/runs/{run['id']}/signoffs/legal/sign-off", json={"version": locked}).status_code == 200
    _as(as_caller, BRANDOWNER)
    assert client.post(f"/api/pr/runs/{run['id']}/signoffs/brand_owner/sign-off", json={"version": locked}).status_code == 200
    _as(as_caller, COMMS)
    assert client.post(f"/api/pr/runs/{run['id']}/signoffs/comms_lead/sign-off", json={"version": locked}).status_code == 200
    r = client.post(f"/api/pr/runs/{run['id']}/approve", json={"version": locked})
    assert r.status_code == 409 and _detail(r)["code"] == "approval_blocked"
    q = client.get(f"/api/pr/runs/{run['id']}").json()["draft"]["quotes"][0]
    client.post(f"/api/pr/runs/{run['id']}/quotes/{q['id']}/approval",
                json={"evidence": "Approved by email", "text_hash": q["text_hash"]})
    _as(as_caller, OWNER)
    r = client.post(f"/api/pr/runs/{run['id']}/approve", json={"version": locked})
    assert r.status_code == 403 and _detail(r)["code"] == "comms_lead_only"
    _as(as_caller, COMMS)
    r = client.post(f"/api/pr/runs/{run['id']}/approve", json={"version": locked})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "approved" and run["signoffs"]["approved"]
    r = client.post(f"/api/pr/runs/{run['id']}/blocks/b1/revise", json={"comment": "x", "version": locked})
    assert r.status_code == 409 and _detail(r)["code"] == "run_locked"


def test_a_stale_quote_hash_cannot_be_approved(world, as_caller):
    run = _drive(_create(as_caller))
    q = run["draft"]["quotes"][0]
    _as(as_caller, COMMS)
    r = client.post(f"/api/pr/runs/{run['id']}/quotes/{q['id']}/approval",
                    json={"evidence": "email", "text_hash": "0" * 64})
    assert r.status_code == 409 and _detail(r)["code"] == "stale_version"


def test_signoff_needs_a_locked_version(world, as_caller):
    run = _drive(_create(as_caller))
    _as(as_caller, LEGAL)
    r = client.post(f"/api/pr/runs/{run['id']}/signoffs/legal/sign-off", json={"version": "0.1"})
    assert r.status_code == 409 and _detail(r)["code"] == "not_in_review"


def test_export_docx_and_md_carry_the_not_approved_banner(world, as_caller):
    run = _drive(_create(as_caller))
    md = client.get(f"/api/pr/runs/{run['id']}/export?format=md")
    assert md.status_code == 200 and "DRAFT — NOT APPROVED FOR RELEASE" in md.text
    assert "attachment" in md.headers["content-disposition"] and "_v0.1_" in md.headers["content-disposition"]
    docx = client.get(f"/api/pr/runs/{run['id']}/export?format=docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"
    bad = client.get(f"/api/pr/runs/{run['id']}/export?format=pdf")
    assert bad.status_code == 422 and _detail(bad)["code"] == "bad_format"


def test_people_added_by_the_owner_can_read_but_not_edit(world, as_caller):
    run = _drive(_create(as_caller))
    r = client.post(f"/api/pr/runs/{run['id']}/participants", json={"user_ids": ["no-such-user"]})
    assert r.status_code == 422 and _detail(r)["code"] == "unknown_member"
    r = client.post(f"/api/pr/runs/{run['id']}/participants", json={"user_ids": [EXEC["id"]]})
    assert r.status_code == 200
    added = next(p for p in r.json()["participants"] if p["user"]["id"] == EXEC["id"])
    assert added["roles"] == ["participant"] and added["added_by"]["id"] == OWNER["id"]
    _as(as_caller, EXEC)
    view = client.get(f"/api/pr/runs/{run['id']}").json()
    assert view["viewer_role"] == "participant" and not view["can"]["revise"]
    r = client.post(f"/api/pr/runs/{run['id']}/blocks/b1/revise", json={"comment": "x", "version": "0.1"})
    assert r.status_code == 403 and _detail(r)["code"] == "not_a_reviewer"


# --------------------------------------------------------------------------- #
# Lists, cards, members
# --------------------------------------------------------------------------- #

def test_runs_list_groups_and_brand_filter(world, as_caller):
    run = _create(as_caller)
    rows = client.get("/api/pr/runs?brand_id=legalsoft").json()["runs"]
    assert [r["id"] for r in rows] == [run["id"]] and rows[0]["is_owner"]
    _as(as_caller, COMMS)
    assert client.get("/api/pr/runs").json()["runs"][0]["is_owner"] is False


def test_brand_card_put_is_revisioned_and_role_gated(world, as_caller):
    _as(as_caller, STRANGER)
    card = client.get("/api/pr/brands/legalsoft/card").json()
    assert card["exists"] and card["revision"] == 1 and not card["can_edit"]
    body = {"base_revision": 1, "card": {"reviewers": dict(REVIEWERS)}}
    r = client.put("/api/pr/brands/legalsoft/card", json=body)
    assert r.status_code == 403 and _detail(r)["code"] == "cannot_edit_card"
    _as(as_caller, COMMS)
    card = client.get("/api/pr/brands/legalsoft/card").json()
    assert card["can_edit"] and not card["can_edit_reviewers"]
    r = client.put("/api/pr/brands/legalsoft/card", json=body | {"card": {
        "reviewers": dict(REVIEWERS), "boilerplate": {"text": "New words"}}})
    assert r.status_code == 422 and _detail(r)["code"] == "boilerplate_approval_required"
    ok = client.put("/api/pr/brands/legalsoft/card", json=body | {
        "card": {"reviewers": dict(REVIEWERS), "boilerplate": {"text": "New words"}},
        "boilerplate_approval": {"approved_by": "Hamid Kohan", "approved_on": "2026-10-28"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["revision"] == 2 and ok.json()["card"]["boilerplate"]["approved_by"] == "Hamid Kohan"
    stale = client.put("/api/pr/brands/legalsoft/card", json=body)
    assert stale.status_code == 409 and _detail(stale)["code"] == "stale_card"
    assert _detail(stale)["current"]["revision"] == 2
    assert _detail(stale)["current"]["updated_by"]["id"] == COMMS["id"]
    _as(as_caller, ADMIN)
    r = client.put("/api/pr/brands/legalsoft/card", json={"base_revision": 2, "card": {
        "reviewers": dict(REVIEWERS, comms_lead="ghost")}})
    assert r.status_code == 422 and _detail(r)["code"] == "unknown_member"


def test_a_domain_admin_or_a_comms_lead_cannot_reassign_reviewers(world, as_caller, monkeypatch):
    """Audit finding 1: whoever names the reviewers decides who reads and signs
    every new draft. Only an admin listed by exact address may change them."""
    from app.config import settings

    # Production shape: a whole domain in ADMIN_EMAILS makes every mailbox there an admin.
    monkeypatch.setattr(settings, "admin_emails", f"{ADMIN['email']},@aivirtual.com", raising=False)
    domain_admin = dict(STRANGER, email="someone@aivirtual.com", is_admin=True)
    takeover = {"comms_lead": domain_admin["id"], "brand_owner": domain_admin["id"], "legal": LEGAL["id"]}
    _as(as_caller, domain_admin)
    r = client.put("/api/pr/brands/legalsoft/card", json={"base_revision": 1, "card": {"reviewers": takeover}})
    assert r.status_code == 403 and _detail(r)["code"] == "cannot_assign_reviewers"
    _as(as_caller, COMMS)
    r = client.put("/api/pr/brands/legalsoft/card", json={"base_revision": 1, "card": {
        "reviewers": dict(REVIEWERS, brand_owner=COMMS["id"])}})
    assert r.status_code == 403 and _detail(r)["code"] == "cannot_assign_reviewers"
    r = client.put("/api/pr/brands/legalsoft/card", json={"base_revision": 1, "card": {
        "reviewers": dict(REVIEWERS, legal=COMMS["id"])}})
    assert r.status_code == 422 and _detail(r)["field"] == "reviewers.legal"
    assert client.get("/api/pr/brands/legalsoft/card").json()["revision"] == 1  # nothing saved
    _as(as_caller, ADMIN)  # named exactly: may reassign, but legal must stay a separate person
    r = client.put("/api/pr/brands/legalsoft/card", json={"base_revision": 1, "card": {
        "reviewers": dict(REVIEWERS, legal=COMMS["id"])}})
    assert r.status_code == 422 and _detail(r)["code"] == "invalid_card"
    r = client.put("/api/pr/brands/legalsoft/card", json={"base_revision": 1, "card": {
        "reviewers": dict(REVIEWERS, brand_owner=EXEC["id"])}})
    assert r.status_code == 200, r.text
    assert r.json()["card"]["reviewers"]["brand_owner"]["id"] == EXEC["id"]


def test_brands_list_and_members(world, as_caller):
    _as(as_caller, OWNER)
    brands = client.get("/api/pr/brands").json()["brands"]
    assert brands[0]["id"] == "legalsoft" and brands[0]["has_card"] and brands[0]["card_sections_total"] == 11
    members = client.get("/api/pr/members").json()["members"]
    assert {m["id"] for m in members} >= {OWNER["id"], COMMS["id"]}


def test_no_card_means_exact_address_admins_are_the_fallback_reviewers(monkeypatch, as_caller):
    install(monkeypatch)  # no card
    run = _create(as_caller)
    assert run["has_card"] is False
    assert [a["id"] for a in run["fallback_admins"]] == [ADMIN["id"]]
    rows = {r["id"]: r for r in run["signoffs"]["rows"]}
    assert rows["legal"]["fallback"] and not rows["legal"]["unassigned"]
    assert run["escalation"] is None


# --------------------------------------------------------------------------- #
# Architecture: the agent package sends nothing
# --------------------------------------------------------------------------- #

_FORBIDDEN = ("inbox_triage_agent", "app.services.canva", "app.routers.canva", "smtplib", "email.mime",
              "googleapiclient", "app.services.drive_source", "app.services.google_http")


def test_pr_writer_agent_imports_no_send_capable_module():
    package = Path(__file__).resolve().parents[3] / "agents" / "PR Writer agent" / "pr_writer_agent"
    offenders = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in _FORBIDDEN):
                    offenders.append(f"{path.name}: {name}")
    assert offenders == [], offenders
    assert len(list(package.glob("*.py"))) >= 10  # non-vacuity: the package was actually read


# --------------------------------------------------------------------------- #
# Deadline, partial package, notes
# --------------------------------------------------------------------------- #

def test_a_step_that_runs_out_of_its_deadline_is_a_504_and_is_retryable(world, as_caller, monkeypatch):
    from pr_writer_agent import pipeline

    db, model = world
    run = _create(as_caller)
    real = pipeline.Deadline
    monkeypatch.setattr(pipeline, "Deadline", lambda *a, **k: real(1.0))
    r = client.post(f"/api/pr/runs/{run['id']}/steps/research")
    assert r.status_code == 504 and _detail(r)["code"] == "model_timeout"
    assert "research_extract" not in model.calls  # the call was never started past the deadline
    monkeypatch.setattr(pipeline, "Deadline", real)
    assert client.post(f"/api/pr/runs/{run['id']}/steps/research").status_code == 200


def test_a_partial_package_keeps_what_worked_and_retries_only_what_failed(world, as_caller):
    db, model = world
    run = _create(as_caller)
    for step in ("research", "draft"):
        run = client.post(f"/api/pr/runs/{run['id']}/steps/{step}").json()
    model.fail["seo"] = pr_llm.LLMOutputInvalid("bad json")
    r = client.post(f"/api/pr/runs/{run['id']}/steps/package")
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["steps"][4]["state"] == "partial" and run["current_version"] == "0.1"
    assert run["package"]["sections"]["seo"]["state"] == "failed"
    assert run["next_step"] == "notes"  # a partial package does not hold the draft back
    model.fail.clear()
    before = {s: model.calls.count(s) for s in ("fact_check", "distribution", "snippets", "seo")}
    run = client.post(f"/api/pr/runs/{run['id']}/steps/package").json()
    assert run["package"]["sections"]["seo"]["state"] == "done"
    after = {s: model.calls.count(s) for s in before}
    assert after == before | {"seo": before["seo"] + 1}


def test_failed_reviewer_notes_leave_a_usable_draft(world, as_caller):
    db, model = world
    model.fail["reviewer_notes"] = pr_llm.LLMUnavailable("provider down")
    run = _create(as_caller)
    for step in ("research", "draft", "package"):
        run = client.post(f"/api/pr/runs/{run['id']}/steps/{step}").json()
    r = client.post(f"/api/pr/runs/{run['id']}/steps/notes")
    assert r.status_code == 424 and _detail(r)["code"] == "model_unavailable"
    run = client.get(f"/api/pr/runs/{run['id']}").json()
    assert run["status"] == "draft" and run["next_step"] is None
    assert run["package"]["sections"]["reviewer_notes"]["state"] == "failed"
    assert run["can"]["send_for_review"]



# --------------------------------------------------------------------------- #
# Security audit 2026-10-28 and staging findings
# --------------------------------------------------------------------------- #

SECRET_NEWS = NEWS + " Codename ZEBRA-QUARTZ-17."


def _trail_rows(db) -> list[dict]:
    return [v for k, v in db.docs.items()
            if k.startswith("agent_runs__a13/") or k.startswith("runs/") or k.startswith("creative_events/")]


def test_no_draft_text_reaches_the_activity_trail_but_model_spend_does(world, as_caller):
    """Finding 2: the trail is readable by every admin through the database
    panel, so it carries fixed summaries only — never news, comments or file
    names. Staging finding 7: it does carry each request's model spend."""
    import json

    db, model = world
    run = _create(as_caller, news=SECRET_NEWS)
    run = _drive(run)
    client.post(f"/api/pr/runs/{run['id']}/blocks/b1/revise",
                json={"comment": "Mention ZEBRA-COMMENT-9", "version": run["current_version"]})
    client.get(f"/api/pr/runs/{run['id']}/export?format=md")
    rows = _trail_rows(db)
    assert len(rows) >= 7  # create, 4 steps, revise, export — the trail was written
    blob = json.dumps(rows)
    for secret in ("ZEBRA-QUARTZ-17", "ZEBRA-COMMENT-9", "Codename", ".md"):
        assert secret not in blob, secret
    trail = [r for r in rows if r.get("agent_id") == "a13" and "usage" in r]
    created = next(r for r in trail if r["action"] == "run_created")
    assert created["usage"]["calls"] == 2 and created["usage"]["models"] == ["fake"]
    assert created["usage"]["cost_usd"] is None  # not reported by the adapter; never guessed
    assert set(created["usage"]) == {"calls", "input_tokens", "output_tokens", "cost_usd", "models"}


def test_clarify_answers_are_screened_like_the_brief(world, as_caller):
    """Finding 3, the exact reproduction: a harmless sentence, then a clarify
    answer announcing an acquisition and layoffs, must pause the run."""
    db, model = world
    model.complete = False
    run = _create(as_caller, news="Legal Soft launches a partnership with a practice management platform.")
    assert run["status"] == "needs_details"
    keys = [f["key"] for f in run["clarify"]["fields"]]
    assert "key_facts" in keys
    r = client.post(f"/api/pr/runs/{run['id']}/clarify", json={"mode": "answers", "answers": {
        "key_facts": "Legal Soft was acquired by Acme Corp and 120 staff were laid off last week."}})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "escalated" and run["next_step"] is None
    assert run["escalation"]["category"] in ("m_and_a", "layoffs")
    assert "acquired by Acme" in run["escalation"]["excerpt"]
    for step in ("research", "draft"):
        r = client.post(f"/api/pr/runs/{run['id']}/steps/{step}")
        assert r.status_code == 409 and _detail(r)["code"] in ("run_paused", "step_out_of_order"), step
    assert _detail(client.post(f"/api/pr/runs/{run['id']}/steps/research"))["code"] == "run_paused"
    _as(as_caller, LEGAL)
    cleared = client.post(f"/api/pr/runs/{run['id']}/escalation/clear", json={"note": "Legal approved the wording."})
    assert cleared.status_code == 200 and cleared.json()["next_step"] == "research"


def test_a_clarify_screen_that_cannot_run_saves_nothing(world, as_caller):
    db, model = world
    model.complete = False
    run = _create(as_caller)
    model.fail["screen"] = pr_llm.LLMOutputInvalid("garbage")
    r = client.post(f"/api/pr/runs/{run['id']}/clarify", json={"mode": "answers", "answers": {
        "why_now": "Our audit finding is resolved."}})
    assert r.status_code == 502 and _detail(r)["code"] == "screen_failed"
    again = client.get(f"/api/pr/runs/{run['id']}").json()
    assert again["clarify"]["state"] == "open" and again["status"] == "needs_details"


def test_revise_refuses_at_the_version_cap_before_paying_for_a_call(world, as_caller, monkeypatch):
    db, model = world
    run = _drive(_create(as_caller))
    monkeypatch.setattr(repo, "PR_VERSIONS_CAP", 1)
    r = client.post(f"/api/pr/runs/{run['id']}/blocks/b1/revise", json={"comment": "x", "version": "0.1"})
    assert r.status_code == 413 and _detail(r)["code"] == "cap_reached"
    assert "revise_block" not in model.calls


def test_parallel_revisions_do_not_each_pay_for_a_call(world, as_caller):
    db, model = world
    run = _drive(_create(as_caller))
    repo.lease_pr_step(run["id"][3:], "revise", user_id=COMMS["id"], lease_s=290)  # a revise in flight
    r = client.post(f"/api/pr/runs/{run['id']}/blocks/b1/revise", json={"comment": "x", "version": "0.1"})
    assert r.status_code == 409 and _detail(r)["code"] == "revision_running"
    assert int(r.headers["Retry-After"]) >= 1 and "revise_block" not in model.calls
    repo.finish_pr_step(run["id"][3:], "revise", user_id=COMMS["id"], state="pending")
    ok = client.post(f"/api/pr/runs/{run['id']}/blocks/b1/revise", json={"comment": "x", "version": "0.1"})
    assert ok.status_code == 200, ok.text
    again = client.post(f"/api/pr/runs/{run['id']}/blocks/b1/revise", json={"comment": "y", "version": "0.2"})
    assert again.status_code == 200, again.text  # the lease was released after the first


def test_identical_placeholders_get_distinct_stable_claim_ids(world, as_caller):
    db, model = world
    base = model._draft

    def twin_placeholders(user):
        payload = base(user)
        payload["blocks"][3]["text"] = ("Led by [VERIFY: exact title] and [VERIFY: exact title], "
                                        "the team supports [VERIFY: customer count from sales] firms.")
        return payload
    model._draft = twin_placeholders
    run = _drive(_create(as_caller))
    twins = [c for c in run["draft"]["claims"] if c["text"] == "[VERIFY: exact title]"]
    assert len(twins) == 2 and twins[0]["id"] != twins[1]["id"]
    ids = [c["id"] for c in run["draft"]["claims"]]
    assert len(ids) == len(set(ids))
    markers = [m for m in run["draft"]["markers"] if m["token"] == "[VERIFY: exact title]"]
    assert {m["claim_id"] for m in markers} == {twins[0]["id"], twins[1]["id"]}
    r = client.post(f"/api/pr/runs/{run['id']}/claims/{twins[0]['id']}/verify",
                    json={"version": "0.1", "note": "HR", "value": "Chief Operating Officer"})
    assert r.status_code == 200, r.text
    after = r.json()
    # Identical placeholders are one missing fact printed twice (e.g. a speaker's
    # title in the attribution and in the approval marker): filling one fills both.
    assert not [c for c in after["draft"]["claims"] if c["text"] == "[VERIFY: exact title]"]
    filled = [c for c in after["draft"]["claims"] if c["text"] == "Chief Operating Officer"]
    assert len(filled) == 1 and filled[0]["status"] == "verified" and filled[0]["id"] == twins[0]["id"]
    other = next(c for c in run["draft"]["claims"] if c["text"] == "[VERIFY: customer count from sales]")
    assert other["id"] in {c["id"] for c in after["draft"]["claims"]}  # untouched rows keep their ids
    # Reading the old version again gives the same ids (stable across reads).
    old = client.get(f"/api/pr/runs/{run['id']}/versions/0.1").json()
    assert [c["id"] for c in old["claims"]] == ids

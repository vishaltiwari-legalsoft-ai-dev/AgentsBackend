"""PR Writer (a13) persistence in ``firestore_repo``: brand cards, runs,
versions, events, sign-offs.

Runs against an in-memory Firestore (subcollections, ``array_contains``,
multi-field ordering, cursors, create-only writes) patched over
``firestore_repo._db`` / ``_transact`` — the offline guard in
``backend/conftest.py`` stays in force and nothing here can reach a real
database. Transaction writes are queued and land only when the callback
returns, so a raise mid-transaction writes nothing, as in Firestore.
"""
from __future__ import annotations

import inspect

import pytest

from app.routers.tests import pr_memory_firestore
from app.routers.tests.pr_memory_firestore import _copy, _Ref, _Snap
from app.services import firestore_repo as repo

OWNER, REVIEWER, OUTSIDER, ADMIN, EXEC = (
    "a" * 32, "b" * 32, "c" * 32, "d" * 32, "e" * 32)


# --------------------------------------------------------------------------- #
# In-memory Firestore (shared with the router suites)
# --------------------------------------------------------------------------- #

@pytest.fixture()
def db(monkeypatch):
    return pr_memory_firestore.install(monkeypatch)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _card(db, reviewers=(REVIEWER,)):
    return repo.save_brand_card("legalsoft", {"legal_name": "Legal Soft", "reviewers": list(reviewers)},
                                expected_revision=0, actor_id=OWNER)


def _run(db, **kw):
    args = dict(owner_id=OWNER, brand_id="legalsoft", content_type="press_release",
                title="Legal Soft integrates with a practice management platform",
                brief={"news": {"value": "integration", "source": "stated"}})
    return repo.create_pr_run(**(args | kw))


def _quote(qid="q1", text="We built this for firms that want to grow.", speaker="Hamid Kohan", title="Founder"):
    return {"quote_id": qid, "speaker": speaker, "title": title, "text": text}


def _version(run_id, version, *, facts=None, package=None, quotes=None, user=OWNER):
    return repo.create_pr_version(
        run_id, user_id=user, version=version,
        package=package if package is not None else {"draft": "WOODLAND HILLS, Calif. — ..."},
        fact_checks=facts if facts is not None else [{"claim": "500 firms", "status": "Verified"}],
        qa={"checks": []}, changes=["first draft"] if version == 1 else ["edit"],
        quotes=quotes if quotes is not None else [_quote()])


def _ready_for_final(run_id, version):
    run = repo.get_pr_run(run_id, OWNER)
    for q in run["quotes"]:
        repo.attest_pr_quote(run_id, q["quote_id"], actor_id=REVIEWER, text_hash=q["text_hash"],
                             note="approved by email")
    for role in repo.PR_REVIEW_ROLES:
        repo.record_pr_signoff(run_id, role=role, actor_id=REVIEWER, version=version)


# --------------------------------------------------------------------------- #
# Brand cards — revision conflict + append-only history
# --------------------------------------------------------------------------- #

def test_brand_card_revisions_advance_and_keep_append_only_history(db):
    first = _card(db)
    assert first["revision"] == 1 and first["card"]["reviewers"] == [REVIEWER]
    second = repo.save_brand_card("legalsoft", {"legal_name": "Legal Soft Inc."},
                                  expected_revision=1, actor_id=REVIEWER)
    assert second["revision"] == 2 and second["updated_by"] == REVIEWER
    history = repo.list_brand_card_history("legalsoft")
    assert [h["revision"] for h in history] == [1]
    assert history[0]["card"]["legal_name"] == "Legal Soft" and history[0]["superseded_by"] == REVIEWER
    assert repo.get_brand_card("legalsoft")["card"]["legal_name"] == "Legal Soft Inc."


def test_a_stale_brand_card_revision_is_refused_never_merged(db):
    _card(db)
    repo.save_brand_card("legalsoft", {"legal_name": "B"}, expected_revision=1, actor_id=OWNER)
    before = _copy(db.docs)
    with pytest.raises(repo.BrandCardConflict) as exc:
        repo.save_brand_card("legalsoft", {"legal_name": "C", "mission": "x"},
                             expected_revision=1, actor_id=REVIEWER)
    assert exc.value.current_revision == 2 and exc.value.expected_revision == 1
    assert db.docs == before, "a refused write must leave the card and its history untouched"
    with pytest.raises(repo.BrandCardConflict):
        repo.save_brand_card("legalsoft", {}, expected_revision=0, actor_id=OWNER)  # create over existing


@pytest.mark.parametrize("brand_id", ["", "Legal Soft", "a/b", "../x", "x" * 101])
def test_brand_card_ids_are_seo_geo_slugs_only(db, brand_id):
    with pytest.raises(ValueError):
        repo.save_brand_card(brand_id, {}, expected_revision=0, actor_id=OWNER)


# --------------------------------------------------------------------------- #
# Participant scoping
# --------------------------------------------------------------------------- #

def test_participants_are_owner_plus_card_reviewers_and_admins_are_not_in(db):
    _card(db)
    run = _run(db, participants=[EXEC])
    assert run["participants"] == [OWNER, REVIEWER, EXEC]
    assert run["brand_card_revision"] == 1
    assert repo.get_pr_run(run["id"], OWNER)["id"] == run["id"]
    assert repo.get_pr_run(run["id"], REVIEWER) is not None
    assert repo.get_pr_run(run["id"], OUTSIDER) is None
    assert repo.get_pr_run(run["id"], ADMIN) is None, "admins do not see drafts by default"
    assert repo.get_pr_run(run["id"], "") is None
    assert repo.get_pr_run("not-a-run-id", OWNER) is None


def test_reviewers_added_to_the_card_later_do_not_join_existing_runs(db):
    _card(db, reviewers=())
    run = _run(db)
    repo.save_brand_card("legalsoft", {"reviewers": [REVIEWER]}, expected_revision=1, actor_id=OWNER)
    assert repo.get_pr_run(run["id"], REVIEWER) is None
    repo.add_pr_participant(run["id"], actor_id=OWNER, user_id=REVIEWER)
    assert repo.get_pr_run(run["id"], REVIEWER) is not None


def test_a_non_participant_cannot_read_or_write_anything_on_the_run(db):
    _card(db)
    run = _run(db)
    _version(run["id"], 1)
    rid = run["id"]
    assert repo.get_pr_version(rid, 1, user_id=OUTSIDER) is None
    assert repo.list_pr_versions(rid, user_id=OUTSIDER) is None
    assert repo.list_pr_events(rid, user_id=OUTSIDER) is None
    assert repo.get_pr_step_output(rid, "research", user_id=OUTSIDER) is None
    before = _copy(db.docs)
    for call in (
        lambda: repo.add_pr_comment(rid, actor_id=OUTSIDER, text="hi"),
        lambda: repo.record_pr_signoff(rid, role="editorial", actor_id=OUTSIDER, version=1),
        lambda: repo.attest_pr_quote(rid, "q1", actor_id=OUTSIDER, text_hash="x"),
        lambda: repo.add_pr_participant(rid, actor_id=OUTSIDER, user_id=OUTSIDER),
        lambda: repo.complete_pr_step(rid, "research", user_id=OUTSIDER, output={}),
        lambda: _version(rid, 2, user=OUTSIDER),
    ):
        with pytest.raises(repo.PrNotFound):
            call()
    assert db.docs == before


def test_list_is_one_participant_query_newest_first_and_paginates(db, monkeypatch):
    _card(db, reviewers=())
    stamps = iter(f"2026-10-27T10:00:{s:02d}+00:00" for s in range(60))
    monkeypatch.setattr(repo, "_now", lambda: next(stamps))
    ids = [_run(db, title=f"news {i}")["id"] for i in range(5)]
    _run(db, owner_id=OUTSIDER)  # someone else's run never shows

    page1 = repo.list_pr_runs(OWNER, limit=2)
    page2 = repo.list_pr_runs(OWNER, limit=2, cursor=page1["next_cursor"])
    page3 = repo.list_pr_runs(OWNER, limit=2, cursor=page2["next_cursor"])
    got = [r["id"] for p in (page1, page2, page3) for r in p["runs"]]
    assert got == list(reversed(ids))
    assert page3["next_cursor"] is None
    assert set(page1["runs"][0]) == set(repo._PR_LIST_FIELDS) | {"id"}, "list rows are projected"
    col, filters, orders = db.streams[0]
    assert col == "pr_runs" and filters == (("participants", "array_contains", OWNER),)
    assert orders == (("updated_at", True), ("__name__", True))
    assert repo.list_pr_runs(OUTSIDER)["runs"][0]["user_id"] == OUTSIDER
    assert repo.list_pr_runs(ADMIN) == {"runs": [], "next_cursor": None}


def test_list_falls_back_to_the_unordered_read_until_the_index_is_built(db, monkeypatch):
    _card(db, reviewers=())
    stamps = iter(f"2026-10-27T11:00:{s:02d}+00:00" for s in range(60))
    monkeypatch.setattr(repo, "_now", lambda: next(stamps))
    ids = [_run(db, title=f"news {i}")["id"] for i in range(3)]
    db.composite_missing = True
    page1 = repo.list_pr_runs(OWNER, limit=2)
    page2 = repo.list_pr_runs(OWNER, limit=2, cursor=page1["next_cursor"])
    assert [r["id"] for r in page1["runs"] + page2["runs"]] == list(reversed(ids))


def test_a_tampered_cursor_is_a_422_not_a_scan(db):
    with pytest.raises(ValueError):
        repo.list_pr_runs(OWNER, cursor="not-a-cursor")


# --------------------------------------------------------------------------- #
# Versions — create-only
# --------------------------------------------------------------------------- #

def test_versions_are_create_only_and_strictly_sequential(db):
    _card(db)
    rid = _run(db)["id"]
    v1 = _version(rid, 1)
    assert v1["version"] == 1 and v1["label"] == "v1"
    stored = _copy(db.doc(f"pr_runs/{rid}/versions/0001"))
    with pytest.raises(repo.PrStaleVersion):
        _version(rid, 1, package={"draft": "overwrite attempt"})
    with pytest.raises(repo.PrStaleVersion):
        _version(rid, 3)
    # Even if the run's pointer were rolled back, the doc itself refuses an overwrite.
    db.docs[f"pr_runs/{rid}"]["latest_version"] = 0
    with pytest.raises(repo.PrAlreadyExists):
        _version(rid, 1, package={"draft": "overwrite attempt"})
    assert db.doc(f"pr_runs/{rid}/versions/0001") == stored


def test_a_create_that_loses_the_race_at_commit_is_a_version_conflict(db, monkeypatch):
    _card(db)
    rid = _run(db)["id"]
    real_get = _Ref.get

    def blind_get(self, transaction=None, **kw):  # the read misses a racer's write
        snap = real_get(self, transaction=transaction, **kw)
        if self.path.endswith("/versions/0001"):
            db.docs[self.path] = {"version": 1, "by": "racer"}
            return _Snap(self, None)
        return snap
    monkeypatch.setattr(_Ref, "get", blind_get)
    with pytest.raises(repo.PrAlreadyExists):
        _version(rid, 1)
    assert db.doc(f"pr_runs/{rid}/versions/0001") == {"version": 1, "by": "racer"}


def test_open_verify_count_is_computed_never_trusted(db):
    _card(db)
    rid = _run(db)["id"]
    _version(rid, 1, facts=[{"claim": "a", "status": "Verified"},
                            {"claim": "b", "status": "Needs confirmation"},
                            {"claim": "c", "status": "probably fine"}])
    assert repo.get_pr_run(rid, OWNER)["open_verify_count"] == 2, "unknown status fails closed"
    _version(rid, 2, facts=[{"claim": "a", "status": "verified"}],
             package={"draft": "x [VERIFY: customer count from ops] y [VERIFY: date]"})
    assert repo.get_pr_run(rid, OWNER)["open_verify_count"] == 2, "markers in the draft still count"
    same = "x [VERIFY: date] y"
    assert repo.count_open_verify({"markdown": same, "blocks": [{"text": same}]}, []) == 1, (
        "the same draft carried twice in a package is one flag, not two")
    assert repo.count_open_verify({"draft": "broken [VERIFY marker"}, []) == 1


# --------------------------------------------------------------------------- #
# Quotes — hash-based reset
# --------------------------------------------------------------------------- #

def test_reset_changed_quotes_is_hash_based_and_deterministic():
    prev = repo.reset_changed_quotes([], [_quote("q1"), _quote("q2", text="Second."), _quote("q3", text="Third.")])
    prev = [q | {"status": "approved", "attested_by": REVIEWER, "attested_at": "t", "note": "ok"} for q in prev]
    nxt = repo.reset_changed_quotes(prev, [
        _quote("q1", text="We built   this for firms that want to grow. "),  # whitespace only
        _quote("q2", text="Second, edited."),                                 # text edit
        _quote("q3", text="Third.", title="CEO"),                             # re-attributed
        _quote("q4", text="New."),                                            # new
    ])
    by = {q["quote_id"]: q for q in nxt}
    assert by["q1"]["status"] == "approved" and by["q1"]["attested_by"] == REVIEWER
    assert by["q2"]["status"] == "pending" and by["q2"]["attested_by"] is None
    assert by["q3"]["status"] == "pending"
    assert by["q4"]["status"] == "pending"
    assert set(by) == {"q1", "q2", "q3", "q4"}, "a quote missing from the new version drops"
    assert repo.quote_text_hash("a  b") == repo.quote_text_hash(" a b ") != repo.quote_text_hash("a b.")
    with pytest.raises(ValueError):
        repo.reset_changed_quotes([], [_quote("q1"), _quote("q1")])


def test_an_edited_quote_goes_back_to_pending_on_the_next_version(db):
    _card(db)
    rid = _run(db)["id"]
    _version(rid, 1, quotes=[_quote("q1"), _quote("q2", text="Partner view.")])
    run = repo.get_pr_run(rid, OWNER)
    for q in run["quotes"]:
        repo.attest_pr_quote(rid, q["quote_id"], actor_id=REVIEWER, text_hash=q["text_hash"])
    _version(rid, 2, quotes=[_quote("q1", text="Reviewer-edited wording."), _quote("q2", text="Partner view.")])
    by = {q["quote_id"]: q for q in repo.get_pr_run(rid, OWNER)["quotes"]}
    assert by["q1"]["status"] == "pending" and by["q2"]["status"] == "approved"


def test_nobody_attests_quote_text_they_were_not_shown(db):
    _card(db)
    rid = _run(db)["id"]
    _version(rid, 1)
    with pytest.raises(repo.PrStaleVersion):
        repo.attest_pr_quote(rid, "q1", actor_id=REVIEWER, text_hash=repo.quote_text_hash("old text"))
    with pytest.raises(repo.PrActorRefused):
        repo.attest_pr_quote(rid, "q1", actor_id=repo.PR_AGENT_ACTOR,
                             text_hash=repo.get_pr_run(rid, OWNER)["quotes"][0]["text_hash"])


# --------------------------------------------------------------------------- #
# Sign-off guard
# --------------------------------------------------------------------------- #

def test_final_signoff_is_blocked_by_flags_pending_quotes_and_missing_roles(db):
    _card(db)
    rid = _run(db)["id"]
    with pytest.raises(repo.PrSignoffBlocked):
        repo.record_pr_signoff(rid, role="final", actor_id=REVIEWER, version=0)  # no version yet
    _version(rid, 1, facts=[{"claim": "x", "status": "needs confirmation"}])
    before = _copy(db.docs)
    with pytest.raises(repo.PrSignoffBlocked) as exc:
        repo.record_pr_signoff(rid, role="final", actor_id=REVIEWER, version=1)
    text = " | ".join(exc.value.blockers)
    assert "[VERIFY]" in text and "q1" in text and "editorial" in text and "compliance" in text
    assert db.docs == before, "a blocked sign-off writes nothing, not even an event"

    _ready_for_final(rid, 1)  # quotes + roles done, but the flag is still open
    with pytest.raises(repo.PrSignoffBlocked) as exc:
        repo.record_pr_signoff(rid, role="final", actor_id=REVIEWER, version=1)
    assert exc.value.blockers == ["1 [VERIFY] flag(s) still open on v1"]


def test_final_signoff_approves_and_locks_only_a_clean_current_version(db):
    _card(db)
    rid = _run(db)["id"]
    _version(rid, 1)
    _ready_for_final(rid, 1)
    _version(rid, 2)  # a new version voids the v1 sign-offs
    assert repo.get_pr_run(rid, OWNER)["signoffs"] == {}
    with pytest.raises(repo.PrStaleVersion):
        repo.record_pr_signoff(rid, role="editorial", actor_id=REVIEWER, version=1)
    _ready_for_final(rid, 2)
    run = repo.record_pr_signoff(rid, role="final", actor_id=REVIEWER, version=2)
    assert run["status"] == "approved" and run["approved_version"] == 2 and run["approved_by"] == REVIEWER
    with pytest.raises(repo.PrRunLocked):
        _version(rid, 3)
    with pytest.raises(repo.PrRunLocked):
        repo.complete_pr_step(rid, "draft", user_id=OWNER, output={})
    kinds = [e["kind"] for e in repo.list_pr_events(rid, user_id=OWNER)]
    assert kinds[0] == "run_created" and kinds[-1] == "signoff"
    assert [e["seq"] for e in repo.list_pr_events(rid, user_id=OWNER)] == list(range(1, len(kinds) + 1))


def test_a_rejection_is_recorded_and_moves_the_run_to_changes_requested(db):
    _card(db)
    rid = _run(db)["id"]
    _version(rid, 1)
    run = repo.record_pr_signoff(rid, role="compliance", actor_id=REVIEWER, version=1,
                                 decision="reject", note="no outcome promises")
    assert run["status"] == "changes_requested"
    assert repo.list_pr_events(rid, user_id=OWNER)[-1]["kind"] == "rejection"


@pytest.mark.parametrize("actor", ["", None, "agent:a13", "a13", "system", "cron:pr", "AGENT:x"])
def test_no_signoff_without_a_human_actor(db, actor):
    _card(db)
    rid = _run(db)["id"]
    _version(rid, 1)
    with pytest.raises(repo.PrActorRefused):
        repo.record_pr_signoff(rid, role="editorial", actor_id=actor, version=1)


def test_the_pipeline_write_paths_cannot_approve_anything(db):
    _card(db)
    rid = _run(db)["id"]
    for patch in ({"status": "approved"}, {"status": "changes_requested"}, {"signoffs": {"final": {}}},
                  {"quotes": []}, {"participants": [OUTSIDER]}, {"escalation": {"cleared_by": "x"}}):
        with pytest.raises(ValueError):
            repo.complete_pr_step(rid, "intake", user_id=OWNER, patch=patch)


def test_only_record_pr_signoff_writes_the_approved_status():
    """Static: no other function in the store even names the approved status."""
    writers = {
        name for name, fn in inspect.getmembers(repo, inspect.isfunction)
        if fn.__module__ == repo.__name__ and "_RUN_APPROVED" in inspect.getsource(fn)
    }
    assert writers <= {"record_pr_signoff", "_pr_refuse_locked"}, writers


# --------------------------------------------------------------------------- #
# Steps, brief, evidence, escalation, events
# --------------------------------------------------------------------------- #

def test_a_finished_step_is_never_rerun_or_rewritten(db):
    _card(db)
    rid = _run(db)["id"]
    run, wrote = repo.complete_pr_step(rid, "research", user_id=OWNER, output={"q": 1},
                                       patch={"evidence": [{"claim": "a", "url": "https://x"}]})
    assert wrote and repo.pr_step_done(run, "research")
    run, wrote = repo.complete_pr_step(rid, "research", user_id=OWNER, output={"q": 2},
                                       patch={"evidence": []})
    assert not wrote and len(run["evidence"]) == 1
    assert repo.get_pr_step_output(rid, "research", user_id=OWNER)["output"] == {"q": 1}
    _version(rid, 1)
    assert not repo.pr_step_done(repo.get_pr_run(rid, OWNER), "research"), "a new round re-opens steps"


def test_create_version_closes_its_step_atomically(db):
    _card(db)
    rid = _run(db)["id"]
    repo.create_pr_version(rid, user_id=OWNER, version=1, package={}, fact_checks=[], qa={},
                           changes=[], quotes=[], close_step="qa")
    assert repo.get_pr_run(rid, OWNER)["steps"]["qa"]["round"] == 1


def test_a_stated_brief_field_is_never_overwritten_by_an_extracted_guess(db):
    _card(db)
    rid = _run(db, brief={"release_date": {"value": "Nov. 12", "source": "stated"},
                          "audience": {"value": "", "source": "extracted"}})["id"]
    run = repo.get_pr_run(rid, OWNER)
    assert run["brief"]["audience"] == {"value": None, "source": "missing"}
    run, _ = repo.complete_pr_step(rid, "intake", user_id=OWNER, patch={"brief": {
        "release_date": {"value": "Nov. 15", "source": "extracted"},
        "audience": {"value": "PI firm owners", "source": "extracted"}}})
    assert run["brief"]["release_date"] == {"value": "Nov. 12", "source": "stated"}
    assert run["brief"]["audience"]["value"] == "PI firm owners"


def test_evidence_is_capped_and_the_drop_is_counted(db):
    _card(db)
    rid = _run(db)["id"]
    rows = [{"claim": "c" * 5000, "url": f"https://x/{i}"} for i in range(repo.PR_EVIDENCE_CAP + 15)]
    run, _ = repo.complete_pr_step(rid, "research", user_id=OWNER, patch={"evidence": rows})
    assert len(run["evidence"]) == repo.PR_EVIDENCE_CAP and run["evidence_dropped"] == 15
    assert len(run["evidence"][0]["claim"]) == 1000


def test_an_escalation_pauses_the_pipeline_until_a_person_clears_it(db):
    _card(db)
    rid = _run(db)["id"]
    run, _ = repo.complete_pr_step(rid, "intake", user_id=OWNER,
                                   patch={"escalation_reason": "mentions pending litigation"})
    assert run["status"] == "escalated"
    with pytest.raises(repo.PrConflict):
        repo.complete_pr_step(rid, "research", user_id=OWNER)
    with pytest.raises(repo.PrActorRefused):
        repo.clear_pr_escalation(rid, actor_id=repo.PR_AGENT_ACTOR, note="ok")
    repo.clear_pr_escalation(rid, actor_id=REVIEWER, note="legal cleared 10-28")
    _, wrote = repo.complete_pr_step(rid, "research", user_id=OWNER)
    assert wrote


def test_events_are_append_only_with_a_gapless_sequence(db):
    _card(db)
    rid = _run(db)["id"]
    repo.add_pr_comment(rid, actor_id=REVIEWER, text="tighten the lede")
    repo.add_pr_comment(rid, actor_id=OWNER, text="done")
    events = repo.list_pr_events(rid, user_id=OWNER)
    assert [(e["seq"], e["kind"]) for e in events] == [(1, "run_created"), (2, "comment"), (3, "comment")]
    assert repo.list_pr_events(rid, user_id=OWNER, after_seq=2)[0]["text"] == "done"
    db.docs[f"pr_runs/{rid}"]["event_count"] = 1  # a writer that lost track of the count
    with pytest.raises(repo.PrAlreadyExists):      # create-only: event 2 is never overwritten
        repo.add_pr_comment(rid, actor_id=OWNER, text="overwrite?")
    assert db.doc(f"pr_runs/{rid}/events/000002")["text"] == "tighten the lede"


# --------------------------------------------------------------------------- #
# The router's flow state: step leases, flow writes, batched reads
# --------------------------------------------------------------------------- #

def test_a_step_lease_admits_one_request_and_turns_the_second_away(db):
    rid = _run(db)["id"]
    first = repo.lease_pr_step(rid, "research", user_id=OWNER, lease_s=290)
    second = repo.lease_pr_step(rid, "research", user_id=OWNER, lease_s=290)
    assert first["acquired"] and not first["done"]
    assert not second["acquired"] and not second["done"]
    assert 1 <= second["retry_after_s"] <= 291
    entry = db.doc(f"pr_runs/{rid}")["flow"]["steps"]["research"]
    assert entry["state"] == "running" and entry["holder"] == OWNER


def test_an_expired_lease_is_taken_over(db):
    rid = _run(db)["id"]
    repo.lease_pr_step(rid, "draft", user_id=OWNER, lease_s=290)
    db.docs[f"pr_runs/{rid}"]["flow"]["steps"]["draft"]["lease_until"] = "2000-01-01T00:00:00+00:00"
    assert repo.lease_pr_step(rid, "draft", user_id=OWNER, lease_s=290)["acquired"]


def test_a_finished_step_is_reported_done_and_never_leased_again(db):
    rid = _run(db)["id"]
    repo.lease_pr_step(rid, "draft", user_id=OWNER, lease_s=290)
    repo.finish_pr_step(rid, "draft", user_id=OWNER, state="done", result={"words": 500},
                        flow_patch={"headline": "H"})
    again = repo.lease_pr_step(rid, "draft", user_id=OWNER, lease_s=290)
    assert again["done"] and not again["acquired"]
    flow = db.doc(f"pr_runs/{rid}")["flow"]
    assert flow["steps"]["draft"]["state"] == "done" and flow["steps"]["draft"]["lease_until"] is None
    assert flow["headline"] == "H"


def test_a_failed_step_keeps_its_error_and_is_leasable_again(db):
    rid = _run(db)["id"]
    repo.lease_pr_step(rid, "research", user_id=OWNER, lease_s=290)
    repo.finish_pr_step(rid, "research", user_id=OWNER, state="failed",
                        error={"code": "search_unavailable", "message": "out of credit"})
    assert db.doc(f"pr_runs/{rid}")["flow"]["steps"]["research"]["error"]["code"] == "search_unavailable"
    retry = repo.lease_pr_step(rid, "research", user_id=OWNER, lease_s=290)
    assert retry["acquired"]
    assert db.doc(f"pr_runs/{rid}")["flow"]["steps"]["research"]["error"] is None


def test_flow_writes_are_participant_scoped_and_cannot_touch_the_step_table(db):
    rid = _run(db)["id"]
    with pytest.raises(repo.PrNotFound):
        repo.lease_pr_step(rid, "draft", user_id=OUTSIDER, lease_s=290)
    with pytest.raises(repo.PrNotFound):
        repo.update_pr_flow(rid, actor_id=OUTSIDER, mutate=lambda run, flow: flow)
    with pytest.raises(ValueError):
        repo.finish_pr_step(rid, "draft", user_id=OWNER, state="done", flow_patch={"steps": {}})


def test_update_pr_flow_needs_a_person_and_writes_only_flow(db, monkeypatch):
    # ``updated_at`` is asserted to move, so the store's clock must move between
    # the create and the update. On the real clock the two land in one tick
    # (CPython 3.12 on Windows: datetime.now steps ~1 ms; both writes take less)
    # and the test then measures the OS timer, not the write. Step the seam.
    stamps = (f"2026-10-28T00:00:00.{n:06d}+00:00" for n in range(1, 1000))
    monkeypatch.setattr(repo, "_now", lambda: next(stamps))

    rid = _run(db)["id"]
    before = _copy(db.doc(f"pr_runs/{rid}"))
    with pytest.raises(repo.PrActorRefused):
        repo.update_pr_flow(rid, actor_id=repo.PR_AGENT_ACTOR, mutate=lambda run, flow: flow)
    run = repo.update_pr_flow(rid, actor_id=OWNER, mutate=lambda run, flow: flow | {"note": "x"},
                              event_kind="escalation_note", event_data={"text": "x"})
    after = db.doc(f"pr_runs/{rid}")
    changed = {k for k in after if after.get(k) != before.get(k)}
    assert changed == {"flow", "updated_at", "event_count"}
    assert run["flow"]["note"] == "x"
    assert db.doc(f"pr_runs/{rid}/events/{after['event_count']:06d}")["kind"] == "escalation_note"


def test_a_refusing_mutation_writes_nothing(db):
    rid = _run(db)["id"]
    before = _copy(db.docs)

    def refuse(run, flow):
        raise repo.PrConflict("no")
    with pytest.raises(repo.PrConflict):
        repo.update_pr_flow(rid, actor_id=OWNER, mutate=refuse, event_kind="comment")
    assert db.docs == before


def test_runs_by_ids_is_one_batched_read_filtered_to_the_callers_runs(db):
    mine = _run(db)["id"]
    theirs = _run(db, owner_id=OUTSIDER)["id"]
    rows = repo.get_pr_runs_by_ids([mine, theirs, "not-an-id"], user_id=OWNER)
    assert [r["id"] for r in rows] == [mine]


# --------------------------------------------------------------------------- #
# Contracts with the AI core and the service
# --------------------------------------------------------------------------- #

def test_store_content_types_are_exactly_the_ai_core_ids():
    """The run doc must hold the real type (``thought_leadership``, not a
    press_release stand-in), so the two lists are one list."""
    from pr_writer_agent import content_types

    assert repo.PR_CONTENT_TYPES == tuple(content_types.CONTENT_TYPES)


def test_open_verify_counts_printed_content_only_under_the_services_package_split(db):
    """``app/services/pr_writer.py`` stores ``pipeline.for_store``'s printed
    content as ``package`` and QA report / notes / block history / evidence as
    ``qa``. Markers in ``qa`` are not flags in the draft and must not count;
    the same marker printed in both ``markdown`` and a block counts once."""
    from pr_writer_agent import pipeline

    printed = "Serving [VERIFY: firm count from ops] firms."
    draft = {
        "headline": "Legal Soft launches X", "markdown": printed,
        "blocks": [{"id": "b1", "kind": "body", "text": printed,
                    "history": ["old [VERIFY: launch date]"]}],
        "qa": {"issues": ["figure without [VERIFY: source]"]},
        "notes": ["[VERIFY: ask comms about pricing]"],
    }
    evidence = [{"claim": "[VERIFY: stale stat]", "url": "https://x"}]
    content, extras = pipeline.for_store(draft, evidence)
    package = {"draft": content, "kind": "draft", "locked": False, "what_changed": "First draft"}
    assert "[VERIFY" in repr(extras), "the fixture must put markers on the qa side too"
    assert repo.count_open_verify(package, []) == 1

    _card(db)
    rid = _run(db)["id"]
    repo.create_pr_version(rid, user_id=OWNER, version=1, package=package, fact_checks=[],
                           qa=extras, changes=["First draft"], quotes=[])
    assert repo.get_pr_run(rid, OWNER)["open_verify_count"] == 1
    _ready_for_final(rid, 1)
    with pytest.raises(repo.PrSignoffBlocked):
        repo.record_pr_signoff(rid, role="final", actor_id=REVIEWER, version=1)

    clean = dict(draft, markdown="Serving 500 firms.",
                 blocks=[dict(draft["blocks"][0], text="Serving 500 firms.")])
    content, extras = pipeline.for_store(clean, evidence)
    assert repo.count_open_verify({"draft": content}, []) == 0, "qa-side markers never hold a clean draft"


def test_the_pipeline_may_set_a_known_content_type_and_nothing_else(db):
    _card(db)
    rid = _run(db)["id"]
    run, _ = repo.complete_pr_step(rid, "intake", user_id=OWNER, patch={"content_type": "thought_leadership"})
    assert run["content_type"] == "thought_leadership"
    for bad in ("byline", "", None, "PRESS_RELEASE"):
        with pytest.raises(ValueError):
            repo.complete_pr_step(rid, "research", user_id=OWNER, patch={"content_type": bad})


def test_a_flow_write_carries_its_content_type_onto_the_run_doc_in_the_same_write(db):
    _card(db)
    rid = _run(db)["id"]

    def choose(ct):
        return lambda run, flow: flow | {"content_type": ct}

    run = repo.update_pr_flow(rid, actor_id=OWNER, mutate=choose("thought_leadership"))
    assert run["content_type"] == "thought_leadership"
    assert db.doc(f"pr_runs/{rid}")["content_type"] == db.doc(f"pr_runs/{rid}")["flow"]["content_type"]
    before = _copy(db.docs)
    with pytest.raises(ValueError):
        repo.update_pr_flow(rid, actor_id=OWNER, mutate=choose("byline"))
    assert db.docs == before, "an unknown type writes neither the flow nor the run"
    repo.update_pr_flow(rid, actor_id=OWNER, mutate=lambda run, flow: flow | {"content_type": None, "x": 1})
    assert db.doc(f"pr_runs/{rid}")["content_type"] == "thought_leadership", "no type named = keep the run's"


def test_a_reviewer_comment_on_a_block_is_not_an_open_flag_but_alternates_are():
    block = {"id": "b1", "kind": "body", "text": "Serving 500 firms.",
             "last_comment": "please [VERIFY: the 500] with ops"}
    assert repo.count_open_verify({"draft": {"markdown": "Serving 500 firms.", "blocks": [block]}}, []) == 0
    assert repo.count_open_verify({"blocks": [block]}, []) == 0
    alternates = {"draft": {"headline_options": ["Legal Soft adds [VERIFY: number] firms"],
                            "subhead_options": ["x [VERIFY: date]"], "blocks": [block]}}
    assert repo.count_open_verify(alternates, []) == 2



def test_flag_pr_escalation_raises_only_and_needs_a_person(db):
    rid = _run(db)["id"]
    with pytest.raises(repo.PrActorRefused):
        repo.flag_pr_escalation(rid, actor_id=repo.PR_AGENT_ACTOR, reason="x")
    with pytest.raises(repo.PrNotFound):
        repo.flag_pr_escalation(rid, actor_id=OUTSIDER, reason="x")
    run = repo.flag_pr_escalation(rid, actor_id=OWNER, reason="acquisition in clarify answers")
    assert run["status"] == "escalated" and run["escalation"]["flagged"] and not run["escalation"]["cleared_by"]
    before = _copy(db.docs)
    repo.flag_pr_escalation(rid, actor_id=OWNER, reason="again")  # already open: unchanged
    assert db.docs == before
    with pytest.raises(repo.PrConflict):  # the pipeline is paused, exactly as for a screen escalation
        repo.complete_pr_step(rid, "research", user_id=OWNER, patch={"angle": "x"})
    repo.clear_pr_escalation(rid, actor_id=REVIEWER if REVIEWER in db.doc(f"pr_runs/{rid}")["participants"]
                             else OWNER, note="Legal reviewed it")
    assert db.doc(f"pr_runs/{rid}")["escalation"]["cleared_by"]

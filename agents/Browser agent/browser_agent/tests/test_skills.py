"""Learned flows: what gets saved, what gets matched, and what gets refused."""
from __future__ import annotations

import app  # noqa: F401 - registers agent roots on sys.path
import pytest

from browser_agent import skills

USER = {"id": "u1", "email": "owner@legalsoft.com"}


@pytest.fixture(autouse=True)
def _local(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_OFFLINE", "1")
    monkeypatch.setenv("BROWSER_LOCAL_DIR", str(tmp_path / "skill_state"))


def _steps():
    """A route with no task-specific values — safe to reuse as-is."""
    return [
        {"kind": "navigate", "url": "https://mail.google.com/"},
        {"kind": "click", "expect": "Compose", "role": "button"},
    ]


def _steps_with_recipient(address="someone@x.com"):
    """A route that types a specific address — only reusable for that address."""
    return [*_steps(), {"kind": "type", "expect": "To recipients", "text": address}]


# --------------------------------------------------------------------------- #
# Distilling a run into something repeatable
# --------------------------------------------------------------------------- #

def test_only_successful_naming_steps_are_learned():
    run = {"steps": [
        {"action": {"kind": "navigate", "url": "https://x.com"}, "result": {"ok": True}},
        {"action": {"kind": "click", "expect": "Compose"}, "result": {"ok": True}},
        {"action": {"kind": "click", "expect": "Wrong"}, "result": {"ok": False}},   # failed
        {"action": {"kind": "wait"}, "result": {"ok": True}},                        # noise
        {"action": {"kind": "scroll"}, "result": {"ok": True}},                      # noise
    ]}
    learned = skills.steps_from_run(run)
    assert [s["kind"] for s in learned] == ["navigate", "click"]
    assert learned[1]["expect"] == "Compose"


def test_a_click_without_a_name_is_not_learned():
    """Saving a bare index would remember a number that means nothing next time."""
    run = {"steps": [{"action": {"kind": "click", "index": 55}, "result": {"ok": True}}]}
    assert skills.steps_from_run(run) == []


def test_saving_nothing_useful_is_refused():
    with pytest.raises(ValueError, match="no repeatable steps"):
        skills.save_skill(USER, "Empty", "goal", "x.com", [])

    with pytest.raises(ValueError):
        skills.save_skill(USER, "Junk", "goal", "x.com", [{"kind": "wait"}])


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

def test_save_then_list_and_fetch():
    skill = skills.save_skill(USER, "Send a Gmail", "send an email", "mail.google.com", _steps())
    assert skill["steps"][1]["expect"] == "Compose"

    rows = skills.list_skills(user_id="u1")
    assert rows[0]["id"] == skill["id"] and rows[0]["steps"] == 2
    assert skills.get_skill(skill["id"])["name"] == "Send a Gmail"


def test_skills_are_private_to_their_owner():
    skills.save_skill(USER, "Mine", "a task", "x.com", _steps())
    assert skills.list_skills(user_id="someone-else") == []


def test_delete_removes_from_both_doc_and_index():
    skill = skills.save_skill(USER, "Temp", "a task", "x.com", _steps())
    assert skills.delete_skill(skill["id"]) is True
    assert skills.get_skill(skill["id"]) is None
    assert skills.list_skills(user_id="u1") == []
    assert skills.delete_skill("nope") is False


def test_use_is_recorded_honestly():
    skill = skills.save_skill(USER, "Send a Gmail", "send an email", "mail.google.com", _steps())
    skills.record_use(skill["id"], ok=False)
    stored = skills.get_skill(skill["id"])
    assert stored["uses"] == 1 and stored["last_ok"] is False
    assert skills.list_skills(user_id="u1")[0]["last_ok"] is False


# --------------------------------------------------------------------------- #
# Matching — deliberately cautious
# --------------------------------------------------------------------------- #

def test_matches_a_differently_worded_version_of_the_same_task():
    skills.save_skill(USER, "Gmail hello", "send a hello email to someone",
                      "mail.google.com", _steps())
    hit = skills.find_match("please send hello email to my friend", "u1",
                            start_url="https://mail.google.com/")
    assert hit and hit["name"] == "Gmail hello"


def test_a_route_that_types_an_address_is_not_reused_for_someone_else():
    """The dangerous case: the wording is close enough to match, but the saved
    steps would type the OLD recipient. Better to think than to misfile mail."""
    skills.save_skill(USER, "Gmail hello", "send a hello email to someone@x.com",
                      "mail.google.com", _steps_with_recipient())
    hit = skills.find_match("please send hello email to my friend", "u1",
                            start_url="https://mail.google.com/")
    assert hit is None


def test_the_same_route_is_reused_when_the_address_matches():
    skills.save_skill(USER, "Gmail hello", "send a hello email to someone@x.com",
                      "mail.google.com", _steps_with_recipient())
    hit = skills.find_match("send a hello email to someone@x.com again", "u1",
                            start_url="https://mail.google.com/")
    assert hit and hit["name"] == "Gmail hello"


def test_amounts_and_reference_numbers_count_as_specific_too():
    steps = [*_steps(), {"kind": "type", "expect": "Amount", "text": "4500.00"}]
    skills.save_skill(USER, "Pay it", "pay invoice 4500.00", "x.com", steps)
    assert skills.find_match("pay invoice 9900.00", "u1") is None
    assert skills.find_match("pay invoice 4500.00 now", "u1") is not None


def test_literals_only_looks_at_typed_text():
    steps = [{"kind": "navigate", "url": "https://x.com/inbox/2024"},
             {"kind": "type", "expect": "To", "text": "a@b.com"}]
    assert skills.literals(steps) == ["a@b.com"]


def test_does_not_match_an_unrelated_task():
    skills.save_skill(USER, "Gmail hello", "send a hello email", "mail.google.com", _steps())
    assert skills.find_match("book a flight to Dubai", "u1") is None


def test_a_different_site_never_matches():
    skills.save_skill(USER, "Gmail hello", "send a hello email", "mail.google.com", _steps())
    assert skills.find_match("send a hello email", "u1", start_url="https://outlook.com/") is None


def test_another_users_skill_is_not_offered():
    skills.save_skill(USER, "Gmail hello", "send a hello email", "mail.google.com", _steps())
    assert skills.find_match("send a hello email", "u2") is None


def test_a_recently_broken_skill_loses_to_a_working_one():
    broken = skills.save_skill(USER, "Old way", "send a hello email now", "x.com", _steps())
    skills.record_use(broken["id"], ok=False)
    skills.save_skill(USER, "New way", "send a hello email now", "x.com", _steps())
    hit = skills.find_match("send a hello email now", "u1")
    assert hit["name"] == "New way"


def test_similarity_ignores_filler_words():
    assert skills.similarity("please go and open my gmail", "open gmail") > 0.4
    assert skills.similarity("open gmail", "delete a spreadsheet") == 0.0


def test_host_is_normalised():
    assert skills.host_of("https://www.mail.google.com/x") == "mail.google.com"
    assert skills.host_of("not a url") == ""

"""Tab digests: deterministic rule matching, honest empty states, fast-model binding."""
from __future__ import annotations

import app  # noqa: F401 - registers agent roots on sys.path
import pytest

from app.services import openrouter as openrouter_service
from browser_agent import digest

USER = {"id": "u1", "email": "owner@legalsoft.com"}

EVENTS = [
    {"url": "https://jira.example.com/browse/BILL-42", "title": "BILL-42 billing export fails",
     "at": "2026-08-10T09:00:00Z", "domain": "jira.example.com"},
    {"url": "https://news.ycombinator.com", "title": "Hacker News",
     "at": "2026-08-10T09:05:00Z", "domain": "news.ycombinator.com"},
]
TABS = [{"url": "https://github.com/org/repo/pull/7", "title": "Fix billing export", "active": True}]


@pytest.fixture(autouse=True)
def _local(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_OFFLINE", "1")
    monkeypatch.setenv("BROWSER_LOCAL_DIR", str(tmp_path / "browser_state"))


class _StubLLM:
    def __init__(self, reply: str):
        self._reply = reply

    def invoke(self, _messages):
        class R:
            content = self._reply

        R.content = self._reply
        return R()


_GOOD = (
    '{"headline":"Mostly chasing a billing export bug.",'
    '"themes":[{"title":"Billing bug","detail":"Jira ticket plus the fix PR."}],'
    '"open_loops":["PR #7 is still open"]}'
)


def _bind(monkeypatch, reply: str) -> list[dict]:
    seen: list[dict] = []

    def fake_get_llm(*_a, **kw):
        seen.append(kw)
        return _StubLLM(reply)

    monkeypatch.setattr(openrouter_service, "get_llm", fake_get_llm)
    return seen


# --------------------------------------------------------------------------- #
# Rule matching — plain, deterministic, no LLM
# --------------------------------------------------------------------------- #

def test_rule_matches_title_and_url():
    rules = [{"id": "r1", "text": "billing", "enabled": True}]
    alerts = digest.match_rules(EVENTS, TABS, rules)
    assert len(alerts) == 1
    assert alerts[0]["count"] == 2  # the Jira ticket and the PR tab
    assert alerts[0]["rule"] == "billing"


def test_rule_with_no_hits_produces_no_alert():
    alerts = digest.match_rules(EVENTS, TABS, [{"id": "r1", "text": "kubernetes"}])
    assert alerts == []


def test_disabled_rule_is_skipped():
    alerts = digest.match_rules(EVENTS, TABS, [{"id": "r1", "text": "billing", "enabled": False}])
    assert alerts == []


def test_duplicate_pages_counted_once():
    events = EVENTS + [dict(EVENTS[0])]  # same ticket seen twice
    alerts = digest.match_rules(events, [], [{"id": "r1", "text": "billing"}])
    assert alerts[0]["count"] == 1


# --------------------------------------------------------------------------- #
# Digest building
# --------------------------------------------------------------------------- #

def test_build_digest_uses_fast_model_and_a11(monkeypatch):
    seen = _bind(monkeypatch, _GOOD)
    result = digest.build_digest(USER, EVENTS, TABS)
    assert seen[0]["agent_id"] == "a11"
    assert seen[0]["fast"] is True
    assert result["headline"].startswith("Mostly chasing")
    assert result["themes"][0]["title"] == "Billing bug"
    assert result["pages_seen"] == 2


def test_empty_trail_says_so_without_calling_the_model(monkeypatch):
    seen = _bind(monkeypatch, _GOOD)
    result = digest.build_digest(USER, [], [])
    assert seen == []  # no LLM spend on an empty trail
    assert "Nothing to report" in result["headline"]
    assert result["themes"] == []


def test_non_web_events_are_dropped(monkeypatch):
    _bind(monkeypatch, _GOOD)
    result = digest.build_digest(USER, [{"url": "chrome://extensions", "title": "Extensions"}], [])
    assert result["pages_seen"] == 0


def test_unparseable_model_reply_raises(monkeypatch):
    _bind(monkeypatch, "I think you browsed some pages.")
    with pytest.raises(ValueError):
        digest.build_digest(USER, EVENTS, TABS)


def test_digest_is_listed_and_retrievable(monkeypatch):
    _bind(monkeypatch, _GOOD)
    result = digest.build_digest(USER, EVENTS, TABS)
    rows = digest.list_digests(user_id="u1")
    assert rows and rows[0]["id"] == result["id"]
    assert digest.get_digest(result["id"])["headline"] == result["headline"]


def test_alerts_ride_along_with_the_digest(monkeypatch):
    _bind(monkeypatch, _GOOD)
    digest.save_config({"watch_rules": [{"id": "r1", "text": "billing"}]})
    result = digest.build_digest(USER, EVENTS, TABS)
    assert result["alerts"][0]["rule"] == "billing"


# --------------------------------------------------------------------------- #
# Watch-rule config
# --------------------------------------------------------------------------- #

def test_save_config_trims_and_drops_blank_rules():
    cfg = digest.save_config({"watch_rules": [
        {"id": "r1", "text": "  billing  "},
        {"id": "r2", "text": "   "},
    ]})
    assert len(cfg["watch_rules"]) == 1
    assert cfg["watch_rules"][0]["text"] == "billing"


def test_save_config_caps_rule_count():
    many = [{"id": f"r{i}", "text": f"topic {i}"} for i in range(digest.MAX_RULES + 5)]
    cfg = digest.save_config({"watch_rules": many})
    assert len(cfg["watch_rules"]) == digest.MAX_RULES

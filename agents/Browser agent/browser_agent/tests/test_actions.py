"""Action grammar + safety policy — the boundary that protects a real browser.

The fixtures here are the SAME shapes the Chrome extension pins in
``src/protocol.test.ts``; keep them in sync when PROTOCOL changes.
"""
from __future__ import annotations

import pytest

from browser_agent import actions


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #

def test_valid_click_round_trips():
    a = actions.validate_action({"kind": "click", "index": 3, "why": "open result"})
    assert a.kind == "click" and a.index == 3


def test_click_carries_the_expected_label():
    """Indexes are re-assigned every step, so the label is what makes a click
    checkable rather than a guess at a moving number."""
    a = actions.validate_action(
        {"kind": "click", "index": 55, "expect": "To recipients", "why": "focus To"}
    )
    assert a.expect == "To recipients"


def test_done_carries_summary_and_extracted():
    a = actions.validate_action(
        {"kind": "done", "summary": "Found 3 stories", "extracted": [{"t": "x"}]}
    )
    assert a.summary == "Found 3 stories" and a.extracted == [{"t": "x"}]


def test_a_name_alone_is_enough_to_click():
    """Skills and recordings carry names, never indexes — an index saved today
    means nothing on tomorrow's page."""
    a = actions.validate_action({"kind": "click", "expect": "Compose", "why": "open it"})
    assert a.expect == "Compose" and a.index is None


def test_an_action_with_neither_name_nor_index_is_refused():
    with pytest.raises(ValueError, match="which element"):
        actions.validate_action({"kind": "click", "why": "click something"})


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "teleport"},                       # unknown kind
        {"kind": "click"},                          # says nothing about what to click
        {"kind": "type", "index": 1},               # missing text
        {"kind": "navigate"},                       # missing url
        {"kind": "select", "index": 2},             # missing value
        {"kind": "done"},                           # missing summary
        "not-a-dict",
    ],
)
def test_invalid_actions_raise(raw):
    with pytest.raises(ValueError):
        actions.validate_action(raw)


def test_unknown_kind_names_the_kind():
    with pytest.raises(ValueError, match="teleport"):
        actions.validate_action({"kind": "teleport"})


# --------------------------------------------------------------------------- #
# Domain policy
# --------------------------------------------------------------------------- #

def test_blocklist_blocks_domain_and_subdomains():
    blocked = actions.parse_domains("paypal.com")
    assert actions.check_url("https://paypal.com/pay", set(), blocked)
    assert actions.check_url("https://www.paypal.com/x", set(), blocked)
    assert actions.check_url("https://example.com", set(), blocked) is None


def test_allowlist_permits_only_listed():
    allowed = actions.parse_domains("legalsoft.com")
    assert actions.check_url("https://app.legalsoft.com/x", allowed, set()) is None
    assert actions.check_url("https://evil.com", allowed, set())


def test_non_web_urls_refused():
    assert actions.check_url("file:///etc/passwd", set(), set())
    assert actions.check_url("javascript:alert(1)", set(), set())


def test_default_blocklist_covers_banking():
    blocked = actions.parse_domains(actions.DEFAULT_BLOCKED_DOMAINS)
    assert actions.check_url("https://chase.com/login", set(), blocked)
    assert actions.check_url("https://accounts.google.com/signin", set(), blocked)


# --------------------------------------------------------------------------- #
# Sensitive-action classifier
# --------------------------------------------------------------------------- #

_ELEMENTS = [
    {"i": 1, "tag": "button", "text": "Pay now"},
    {"i": 2, "tag": "a", "text": "Read more"},
    {"i": 3, "tag": "button", "text": "Delete account"},
    {"i": 4, "tag": "button", "text": "Send message"},
]


@pytest.mark.parametrize("index,expected", [(1, True), (2, False), (3, True), (4, True)])
def test_click_sensitivity_by_label(index, expected):
    act = actions.Action(kind="click", index=index)
    assert actions.is_sensitive(act, _ELEMENTS) is expected


def test_checkout_navigation_is_sensitive():
    act = actions.Action(kind="navigate", url="https://shop.com/checkout")
    assert actions.is_sensitive(act, [])


def test_plain_navigation_not_sensitive():
    act = actions.Action(kind="navigate", url="https://news.ycombinator.com")
    assert not actions.is_sensitive(act, [])


def test_enter_on_sensitive_button_counts():
    act = actions.Action(kind="key", text="Enter", index=1)
    assert actions.is_sensitive(act, _ELEMENTS)


def test_readmore_substring_does_not_falsely_trigger():
    # "buy" must match as a word, not inside "buyer".
    els = [{"i": 9, "tag": "a", "text": "Contact the buyer"}]
    assert not actions.is_sensitive(actions.Action(kind="click", index=9), els)

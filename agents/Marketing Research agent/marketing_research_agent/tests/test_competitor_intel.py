from marketing_research_agent.modules import competitor_intel as ci


def fake_fetcher_factory(text):
    return lambda url: text


def test_snapshot_hashes_content():
    snap = ci.snapshot("Smith.ai", "https://smith.ai/", fetcher=fake_fetcher_factory("Pricing: $99"))
    assert snap.content_hash and snap.text == "Pricing: $99"


def test_diff_detects_change(monkeypatch):
    monkeypatch.setenv("MR_OFFLINE", "1")
    a = ci.snapshot("Smith.ai", "u", fetcher=fake_fetcher_factory("v1"))
    b = ci.snapshot("Smith.ai", "u", fetcher=fake_fetcher_factory("v2 changed"))
    assert ci.diff(a, b)["changed"] is True


def test_diff_no_change():
    a = ci.snapshot("Smith.ai", "u", fetcher=fake_fetcher_factory("same"))
    b = ci.snapshot("Smith.ai", "u", fetcher=fake_fetcher_factory("same"))
    assert ci.diff(a, b)["changed"] is False


def test_first_snapshot_marked_changed():
    cur = ci.snapshot("Smith.ai", "u", fetcher=fake_fetcher_factory("new"))
    assert ci.diff(None, cur)["changed"] is True

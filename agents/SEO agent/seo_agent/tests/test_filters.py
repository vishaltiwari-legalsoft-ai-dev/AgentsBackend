from seo_agent.filters import filter_entries
from seo_agent.schemas import SerpEntry


def _entry(pos: int, url: str, title: str = "t") -> SerpEntry:
    return SerpEntry(url=url, title=title, position=pos)


def test_blocklisted_domains_excluded_with_reason():
    entries = [
        _entry(1, "https://example-legal.com/intake"),
        _entry(2, "https://www.reddit.com/r/law/comments/1"),
        _entry(3, "https://www.youtube.com/watch?v=x"),
        _entry(4, "https://www.quora.com/What-is-intake"),
        _entry(5, "https://lawfirmops.com/guide"),
    ]
    kept, excluded = filter_entries(entries, top_n=10)
    assert [e.url for e in kept] == [
        "https://example-legal.com/intake", "https://lawfirmops.com/guide",
    ]
    reasons = {d["url"]: d["reason"] for d in excluded}
    assert reasons["https://www.reddit.com/r/law/comments/1"].startswith("blocklist:")
    assert len(excluded) == 3


def test_directory_url_patterns_excluded():
    entries = [
        _entry(1, "https://www.yelp.com/biz/firm"),
        _entry(2, "https://firm.example.com/blog/intake-guide"),
        _entry(3, "https://www.indeed.com/q-legal-intake-jobs.html"),
    ]
    kept, excluded = filter_entries(entries, top_n=10)
    assert [e.url for e in kept] == ["https://firm.example.com/blog/intake-guide"]
    assert len(excluded) == 2


def test_top_n_cap_applies_after_filtering():
    entries = [_entry(i, f"https://site{i}.com/a") for i in range(1, 15)]
    kept, excluded = filter_entries(entries, top_n=10)
    assert len(kept) == 10
    assert kept[0].position == 1
    overflow = [d for d in excluded if d["reason"] == "beyond-top-n"]
    assert len(overflow) == 4


def test_lookalike_domains_are_not_blocked():
    entries = [
        _entry(1, "https://amazon.computer.com/products"),
        _entry(2, "https://reddit.company.com/page"),
        _entry(3, "https://findlaw.com/lawyersearchtool"),
        _entry(4, "https://findlaw.com/lawyers/smith"),
    ]
    kept, excluded = filter_entries(entries, top_n=10)
    assert [e.url for e in kept] == [
        "https://amazon.computer.com/products",
        "https://reddit.company.com/page",
        "https://findlaw.com/lawyersearchtool",
    ]
    assert excluded == [{"url": "https://findlaw.com/lawyers/smith", "reason": "blocklist:findlaw.com/lawyers"}]

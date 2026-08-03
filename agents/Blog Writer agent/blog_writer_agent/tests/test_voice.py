"""Brand voice study: read published posts, store the profile, never invent."""
from __future__ import annotations

import pytest

from blog_writer_agent import voice

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com"}

INVENTORY = {
    "posts": [
        {"url": "https://legalsoft.com/blog/one/", "title": "One"},
        {"url": "https://legalsoft.com/blog/two/", "title": "Two"},
        {"url": "https://legalsoft.com/blog/broken/", "title": "Broken"},
    ]
}

_PROFILE = {
    "tone": "practical, direct",
    "pov": "second person",
    "dos": ["cite numbers"],
    "donts": ["marketing hype"],
    "summary": "Practical guides for law firm operators, written plainly.",
}


def _fetch(url, client=None):
    if "broken" in url:
        raise OSError("boom")
    return {"url": url, "title": f"T {url[-5:-1]}", "text": f"Body text of {url}", "status": 200}


def test_study_reads_posts_and_stores_profile():
    prompts: list[str] = []

    def fake_llm(system, prompt, **kw):
        prompts.append(prompt)
        return dict(_PROFILE)

    doc = voice.study(BRAND, INVENTORY, fetch=_fetch, llm=fake_llm)
    assert doc["count"] == 2  # broken post skipped, not fatal
    assert doc["posts_read"] == ["https://legalsoft.com/blog/one/", "https://legalsoft.com/blog/two/"]
    assert doc["profile"]["tone"] == "practical, direct"
    assert "Body text of" in prompts[0]
    assert voice.latest("legalsoft") == doc
    assert voice.latest("unknown") is None


def test_study_requires_inventory_and_readable_posts():
    with pytest.raises(ValueError):
        voice.study(BRAND, None, fetch=_fetch, llm=lambda s, p, **kw: _PROFILE)
    with pytest.raises(ValueError):
        voice.study(
            BRAND, {"posts": [{"url": "https://legalsoft.com/blog/broken/", "title": "B"}]},
            fetch=_fetch, llm=lambda s, p, **kw: _PROFILE,
        )


def test_unusable_profile_raises_instead_of_inventing():
    with pytest.raises(ValueError):
        voice.study(BRAND, INVENTORY, fetch=_fetch, llm=lambda s, p, **kw: {"summary": ""})


def test_digest_flattens_profile_for_prompts():
    text = voice.digest({"profile": _PROFILE})
    assert "tone: practical, direct" in text
    assert "donts: marketing hype" in text
    assert voice.digest(None) == ""

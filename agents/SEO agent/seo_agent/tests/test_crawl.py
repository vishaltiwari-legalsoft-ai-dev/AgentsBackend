from pathlib import Path

from seo_agent.crawl import crawl_pages, extract
from seo_agent.schemas import SerpEntry

HTML = (Path(__file__).parent / "fixtures" / "page_intake_guide.html").read_text(encoding="utf-8")


def test_extract_pulls_body_headings_and_counts():
    doc = extract("https://example-legal.com/intake", 1, HTML)
    assert doc.rank == 1
    assert doc.title == "Legal Intake Specialist: Complete Guide"
    assert "first point of contact" in doc.body_text
    assert "should not appear" not in doc.body_text          # script stripped
    assert "Home | About" not in doc.body_text               # nav stripped
    assert doc.headings == [
        "Legal Intake Specialist: Complete Guide",
        "What Does a Legal Intake Specialist Do?",
        "Salary and Hiring",
        "FAQ",
    ]
    assert doc.word_count > 30
    assert any("How much does" in t for t in doc.faq_texts)


def test_crawl_pages_records_failures_and_continues():
    entries = [
        SerpEntry(url="https://good.com/a", position=1),
        SerpEntry(url="https://down.com/b", position=2),
    ]

    def fetcher(url: str) -> str:
        if "down.com" in url:
            raise RuntimeError("connection refused")
        return HTML

    pages, failures = crawl_pages(entries, fetcher=fetcher)
    assert len(pages) == 1
    assert pages[0].url == "https://good.com/a"
    assert failures == [{"url": "https://down.com/b", "reason": "fetch-failed: connection refused"}]

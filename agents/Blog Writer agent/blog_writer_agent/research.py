"""Gap-driven deep research: fan out over source classes, read pages, bank evidence.

One call to :func:`research_step` = one round (request-safe on Cloud Run).
Round 1 fans the topic out across the five angles below; every later round
turns the previous round's *gaps* — claims still unsupported — into queries.
The run stops claiming progress the moment a round adds nothing new.

The ledger is the only thing drafting may cite: each item carries the claim,
a verbatim quote, the URL it was read from, and a credibility note.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from seo_geo_agent import sources
from seo_geo_agent.sources import CredentialMissing

from blog_writer_agent import llm as bw_llm
from blog_writer_agent import state

# Source classes every topic gets researched through. "anecdotes" is deliberate:
# forums and community threads carry the lived-experience material readers trust.
ANGLES = [
    {"key": "studies", "query_tpl": "{topic} statistics study report"},
    {"key": "experts", "query_tpl": "{topic} best practices expert guide"},
    {"key": "news", "query_tpl": "{topic} news 2026"},
    {"key": "anecdotes", "query_tpl": "{topic} reddit forum real experience"},
    {"key": "competitors", "query_tpl": "{topic} blog article"},
]
ROUND_CAP = 4          # UI offers "Go deeper" past this; the loop never auto-runs past it
# The run index is ONE Firestore document every save appends to, and Firestore
# hard-caps a document at 1 MiB. Uncapped it eventually refuses every write —
# and ``state.save`` is not best-effort here, so ``save_run`` would raise and the
# agent would stop persisting runs entirely, permanently. Per owner, not global:
# a busy colleague must not evict everyone else's runs from the only list the UI
# reads (``browser_agent.skills._capped`` solves the same problem the same way).
INDEX_CAP_PER_USER = 200
QUERIES_PER_ROUND = 6
READS_PER_ROUND = 8
MINI_READS = 3         # targeted reads for a single block revision

_EXTRACT_SYSTEM = (
    "You extract evidence from a web page for a research ledger. Return JSON "
    '{"evidence": [{"claim", "quote", "url", "source_name", "source_class", '
    '"date", "credibility"}]}. Rules: quote must be verbatim from the page; '
    "claim is one factual sentence the quote supports; source_class is one of "
    "studies|experts|news|anecdotes|competitors; date empty if the page shows "
    "none; credibility is a short honest note (peer-reviewed, vendor blog, "
    "forum anecdote...). Return an empty list when the page has no usable "
    "evidence — never invent."
)
_GAP_SYSTEM = (
    "You run gap analysis on a research ledger. Given the topic and the "
    "evidence collected so far, return JSON {\"gaps\": [..]} — each gap a "
    "concrete search query for what a rigorous article still lacks (missing "
    "numbers, counter-views, first-hand anecdotes, recent developments). "
    "Return an empty list when the ledger already covers the topic well."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run(brand: dict, topic: str, notes: str = "", user_id: str = "") -> dict:
    slug = "".join(c if c.isalnum() else "-" for c in topic.lower()).strip("-")[:32]
    run = {
        "id": f"bw-{slug}-{uuid.uuid4().hex[:6]}",
        "brand_id": brand["id"],
        "brand_name": brand.get("name", brand["id"]),
        "domain": brand.get("domain", ""),
        "topic": topic,
        "notes": notes,
        "user_id": user_id,
        "created": _now(),
        "status": "research",
        "rounds": [],
        "ledger": [],
        "gaps": [],
        "draft": None,
        "visuals": None,
    }
    save_run(run)
    return run


def save_run(run: dict) -> None:
    state.save(f"run-{run['id']}", run)
    index = state.load("runs-index") or {"runs": []}
    entry = {
        "id": run["id"],
        "brand_id": run["brand_id"],
        "brand_name": run["brand_name"],
        "topic": run["topic"],
        # Legacy runs predate ownership — an absent/empty user_id means
        # "visible to everyone", never invent one.
        "user_id": run.get("user_id", ""),
        "created": run["created"],
        "status": run["status"],
    }
    runs = [r for r in index["runs"] if r["id"] != run["id"]]
    index["runs"] = _capped([entry] + runs)
    state.save("runs-index", index)


def _capped(rows: list[dict]) -> list[dict]:
    """Trim to the newest ``INDEX_CAP_PER_USER`` rows per owner (rows are
    newest-first). Legacy runs carry no user_id and share one bucket."""
    kept_per_user: dict[str, int] = {}
    out: list[dict] = []
    for row in rows:
        owner = str(row.get("user_id") or "")
        seen = kept_per_user.get(owner, 0)
        if seen >= INDEX_CAP_PER_USER:
            continue
        kept_per_user[owner] = seen + 1
        out.append(row)
    return out


def load_run(run_id: str) -> dict | None:
    return state.load(f"run-{run_id}")


def list_runs() -> list[dict]:
    index = state.load("runs-index") or {"runs": []}
    return index["runs"]


def _default_search():
    if not sources.serper_available():
        raise CredentialMissing(
            "SEO_SERPER_API_KEY not set — deep research needs live search results"
        )
    return sources.serper_search


def _read_urls(run: dict) -> set[str]:
    return {u for r in run["rounds"] for u in r.get("read", [])}


def _dedupe_key(item: dict) -> str:
    return item.get("claim", "").strip().lower()


def _extract(page: dict, topic: str, llm) -> list[dict]:
    text = (page.get("text") or "")[:6000]
    if not text.strip():
        return []
    page_url = page.get("final_url") or page.get("url") or ""
    payload = llm(
        _EXTRACT_SYSTEM,
        f"Topic being researched: {topic}\nPage URL: {page_url}\n"
        f"Page title: {page.get('title', '')}\n\nPage text:\n{text}",
    )
    items = payload.get("evidence", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    kept = []
    for item in items:
        if not isinstance(item, dict) or not item.get("claim"):
            continue
        # Provenance is ours, never the model's: the ledger cites the page we read.
        item["url"] = page_url
        item.setdefault("source_name", page.get("title", "") or page_url)
        kept.append(item)
    return kept


def _bank(run: dict, extracted: list[dict]) -> int:
    """Dedupe against the ledger and append; returns how many were new."""
    seen = {_dedupe_key(i) for i in run["ledger"]}
    added = 0
    for item in extracted:
        key = _dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        item["id"] = f"ev-{len(run['ledger']) + 1}"
        run["ledger"].append(item)
        added += 1
    return added


def _run_queries(run: dict, queries: list[dict], reads_cap: int, *, search, fetch, llm) -> tuple[list, list, int]:
    """Search each query, read unseen hits, extract evidence. Returns (query records, read urls, added)."""
    already = _read_urls(run)
    records, candidates = [], []
    for q in queries:
        serp = search(q["q"])
        hits = [h.get("link", "") for h in serp.get("organic", []) if h.get("link")]
        records.append({"angle": q["angle"], "q": q["q"], "hits": len(hits)})
        candidates.extend(h for h in hits if h not in already and h not in candidates)

    read, extracted = [], []
    for url in candidates[:reads_cap]:
        try:
            page = fetch(url)
        except Exception:  # noqa: BLE001 — a dead page is skipped, not fatal
            continue
        page = page if isinstance(page, dict) else page.__dict__
        read.append(url)
        extracted.extend(_extract(page, run["topic"], llm))
    return records, read, _bank(run, extracted)


def research_step(run: dict, *, search=None, fetch=None, llm=None) -> dict:
    search = search or _default_search()
    fetch = fetch or sources.fetch_page  # parsed body text, not raw HTML
    llm = llm or bw_llm.llm_json

    if not run["rounds"]:
        queries = [{"angle": a["key"], "q": a["query_tpl"].format(topic=run["topic"])} for a in ANGLES]
    else:
        queries = [{"angle": "gap", "q": g} for g in run["gaps"][:QUERIES_PER_ROUND]]

    records, read, added = _run_queries(run, queries, READS_PER_ROUND, search=search, fetch=fetch, llm=llm)

    ledger_lines = "\n".join(
        f"- [{i['id']}] ({i.get('source_class', '?')}) {i['claim']}" for i in run["ledger"]
    ) or "(empty)"
    gaps_payload = llm(_GAP_SYSTEM, f"Topic: {run['topic']}\n\nLedger so far:\n{ledger_lines}")
    gaps = gaps_payload.get("gaps", []) if isinstance(gaps_payload, dict) else []
    run["gaps"] = [g for g in gaps if isinstance(g, str) and g.strip()]

    run["rounds"].append(
        {"n": len(run["rounds"]) + 1, "at": _now(), "queries": records, "read": read, "added": added, "gaps": run["gaps"]}
    )
    if added == 0:
        run["status"] = "saturated"
    elif len(run["rounds"]) >= ROUND_CAP:
        run["status"] = "capped"
    else:
        run["status"] = "research"
    save_run(run)
    return run


def mini_research(run: dict, queries: list[str], *, search=None, fetch=None, llm=None) -> list[dict]:
    """Targeted evidence for one block revision; appends to the ledger."""
    search = search or _default_search()
    fetch = fetch or sources.fetch_page  # parsed body text, not raw HTML
    llm = llm or bw_llm.llm_json

    before = len(run["ledger"])
    _run_queries(
        run,
        [{"angle": "targeted", "q": q} for q in queries[:MINI_READS]],
        MINI_READS,
        search=search, fetch=fetch, llm=llm,
    )
    save_run(run)
    return run["ledger"][before:]

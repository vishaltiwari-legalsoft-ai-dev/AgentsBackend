"""Stage 1 — team steps 1-4: SERP intel, competitor classification, keyword gap,
usage benchmarks, LSI. Output: the Keyword Target Sheet (Gate 1)."""
from __future__ import annotations

import re
from collections import Counter

from seo_geo_agent import sources
from seo_geo_agent.sources import CredentialMissing

from . import rules

_STOP = frozenset(
    "a an and are as at be but by can do for from has have how i if in is it of on or our "
    "the this that to was we what when where which who why will with you your".split()
)


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", text.lower()) if t not in _STOP and len(t) > 2}


def _token_list(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9']+", text.lower()) if t not in _STOP and len(t) > 2]


def phrase_count(text: str, phrase: str) -> int:
    return len(re.findall(re.escape(phrase.lower()), text.lower()))


_QUESTION_STARTS = ("what", "how", "why", "who", "can", "do", "does", "is", "are", "should")


def _tag_for(keyword: str, main: str) -> str:
    kl = keyword.lower().strip()
    if kl == main.lower():
        return "main"
    if kl.endswith("?") or (kl.split() and kl.split()[0] in _QUESTION_STARTS):
        return "aio"
    if len(kl.split()) >= 5:
        return "long_tail"
    return "secondary"


def _classify(pages: list[dict], keyword: str, llm) -> tuple[list[dict], list[str]]:
    """Intent / page type / audience per competitor (team step 3a)."""
    try:
        raw = llm(
            'You are an SEO analyst. JSON only: {"pages": [{"url": str, "intent": str, '
            '"page_type": str, "audience": str}]}. intent must be one of '
            "informational|commercial|transactional|navigational.",
            f"Classify these pages ranking for '{keyword}': "
            + "; ".join(f"{p['url']} (title: {p['title']}, h1: {p['h1']})" for p in pages),
        )
        by_url = {p.get("url"): p for p in raw.get("pages", []) if isinstance(p, dict)}
        out = [
            {**p, **{k: str(by_url.get(p["url"], {}).get(k, ""))[:80]
                     for k in ("intent", "page_type", "audience")}}
            for p in pages
        ]
        return out, []
    except CredentialMissing as exc:
        return ([{**p, "intent": "", "page_type": "", "audience": ""} for p in pages],
                [f"competitor classification skipped ({exc})"])


def _gap(keyword: str, serp: dict, competitor_rows: dict[str, list[dict]]) -> tuple[list[dict], list[str]]:
    """Keyword gap (team step 3b). Pasted Ahrefs rows when present; honest SERP fallback."""
    if any(competitor_rows.values()):
        merged: dict[str, dict] = {}
        for url, rows in competitor_rows.items():
            for r in rows:
                e = merged.setdefault(r["keyword"].lower(),
                                      {"keyword": r["keyword"], "volume": r["volume"], "in": set()})
                e["in"].add(url)
                e["volume"] = e["volume"] or r["volume"]
        out = [
            {"keyword": e["keyword"], "tag": _tag_for(e["keyword"], keyword),
             "volume": e["volume"], "overlap": len(e["in"]), "source": "ahrefs_pasted"}
            for e in sorted(merged.values(), key=lambda x: -(x["volume"] or 0))
        ]
        return out[:40], []
    est = [{"keyword": q, "tag": "aio", "volume": None, "overlap": 0, "source": "serp_estimated"}
           for q in serp["paa"][:8]]
    est += [{"keyword": r, "tag": _tag_for(r, keyword), "volume": None, "overlap": 0,
             "source": "serp_estimated"}
            for r in serp["related"][:12] if r.lower() != keyword.lower()]
    return est, ["no Ahrefs competitor keywords pasted — gap list is SERP-estimated (no volume data)"]


def _lsi(keyword: str, serp: dict, frequent: list[str], llm) -> tuple[list[dict], list[str]]:
    """LSI top-10, natural fits only (team step 4)."""
    notes: list[str] = []
    try:
        raw = llm(
            'JSON only: {"lsi": [{"term": str, "fit_note": str}]}.',
            f"Give the {rules.LSI_COUNT} best LSI/semantic keywords for an article targeting "
            f"'{keyword}'. Only terms that fit naturally — reject anything that would be a forced "
            f"insertion. One-line fit_note each on where it belongs. Candidates — related searches: "
            f"{serp['related'][:10]}; frequent competitor terms: {frequent[:10]}.",
        )
        items = [{"term": str(i.get("term", ""))[:60], "fit_note": str(i.get("fit_note", ""))[:160]}
                 for i in raw.get("lsi", []) if isinstance(i, dict) and i.get("term")]
        if items:
            return items[:rules.LSI_COUNT], notes
        notes.append("LLM returned no LSI terms — using SERP-derived terms")
    except CredentialMissing as exc:
        notes.append(f"LSI via LLM skipped ({exc}) — using SERP-derived terms")
    fallback = [r for r in serp["related"] if r.lower() != keyword.lower()][:rules.LSI_COUNT]
    return [{"term": t, "fit_note": "from related searches"} for t in fallback], notes


def build_research(keyword: str, pasted: dict, search=None, fetch=None, llm=None) -> dict:
    search = search or sources.serper_search
    fetch = fetch or sources.fetch_page
    llm = llm or sources.llm_json
    degraded: list[str] = []

    serp = search(keyword)  # CredentialMissing propagates — router turns it into a 503
    pages: list[dict] = []
    for r in serp["organic"][:rules.TOP_N]:
        f = fetch(r["link"])
        pages.append({"url": r["link"], "title": r["title"] or f.title, "position": r["position"],
                      "h1": (f.h1[:1] or [""])[0], "word_count": f.word_count, "text": f.text})
        if f.status != 200:
            degraded.append(f"could not fully fetch {r['link']} (status {f.status})")

    classified, notes = _classify(
        [{k: p[k] for k in ("url", "title", "h1")} for p in pages], keyword, llm)
    degraded += notes
    gap, notes = _gap(keyword, serp, pasted.get("competitor_keywords") or {})
    degraded += notes

    top1_text = pages[0]["text"] if pages else ""
    main_count = phrase_count(top1_text, keyword)
    frequent = [{"term": t, "count": c}
                for t, c in Counter(_token_list(top1_text)).most_common(rules.FREQUENT_TERMS)]
    lsi, notes = _lsi(keyword, serp, [f["term"] for f in frequent], llm)
    degraded += notes

    intents = {c["intent"] for c in classified if c["intent"]}
    metrics = {"volume": None, "kd": None, "traffic_potential": None, **(pasted.get("metrics") or {})}
    has_ahrefs = metrics.get("volume") is not None or any((pasted.get("competitor_keywords") or {}).values())
    return {
        "keyword": keyword,
        "metrics": metrics,
        "serp": {"top3": [{"url": p["url"], "title": p["title"], "position": p["position"]} for p in pages],
                 "paa": serp["paa"], "related": serp["related"], "aio_present": serp["aio_present"]},
        "competitors": [{k: c[k] for k in ("url", "intent", "page_type", "audience")} for c in classified],
        "mixed_intent": len(intents) > 1,
        "gap": gap,
        "usage": {"main_count_top1": main_count,
                  "target_min": max(main_count, 1),
                  "target_max": max(main_count, 1) + rules.KEYWORD_COUNT_BONUS,
                  "frequent_terms": frequent},
        "lsi": lsi,
        "data_source": "ahrefs_pasted" if has_ahrefs else "serp_estimated",
        "degraded": degraded,
    }

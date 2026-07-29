"""Team steps 9-10: source real studies/stats, verify each on the live page,
vet domain rating. The anti-hallucination gate: no verification, no citation."""
from __future__ import annotations

from seo_geo_agent import sources
from seo_geo_agent.sources import CredentialMissing

from . import rules
from .research import tokens


def _claim_on_page(claim: str, source_name: str, page_text: str) -> bool:
    want = tokens(claim) | tokens(source_name)
    have = tokens(page_text)
    return bool(want) and len(want & have) >= max(2, len(want) // 3)


def _dr_fields(domain: str, dr_pasted: dict[str, int]) -> dict:
    dr = dr_pasted.get(domain)
    return {"dr": dr, "dr_status": "ok" if dr is not None else "unverified"}


def source_citations(outline_doc: dict, dr_pasted: dict[str, int], llm=None, fetch_raw=None) -> dict:
    llm = llm or sources.llm_json
    fetch_raw = fetch_raw or sources.fetch_text
    target = outline_doc["targets"]["links"]
    headings = [o["heading"] for o in outline_doc["outline"]]
    items: list[dict] = []
    rejected: set[str] = set()
    rounds = 0
    while len(items) < target and rounds < rules.CITATION_MAX_ROUNDS:
        rounds += 1
        try:
            raw = llm(
                'JSON only: {"citations": [{"claim": str, "source_name": str, "url": str, '
                '"section": str}]}.',
                f"Find {target - len(items) + 2} real studies, reports or statistics that support "
                f"sections of this outline: {headings}. Exact URLs only. source_name must be "
                'specific ("Clio 2025 Legal Trends Report", never just "Clio"). section must be one '
                f"of the outline headings verbatim. Never repeat these rejected URLs: {sorted(rejected)[:20]}",
            )
        except CredentialMissing as exc:
            return {"items": items, "short_by": max(0, target - len(items)), "rounds": rounds,
                    "degraded": [f"citation sourcing unavailable ({exc})"]}
        for c in raw.get("citations", []) if isinstance(raw, dict) else []:
            url = str(c.get("url", "")).strip()
            if (not url.startswith("http") or url in rejected
                    or any(i["url"] == url for i in items) or len(items) >= target):
                continue
            try:
                page = fetch_raw(url)
            except CredentialMissing as exc:
                return {"items": items, "short_by": max(0, target - len(items)), "rounds": rounds,
                        "degraded": [f"citation verification unavailable ({exc})"]}
            claim = str(c.get("claim", ""))[:200]
            name = str(c.get("source_name", ""))[:120]
            if page["status"] != 200 or not _claim_on_page(claim, name, page["text"]):
                rejected.add(url)
                continue
            domain = sources.domain_of(url)
            dr_bits = _dr_fields(domain, dr_pasted)
            if dr_bits["dr"] is not None and dr_bits["dr"] < rules.DR_THRESHOLD:
                rejected.add(url)
                continue
            section = str(c.get("section", ""))[:120]
            items.append({"id": f"c{len(items) + 1}", "claim": claim, "source_name": name,
                          "url": url, "domain": domain, **dr_bits,
                          "section": section if section in headings else headings[0],
                          "verified": True})
    return {"items": items, "short_by": max(0, target - len(items)), "rounds": rounds, "degraded": []}


def revet(doc: dict, dr_pasted: dict[str, int], target: int) -> dict:
    """Gate-2 DR paste: enforce the threshold on already-verified items."""
    kept = []
    for item in doc["items"]:
        dr = dr_pasted.get(item["domain"], item.get("dr"))
        if dr is not None and dr < rules.DR_THRESHOLD:
            continue
        kept.append({**item, "dr": dr, "dr_status": "ok" if dr is not None else "unverified"})
    kept = [{**i, "id": f"c{n + 1}"} for n, i in enumerate(kept)]
    return {**doc, "items": kept, "short_by": max(0, target - len(kept))}

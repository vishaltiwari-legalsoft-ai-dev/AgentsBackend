"""Competitor & SERP intelligence — rank tracking, competitor discovery,
sitemap watch (new content feed), and SERP reverse-engineering.

Tracked keywords = brand seeds + top cluster heads, so the watchlist follows
the strategy instead of needing separate curation.
"""
from __future__ import annotations

import re
from datetime import date

from . import keywords as kw_lab
from . import sources, state
from .sources import CredentialMissing, fetch_page
from .topics import _tokens

MAX_TRACKED = 15
MAX_SNAPSHOTS = 12
MAX_DEEP_PAGES = 5


def _domain(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host


def tracked_keywords(brand: dict) -> list[str]:
    from . import site_brain  # site-review seeds fill in when the brand has none

    seeds = [s.strip() for s in site_brain.effective_seeds(brand).get("seeds", []) if s.strip()]
    lab = kw_lab.latest(brand["id"])
    heads = [c["name"] for c in (lab or {}).get("clusters", [])]
    out: list[str] = []
    for kw in seeds + heads:
        if kw.lower() not in [o.lower() for o in out]:
            out.append(kw)
    return out[:MAX_TRACKED]


def rank_snapshot(brand: dict, search=None) -> dict:
    """Record where we rank today for every tracked keyword, and which domains
    keep showing up above us (competitor discovery)."""
    if search is None:
        if not sources.serper_available():
            raise CredentialMissing("Serper key missing — rank tracking needs live SERPs")
        search = sources.serper_search
    ranks: dict[str, dict] = {}
    seen_domains: dict[str, int] = {}
    for kw in tracked_keywords(brand):
        serp = search(kw)
        ours = next(
            (r["position"] for r in serp["organic"] if brand["domain"] in r["link"]), None
        )
        top = [_domain(r["link"]) for r in serp["organic"]]
        for d in top:
            if d and d != brand["domain"]:
                seen_domains[d] = seen_domains.get(d, 0) + 1
        ranks[kw] = {"position": ours, "top": top[:5]}

    doc = state.load(f"ranks-{brand['id']}") or {"snapshots": []}
    doc["snapshots"] = (doc["snapshots"] + [{"at": date.today().isoformat(), "ranks": ranks}])[-MAX_SNAPSHOTS:]
    doc["suggested_competitors"] = [
        d for d, _ in sorted(seen_domains.items(), key=lambda kv: -kv[1])[:8]
    ]
    state.save(f"ranks-{brand['id']}", doc)
    return doc


def rank_shifts(brand_id: str) -> list[dict]:
    """Per-keyword movement between the two latest snapshots."""
    doc = state.load(f"ranks-{brand_id}")
    if not doc or not doc.get("snapshots"):
        return []
    latest = doc["snapshots"][-1]
    prev = doc["snapshots"][-2] if len(doc["snapshots"]) > 1 else {"ranks": {}}
    shifts = []
    for kw, entry in latest["ranks"].items():
        before = (prev["ranks"].get(kw) or {}).get("position")
        now = entry.get("position")
        shifts.append({
            "keyword": kw,
            "position": now,
            "previous": before,
            "delta": (before - now) if (before and now) else None,  # positive = we moved up
            "top": entry.get("top", []),
        })
    shifts.sort(key=lambda s: (s["position"] is None, s["position"] or 99))
    return shifts


def sitemap_watch(brand: dict, fetch_sitemap=None, state_key: str | None = None) -> dict:
    """Diff each tracked competitor's sitemap vs last check -> new content feed.

    ``state_key`` lets independent callers (the manual-competitor tracker vs the
    top-5 profile builder) keep their own "known domains" state so one doesn't
    stomp the other's doc — default preserves the original shared doc id."""
    fetch = fetch_sitemap or sources.fetch_sitemap
    key = state_key or f"sitemaps-{brand['id']}"
    stored = state.load(key) or {"domains": {}}
    feed: dict[str, dict] = {}
    for comp in brand.get("competitors", [])[:8]:
        try:
            urls = fetch(comp)
        except CredentialMissing:
            raise
        known = set(stored["domains"].get(comp, []))
        new = [u for u in urls if u not in known] if known else []
        stored["domains"][comp] = urls
        feed[comp] = {
            "at": date.today().isoformat(),
            "total": len(urls),
            "new_urls": new[:20],
            "new_count": len(new),
            "first_check": not known,
        }
    stored["last_feed"] = feed
    state.save(key, stored)
    return feed


def serp_deep_dive(brand: dict, query: str, search=None, fetch=fetch_page) -> dict:
    """Reverse-engineer the top of the SERP for one query: who ranks, what
    structure and questions their pages share, which entities keep appearing."""
    if search is None:
        if not sources.serper_available():
            raise CredentialMissing("Serper key missing — SERP analysis needs live results")
        search = sources.serper_search
    serp = search(query)
    pages = []
    for r in serp["organic"][:MAX_DEEP_PAGES]:
        try:
            facts = fetch(r["link"])
        except CredentialMissing:
            break
        if facts.status == 200:
            pages.append(facts)

    # Structural patterns: heading themes shared by 2+ of the top pages.
    theme_counts: dict[str, int] = {}
    theme_display: dict[str, str] = {}
    for p in pages:
        seen_here = set()
        for h in p.h2 + p.h3:
            key = " ".join(sorted(_tokens(h)))
            if key and key not in seen_here:
                seen_here.add(key)
                theme_counts[key] = theme_counts.get(key, 0) + 1
                theme_display.setdefault(key, h)
    common_themes = [theme_display[k] for k, n in sorted(theme_counts.items(), key=lambda kv: -kv[1]) if n >= 2][:12]

    # Entities: significant terms appearing across multiple titles/headings.
    term_counts: dict[str, int] = {}
    for p in pages:
        for text in [p.title] + p.h1 + p.h2:
            for tok in _tokens(text):
                term_counts[tok] = term_counts.get(tok, 0) + 1
    entities = [t for t, n in sorted(term_counts.items(), key=lambda kv: -kv[1]) if n >= 3][:15]

    questions = list(dict.fromkeys(serp["paa"] + [q for p in pages for q in p.questions]))[:12]
    word_counts = [p.word_count for p in pages if p.word_count]
    return {
        "query": query,
        "at": date.today().isoformat(),
        "who_ranks": [{"domain": _domain(r["link"]), "title": r["title"], "position": r["position"]}
                      for r in serp["organic"]],
        "our_position": next((r["position"] for r in serp["organic"] if brand["domain"] in r["link"]), None),
        "common_themes": common_themes,
        "questions": questions,
        "entities": entities,
        "target_word_count": round(sum(word_counts) / len(word_counts)) if word_counts else None,
        "schema_seen": sorted({t for p in pages for t in p.schema_types}),
        "aio_present": serp["aio_present"],
        "pages_analyzed": len(pages),
    }


# --------------------------- competitor profiles ---------------------------

MAX_PROFILES = 5
MAX_CONTENT_POSTS = 5
MAX_SERPER_PER_COMPETITOR = 2
MAX_HOT_TOPICS = 5


def resolve_top5(brand: dict, ranks_doc: dict | None) -> list[str]:
    """The competitor set a profile run covers: whatever the brand curated,
    topped up with SERP-discovered domains until there are 5."""
    manual = [d for d in brand.get("competitors", []) or [] if d]
    suggested = (ranks_doc or {}).get("suggested_competitors") or []
    out: list[str] = []
    for d in manual + suggested:
        if d.lower() not in [o.lower() for o in out]:
            out.append(d)
    return out[:MAX_PROFILES]


def _domain_stats(domain: str, latest_ranks: dict) -> tuple[int | None, float | None, list[dict]]:
    """Visibility, average position, and the keywords a competitor beats us on —
    all read straight off the latest rank snapshot, no extra lookups."""
    total = len(latest_ranks)
    positions: list[int] = []
    won: list[dict] = []
    for kw, entry in latest_ranks.items():
        top = [t.lower() for t in entry.get("top", [])]
        if domain.lower() not in top:
            continue
        their_position = top.index(domain.lower()) + 1
        positions.append(their_position)
        our_position = entry.get("position")
        if our_position is None or their_position < our_position:
            won.append({"keyword": kw, "their_position": their_position, "our_position": our_position})
    visibility_pct = round(100 * len(positions) / total) if total else None
    avg_position = round(sum(positions) / len(positions), 1) if positions else None
    return visibility_pct, avg_position, won


def _match_volume(title: str, clusters: list[dict]):
    """Honest reach math needs a real number: only fires when a keyword-lab
    cluster's keyword is actually contained in the post title, and that
    cluster carries a volume estimate."""
    title_tokens = _tokens(title)
    if not title_tokens:
        return None
    for c in clusters:
        volume = c.get("volume_est")
        if not volume:
            continue
        for kw in c.get("keywords", []):
            kw_tokens = _tokens(kw)
            if kw_tokens and kw_tokens <= title_tokens:
                return volume
    return None


def _slug_words(url: str) -> list[str]:
    path = re.sub(r"^https?://[^/]+", "", url)
    return [w for w in re.split(r"[^a-z0-9]+", path.lower()) if len(w) > 2]


def _hot_topics(urls: list[str]) -> list[str]:
    """Top recurring token bigrams across a competitor's new URL slugs — plain
    string munging, no AI, but enough to see what they're publishing about."""
    counts: dict[str, int] = {}
    for u in urls:
        words = _slug_words(u)
        for a, b in zip(words, words[1:]):
            bigram = f"{a} {b}"
            counts[bigram] = counts.get(bigram, 0) + 1
    return [b for b, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:MAX_HOT_TOPICS]


def build_profiles(brand: dict, search=None, fetch=None, fetch_sitemap=None) -> dict:
    """Full competitor-profile pass for the brand's top 5: visibility + keywords
    won from the latest rank snapshot, plus a content feed with honestly labelled
    reach estimates. Persisted to ``competitor-profiles-{brand_id}``."""
    ranks_doc = state.load(f"ranks-{brand['id']}")
    if not ranks_doc or not ranks_doc.get("snapshots"):
        raise CredentialMissing("Run a data refresh first — competitor discovery needs rank snapshots")
    from . import insights  # lazy: insights imports this module at load time

    fetch = fetch or fetch_page
    if search is None:
        search = sources.serper_search if sources.serper_available() else None

    top5 = resolve_top5(brand, ranks_doc)
    latest_ranks = ranks_doc["snapshots"][-1]["ranks"]
    lab = kw_lab.latest(brand["id"])
    clusters = (lab or {}).get("clusters", [])

    notes: list[str] = []
    feed: dict[str, dict] = {}
    if top5:
        try:
            feed = sitemap_watch({**brand, "competitors": top5}, fetch_sitemap=fetch_sitemap,
                                  state_key=f"profile-sitemaps-{brand['id']}")
        except CredentialMissing as exc:
            notes.append(f"Sitemap watch: {exc}")

    profiles = []
    for domain in top5:
        visibility_pct, avg_position, keywords_won = _domain_stats(domain, latest_ranks)
        new_urls = feed.get(domain, {}).get("new_urls", [])

        recent_posts: list[dict] = []
        calls = 0
        for url in new_urls[:MAX_CONTENT_POSTS]:
            try:
                facts = fetch(url)
            except CredentialMissing as exc:
                notes.append(f"Page fetch {url}: {exc}")
                break
            if facts.status != 200 or not (facts.title or facts.text):
                continue
            title = facts.title or url
            topic = max(facts.h1, key=len) if facts.h1 else title
            volume = _match_volume(title, clusters)
            clicks = None
            # A matched volume is real signal even before any SERP check runs —
            # only claim "no volume data" when there genuinely was none.
            basis = "volume matched — SERP check unavailable this run" if volume else "no volume data — reach unknown"
            if volume and search and calls < MAX_SERPER_PER_COMPETITOR:
                calls += 1
                try:
                    serp = search(topic)
                    position = next(
                        (r["position"] for r in serp["organic"] if domain in r["link"]), None
                    )
                    if position:
                        clicks = round(volume * insights.ctr_at(position))
                        basis = "lab volume × CTR curve"
                except CredentialMissing as exc:
                    notes.append(f"Serper {domain}: {exc}")
                    search = None
            recent_posts.append({
                "url": url, "title": title, "topic": topic,
                "est_monthly_clicks": clicks, "estimate_basis": basis,
            })

        profiles.append({
            "domain": domain,
            "visibility_pct": visibility_pct,
            "avg_position": avg_position,
            "keywords_won": keywords_won,
            "recent_posts": recent_posts,
            "hot_topics": _hot_topics(new_urls),
        })

    doc = {"at": date.today().isoformat(), "notes": notes, "profiles": profiles}
    state.save(f"competitor-profiles-{brand['id']}", doc)
    return doc


def latest_profiles(brand_id: str) -> dict | None:
    return state.load(f"competitor-profiles-{brand_id}")

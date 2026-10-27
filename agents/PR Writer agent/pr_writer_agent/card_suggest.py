"""Brand card suggestions from the brand's own public web presence (a13).

``POST /api/pr/brands/{id}/card/suggest`` reads up to :data:`MAX_PAGES` pages
on the brand's own domain (homepage, about, press, leadership, contact, a few
Serper ``site:`` hits and any ``extra_urls`` the caller names), fences every
page as untrusted, makes ONE extraction call on the fast slot and then keeps
only what the pages themselves support. Nothing here writes anywhere: the
output is a suggestion the person pours into the card editor and saves with
the existing PUT.

The rule that matters: **every suggestion cites a page that was actually read
and whose text supports it.** The model names a page id and a verbatim quote
per item; code checks the quote is really on that page and that the value's
key strings are in the page's text (``exact`` fields verbatim, prose fields by
word overlap), the same way ``evidence.from_pages`` keeps a research quote only
when it is on the page. What fails is dropped, never kept as a plausible
guess, and a section with nothing left is omitted.

Pure apart from the injected ``search`` / ``fetch`` / ``sitemap`` / ``invoke``
callables, so the router suite drives it offline.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import urlparse

from pr_writer_agent import evidence as ev
from pr_writer_agent import llm as pr_llm
from pr_writer_agent.prompts import STR, STR_LIST, INJECTION, Step, arr, obj

STEP_NAME = "card_suggest"
#: Card sections this route may suggest — every card section except ``reviewers``.
SECTIONS = ("identity", "boilerplate", "messaging", "proof_points", "spokespeople",
            "product_names", "avoid", "media_contact", "press_kit", "keywords")

MAX_PAGES = 10
MAX_EXTRA_URLS = 5
SERPER_RESULTS = 5
PAGE_CHARS = 5000
#: Seconds of the request deadline kept back for the model call: fetching
#: stops when less than this is left, so a slow site cannot starve extraction.
MODEL_RESERVE_S = 90

#: Paths probed when the sitemap gives nothing for a section (first hit wins).
PROBE_PATHS = {
    "about": ("/about", "/about-us", "/company", "/who-we-are"),
    "press": ("/press", "/news", "/newsroom", "/media", "/press-room"),
    "team": ("/leadership", "/team", "/our-team", "/management", "/people"),
    "contact": ("/contact", "/contact-us"),
}
#: Words in a sitemap path that mark a page as one of those sections.
SECTION_WORDS = {
    "about": ("about", "company", "who-we-are"),
    "press": ("press", "news", "newsroom", "media"),
    "team": ("leadership", "team", "management", "people"),
    "contact": ("contact",),
}

#: Full US state name -> AP dateline abbreviation (AP Stylebook; eight states
#: are never abbreviated). Used to build ``identity.dateline`` in code.
AP_STATE = {
    "alabama": "Ala.", "alaska": "Alaska", "arizona": "Ariz.", "arkansas": "Ark.", "california": "Calif.",
    "colorado": "Colo.", "connecticut": "Conn.", "delaware": "Del.", "florida": "Fla.", "georgia": "Ga.",
    "hawaii": "Hawaii", "idaho": "Idaho", "illinois": "Ill.", "indiana": "Ind.", "iowa": "Iowa",
    "kansas": "Kan.", "kentucky": "Ky.", "louisiana": "La.", "maine": "Maine", "maryland": "Md.",
    "massachusetts": "Mass.", "michigan": "Mich.", "minnesota": "Minn.", "mississippi": "Miss.",
    "missouri": "Mo.", "montana": "Mont.", "nebraska": "Neb.", "nevada": "Nev.", "new hampshire": "N.H.",
    "new jersey": "N.J.", "new mexico": "N.M.", "new york": "N.Y.", "north carolina": "N.C.",
    "north dakota": "N.D.", "ohio": "Ohio", "oklahoma": "Okla.", "oregon": "Ore.", "pennsylvania": "Pa.",
    "rhode island": "R.I.", "south carolina": "S.C.", "south dakota": "S.D.", "tennessee": "Tenn.",
    "texas": "Texas", "utah": "Utah", "vermont": "Vt.", "virginia": "Va.", "washington": "Wash.",
    "west virginia": "W.Va.", "wisconsin": "Wis.", "wyoming": "Wyo.", "district of columbia": "D.C.",
}
_AP_BY_CODE = {"AL": "Ala.", "AK": "Alaska", "AZ": "Ariz.", "AR": "Ark.", "CA": "Calif.", "CO": "Colo.",
               "CT": "Conn.", "DE": "Del.", "FL": "Fla.", "GA": "Ga.", "HI": "Hawaii", "ID": "Idaho",
               "IL": "Ill.", "IN": "Ind.", "IA": "Iowa", "KS": "Kan.", "KY": "Ky.", "LA": "La.", "ME": "Maine",
               "MD": "Md.", "MA": "Mass.", "MI": "Mich.", "MN": "Minn.", "MS": "Miss.", "MO": "Mo.",
               "MT": "Mont.", "NE": "Neb.", "NV": "Nev.", "NH": "N.H.", "NJ": "N.J.", "NM": "N.M.",
               "NY": "N.Y.", "NC": "N.C.", "ND": "N.D.", "OH": "Ohio", "OK": "Okla.", "OR": "Ore.",
               "PA": "Pa.", "RI": "R.I.", "SC": "S.C.", "SD": "S.D.", "TN": "Tenn.", "TX": "Texas",
               "UT": "Utah", "VT": "Vt.", "VA": "Va.", "WA": "Wash.", "WV": "W.Va.", "WI": "Wis.",
               "WY": "Wyo.", "DC": "D.C."}


# --------------------------------------------------------------------------- #
# Domains and URLs
# --------------------------------------------------------------------------- #

def own_domain(url_or_domain: str) -> str:
    raw = (url_or_domain or "").strip().lower()
    host = urlparse(raw).hostname if "://" in raw else raw.split("/")[0].split(":")[0]
    return (host or "").removeprefix("www.")


def on_domain(url: str, domain: str) -> bool:
    """True when ``url`` is an http(s) address on ``domain`` or a subdomain."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not domain:
        return False
    host = parsed.hostname.lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def _path_words(url: str) -> str:
    return urlparse(url).path.lower()


def plan_urls(domain: str, sitemap_urls: list[str]) -> list[str]:
    """The brand's own pages to probe, homepage first: for each section a
    sitemap URL whose path names it, else that section's probe paths."""
    base = f"https://{domain}"
    out: list[str] = [base + "/"]
    own_sitemap = [u for u in sitemap_urls if on_domain(u, domain)]
    for section, words in SECTION_WORDS.items():
        hit = next((u for u in own_sitemap
                    if any(re.search(rf"(^|/|-){re.escape(w)}(/|-|$|\.)", _path_words(u)) for w in words)
                    and _path_words(u).count("/") <= 2), None)
        if hit:
            out.append(hit)
        else:
            out.extend(base + p for p in PROBE_PATHS[section])
    return list(dict.fromkeys(out))


def _as_page(fetched: Any, url: str) -> dict | None:
    data = fetched if isinstance(fetched, dict) else dict(getattr(fetched, "__dict__", {}) or {})
    text = str(data.get("text") or "").strip()
    if not text:
        return None
    return {"url": str(data.get("read_url") or data.get("url") or url), "title": str(data.get("title") or ""),
            "text": text}


def gather(domain: str, brand_name: str, *, search: Callable[[str], dict], fetch: Callable[[str], Any],
           sitemap: Callable[[str], list[str]] | None, extra_urls: list[str],
           time_left: Callable[[], float] = lambda: 1e9) -> dict[str, Any]:
    """``{"pages": [{id, url, title, text}], "candidates": n}``.

    Search runs first and its failure propagates (``SearchUnavailable`` /
    ``CredentialMissing`` -> the router's honest 424). Order: the probed
    sections (one page each), then every ``extra_urls`` page the caller named,
    then the Serper hits. A page that will not load, loads empty, repeats one
    already read, or lands off the brand's domain is skipped. Fetching stops
    when :data:`MODEL_RESERVE_S` seconds or fewer remain."""
    serp = search(f"site:{domain} {brand_name}".strip())
    hits = [h.get("link", "") for h in (serp or {}).get("organic", []) if h.get("link")]
    hits = [h for h in hits if on_domain(h, domain)][:SERPER_RESULTS]
    sitemap_urls: list[str] = []
    if sitemap is not None:
        try:
            sitemap_urls = list(sitemap(domain) or [])
        except Exception:  # noqa: BLE001 — a missing sitemap means probing, not failing
            sitemap_urls = []
    probe = plan_urls(domain, sitemap_urls)
    probed_ok = 0
    pages: list[dict] = []
    seen: set[str] = set()

    def read(url: str) -> bool:
        if len(pages) >= MAX_PAGES or time_left() < MODEL_RESERVE_S:
            return False
        try:
            page = _as_page(fetch(url), url)
        except Exception:  # noqa: BLE001 — a dead page is skipped, not fatal
            return False
        if page is None or not on_domain(page["url"], domain):
            return False
        key = page["url"].rstrip("/").lower()
        if key in seen or ev._norm_ws(page["text"])[:400] in {ev._norm_ws(p["text"])[:400] for p in pages}:
            return False
        seen.add(key)
        pages.append(page)
        return True

    # One page per probed section: the first probe path that answers stops the rest.
    by_section: dict[str, list[str]] = {}
    for url in probe:
        path = _path_words(url)
        section = next((s for s, paths in PROBE_PATHS.items()
                        if path in paths or path.rstrip("/") in paths), None)
        by_section.setdefault(section or url, []).append(url)
    for section, urls in by_section.items():
        for url in urls:
            if read(url):
                probed_ok += 1
                if section in PROBE_PATHS:
                    break
    extras = list(dict.fromkeys(u.strip() for u in extra_urls if u and u.strip()))
    for url in extras:
        read(url)  # the caller asked for it by name: always attempted, never grouped away
    for url in hits:
        read(url)
    return {"pages": [{"id": f"pg-{i + 1}", **p} for i, p in enumerate(pages)],
            "candidates": len(probe) + len(extras) + len(hits), "probed": probed_ok}


# --------------------------------------------------------------------------- #
# The one model call
# --------------------------------------------------------------------------- #

EVIDENCE = arr(obj({"page_id": STR, "quote": STR}), hi=4)


def _section(props: dict) -> dict:
    return {"anyOf": [obj({**props, "evidence": EVIDENCE}), {"type": "null"}]}


SUGGEST_SCHEMA = obj({
    "identity": _section({"legal_name": STR, "short_name": STR, "hq_city": STR, "hq_state": STR,
                          "newsroom_url": STR}),
    "boilerplate": _section({"text": STR}),
    "messaging": _section({"mission": STR, "positioning": STR, "key_messages": arr(STR, hi=3)}),
    "proof_points": arr(obj({"claim": STR, "date": STR, "evidence": EVIDENCE}), hi=8),
    "spokespeople": arr(obj({"name": STR, "title": STR, "topics": STR, "evidence": EVIDENCE}), hi=8),
    "product_names": arr(obj({"name": STR, "evidence": EVIDENCE}), hi=15),
    "avoid": _section({"words": STR_LIST, "competitor_policy": STR}),
    "media_contact": _section({"name": STR, "title": STR, "email": STR, "phone": STR}),
    "press_kit": arr(obj({"label": STR, "url": STR, "evidence": EVIDENCE}), hi=10),
    "keywords": arr(obj({"keyword": STR, "evidence": EVIDENCE}), hi=20),
    "injection_notes": INJECTION,
})

_TASK = """Brand card suggestions. The fenced pages are the brand's own public website. Fill the brand card sections below ONLY with what those pages state. Every item names the page it came from (page_id) and quotes the exact passage, copied verbatim, that supports it. Code will check each quote against the page and drop anything that is not there, so paraphrased quotes and guessed values are wasted output.
- identity: the legal entity name as written (with LLC/Inc./Ltd. if shown), the everyday short name, the headquarters city and state/region, and the URL of the press or news page if one exists. Leave a field "" when the pages do not state it.
- boilerplate: the brand's own "About" paragraph as written on the site (one paragraph).
- messaging: mission and positioning in the site's own words; up to 3 key messages the site repeats.
- proof_points: concrete facts the site states — numbers, years founded, client counts, certifications, awards — with the date the page gives ("" if none).
- spokespeople: named people with their exact titles; topics = what the page says they lead or speak on ("" if nothing).
- product_names: named products or services, spelled exactly as the site spells them.
- avoid: ONLY when the site publishes usage or brand guidance (words it says not to use, how it refers to competitors). Usually null.
- media_contact: ONLY a press or media contact the site publishes (an e-mail or phone on a page about press, media or PR). Never a sales or support contact. Usually null.
- press_kit: links the site offers to press materials (logos, brand assets, media kit, fact sheet) as absolute URLs that appear on the pages.
- keywords: terms the site emphasises about what it does (headings, repeated phrases), each written as it appears.
Use null for a section with nothing, an empty list for a list with nothing. Never fill a gap with what a company like this usually says."""

STEP = Step(name=STEP_NAME, slot="fast", timeout_s=75, max_tokens=3500, temperature=0.0, schema=SUGGEST_SCHEMA,
            task=_TASK, est_input_tokens=14000, est_output_tokens=1500)


def prompt_user(brand: dict, pages: list[dict], seeds: list[str]) -> str:
    head = {"brand": brand.get("name") or "", "domain": brand.get("domain") or "",
            "seo_seed_keywords": list(seeds or [])[:20]}
    return f"BRAND: {json.dumps(head, ensure_ascii=False)}\n\n" + "\n\n".join(
        f"PAGE {p['id']} — {p['url']}\nTITLE: {p.get('title') or ''}\n"
        + ev.fence("web_page", p["id"], p.get("text", ""), limit=PAGE_CHARS) for p in pages)


# --------------------------------------------------------------------------- #
# Verification — what the pages support
# --------------------------------------------------------------------------- #

_STOP = frozenset("the and for with that this from your our their are was were will have has into over under about "
                  "more than then them they what when where which while who whom whose with within without".split())
_WORD = re.compile(r"[a-z0-9][a-z0-9'&.-]*")


def _norm(text: Any) -> str:
    return ev._norm_ws(str(text or ""))


def _words(text: str) -> set[str]:
    return {w.strip(".'-") for w in _WORD.findall(_norm(text)) if len(w) >= 4 and w not in _STOP}


def _overlap(value: str, page_text: str) -> float:
    want = _words(value)
    if not want:
        return 0.0
    have = _words(page_text)
    return len(want & have) / len(want)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


class _Check:
    """Which of the cited pages support one item, and how well."""

    def __init__(self, pages: dict[str, dict], item: dict):
        self.pages = pages
        self.cited = [str(e.get("page_id") or "") for e in (item.get("evidence") or []) if isinstance(e, dict)]
        self.quotes = {str(e.get("page_id") or ""): str(e.get("quote") or "") for e in (item.get("evidence") or [])
                       if isinstance(e, dict)}
        self.sources: list[str] = []
        self.verbatim_quote = False
        self.min_ratio = 1.0

    def _candidates(self) -> list[dict]:
        return [self.pages[i] for i in dict.fromkeys(self.cited) if i in self.pages]

    def exact(self, *values: str) -> bool:
        """Every non-empty value appears verbatim (case/space-insensitive) on
        one cited page. Records that page as a source."""
        wanted = [_norm(v) for v in values if str(v or "").strip()]
        if not wanted:
            return False
        for page in self._candidates():
            text = _norm(page["text"])
            if all(w in text for w in wanted):
                self._hit(page)
                return True
        return False

    def prose(self, value: str, floor: float = 0.6) -> bool:
        """A paraphrase-tolerant check for sentences: at least ``floor`` of the
        value's significant words are on one cited page."""
        if not str(value or "").strip():
            return False
        best, best_page = 0.0, None
        for page in self._candidates():
            r = _overlap(value, page["text"])
            if r > best:
                best, best_page = r, page
        if best_page is None or best < floor:
            return False
        self.min_ratio = min(self.min_ratio, best)
        self._hit(best_page)
        return True

    def _hit(self, page: dict) -> None:
        if page["url"] not in self.sources:
            self.sources.append(page["url"])
        quote = _norm(self.quotes.get(page["id"], ""))
        if quote and quote in _norm(page["text"]):
            self.verbatim_quote = True

    def confidence(self) -> str:
        if self.verbatim_quote and self.min_ratio >= 0.85:
            return "high"
        if self.verbatim_quote or self.min_ratio >= 0.75:
            return "medium"
        return "low"


def _rank(levels: list[str]) -> str:
    order = {"high": 0, "medium": 1, "low": 2}
    return max(levels, key=lambda l: order.get(l, 2)) if levels else "low"


def _note(kept: int, offered: int, what: str) -> str | None:
    dropped = offered - kept
    return None if dropped <= 0 else f"{dropped} of {offered} {what} left out: not found on the pages read."


def _suggestion(value: Any, sources: list[str], confidence: str, note: str | None) -> dict:
    return {"value": value, "sources": list(dict.fromkeys(sources)), "confidence": confidence, "note": note}


def _ap_state(state: str) -> str:
    raw = (state or "").strip().rstrip(".")
    if not raw:
        return ""
    if raw.upper() in _AP_BY_CODE:
        return _AP_BY_CODE[raw.upper()]
    return AP_STATE.get(raw.lower(), raw)


def _identity(payload: dict | None, pages: dict[str, dict], domain: str, homepage: dict | None) -> dict | None:
    value = {"legal_name": "", "short_name": "", "website": "", "newsroom_url": "", "hq_city": "", "hq_state": "",
             "dateline": ""}
    sources: list[str] = []
    levels: list[str] = []
    found = 0
    if homepage is not None:
        value["website"] = f"https://{domain}"
        sources.append(homepage["url"])
    for key in ("legal_name", "short_name", "hq_city", "hq_state"):
        text = str((payload or {}).get(key) or "").strip()
        if not text:
            continue
        chk = _Check(pages, payload or {})
        if chk.exact(text):
            value[key] = text
            sources += chk.sources
            levels.append(chk.confidence())
            found += 1
    newsroom = str((payload or {}).get("newsroom_url") or "").strip()
    read_urls = {p["url"].rstrip("/"): p for p in pages.values()}
    if newsroom and on_domain(newsroom, domain) and (
            newsroom.rstrip("/") in read_urls or any(newsroom.lower() in p["text"].lower() for p in pages.values())):
        value["newsroom_url"] = newsroom
        src = read_urls.get(newsroom.rstrip("/"))
        sources.append(src["url"] if src else next(p["url"] for p in pages.values() if newsroom.lower() in p["text"].lower()))
        found += 1
    else:
        # The model's URL (if any) was not a page we read nor on one: fall back
        # to a press/news page we did read, which is a fact we hold ourselves.
        press = next((p for p in pages.values()
                      if any(w in _path_words(p["url"]).split("/") for w in SECTION_WORDS["press"])), None)
        if press is not None:
            value["newsroom_url"] = press["url"]
            sources.append(press["url"])
    if value["hq_city"] and value["hq_state"]:
        value["dateline"] = f"{value['hq_city'].upper()}, {_ap_state(value['hq_state'])}"
    if not found and not value["website"]:
        return None
    return _suggestion(value, sources, _rank(levels) if levels else "medium", None)


def _rows(items: Any, pages: dict[str, dict], keep: Callable[[dict, _Check], dict | None], what: str) -> dict | None:
    offered = [i for i in (items or []) if isinstance(i, dict)]
    kept, sources, levels = [], [], []
    for item in offered:
        chk = _Check(pages, item)
        row = keep(item, chk)
        if row is None:
            continue
        kept.append(row)
        sources += chk.sources
        levels.append(chk.confidence())
    if not kept:
        return None
    return _suggestion(kept, sources, _rank(levels), _note(len(kept), len(offered), what))


def finalize(payload: dict, pages: list[dict], *, domain: str, seeds: list[str]) -> dict[str, dict]:
    """Keep what the read pages support, in the card's own shapes."""
    by_id = {p["id"]: p for p in pages}
    homepage = next((p for p in pages if _path_words(p["url"]).strip("/") == ""), None)
    out: dict[str, dict] = {}

    ident = _identity(payload.get("identity"), by_id, domain, homepage)
    if ident:
        out["identity"] = ident

    bp = payload.get("boilerplate") or {}
    text = str(bp.get("text") or "").strip()[:3000]
    chk = _Check(by_id, bp)
    if text and chk.prose(text, 0.7):
        out["boilerplate"] = _suggestion({"text": text}, chk.sources, chk.confidence(), None)

    msg = payload.get("messaging") or {}
    value = {"mission": "", "positioning": "", "key_messages": []}
    sources, levels, offered, kept = [], [], 0, 0
    for key in ("mission", "positioning"):
        t = str(msg.get(key) or "").strip()
        if not t:
            continue
        offered += 1
        chk = _Check(by_id, msg)
        if chk.prose(t):
            value[key] = t
            sources += chk.sources
            levels.append(chk.confidence())
            kept += 1
    for t in [str(k).strip() for k in (msg.get("key_messages") or []) if str(k).strip()][:3]:
        offered += 1
        chk = _Check(by_id, msg)
        if chk.prose(t):
            value["key_messages"].append(t)
            sources += chk.sources
            levels.append(chk.confidence())
            kept += 1
    if kept:
        out["messaging"] = _suggestion(value, sources, _rank(levels), _note(kept, offered, "messaging lines"))

    def proof(item: dict, chk: _Check) -> dict | None:
        claim = str(item.get("claim") or "").strip()
        if not claim or not chk.prose(claim, 0.75):
            return None
        date_ = str(item.get("date") or "").strip()
        if date_ and not any(_norm(date_) in _norm(by_id[i]["text"]) for i in chk.cited if i in by_id):
            date_ = ""
        return {"claim": claim, "source": chk.sources[0], "date": date_}
    rows = _rows(payload.get("proof_points"), by_id, proof, "proof points")
    if rows:
        out["proof_points"] = rows

    def person(item: dict, chk: _Check) -> dict | None:
        name, title = str(item.get("name") or "").strip(), str(item.get("title") or "").strip()
        if not name or not title or not chk.exact(name, title):
            return None
        topics = str(item.get("topics") or "").strip()
        if topics and not chk.prose(topics):
            topics = ""
        return {"name": name, "title": title, "topics": topics, "quote_style": "", "approval_contact": ""}
    rows = _rows(payload.get("spokespeople"), by_id, person, "people")
    if rows:
        out["spokespeople"] = rows

    def product(item: dict, chk: _Check) -> str | None:
        name = str(item.get("name") or "").strip()
        return name if name and chk.exact(name) else None
    rows = _rows(payload.get("product_names"), by_id, product, "product names")
    if rows:
        rows["value"] = list(dict.fromkeys(rows["value"]))
        out["product_names"] = rows

    avoid = payload.get("avoid") or {}
    words, sources, levels = [], [], []
    for w in [str(w).strip() for w in (avoid.get("words") or []) if str(w).strip()]:
        chk = _Check(by_id, avoid)
        if chk.exact(w):
            words.append(w)
            sources += chk.sources
            levels.append(chk.confidence())
    policy = str(avoid.get("competitor_policy") or "").strip()
    chk = _Check(by_id, avoid)
    if policy and chk.prose(policy):
        sources += chk.sources
        levels.append(chk.confidence())
    else:
        policy = ""
    if words or policy:
        out["avoid"] = _suggestion({"words": words, "competitor_policy": policy}, sources, _rank(levels), None)

    mc = payload.get("media_contact") or {}
    email, phone = str(mc.get("email") or "").strip(), str(mc.get("phone") or "").strip()
    chk = _Check(by_id, mc)
    press_pages = [p for p in chk._candidates()
                   if re.search(r"\b(press|media|public relations|\bpr\b|communications)\b", p["text"], re.I)]
    contact = None
    if press_pages:
        only_press = _Check({p["id"]: p for p in press_pages}, mc)
        has_email = bool(email) and bool(re.fullmatch(r"[\w.+-]+@[\w-]+\.[\w.-]+", email)) and only_press.exact(email)
        phone_page = next((p for p in press_pages if len(_digits(phone)) >= 7 and _digits(phone) in _digits(p["text"])),
                          None)
        has_phone = phone_page is not None
        if has_email or has_phone:
            if has_phone:
                only_press._hit(phone_page)
            name = str(mc.get("name") or "").strip()
            title = str(mc.get("title") or "").strip()
            contact = {"name": name if name and only_press.exact(name) else "",
                       "title": title if title and only_press.exact(title) else "",
                       "email": email if has_email else "", "phone": phone if has_phone else ""}
            out["media_contact"] = _suggestion(contact, only_press.sources, only_press.confidence(), None)

    read_urls = {p["url"].rstrip("/").lower() for p in pages}

    def kit(item: dict, chk: _Check) -> dict | None:
        url = str(item.get("url") or "").strip()
        if not url or not on_domain(url, domain):
            return None
        cited = [by_id[i] for i in chk.cited if i in by_id]
        on_page = next((p for p in cited if url.lower() in p["text"].lower()), None)
        if url.rstrip("/").lower() not in read_urls and on_page is None:
            return None
        chk._hit(on_page or next(p for p in pages if p["url"].rstrip("/").lower() == url.rstrip("/").lower()))
        label = str(item.get("label") or "").strip()
        if label and not chk.exact(label):
            label = urlparse(url).path.strip("/").rsplit("/", 1)[-1] or "Press kit"
        return {"label": label or "Press kit", "url": url}
    rows = _rows(payload.get("press_kit"), by_id, kit, "links")
    if rows:
        out["press_kit"] = rows

    kw_value, kw_sources, kw_levels = [], [], []
    for seed in [str(s).strip() for s in (seeds or []) if str(s).strip()]:
        page = next((p for p in pages if _norm(seed) in _norm(p["text"])), None)
        if page is not None and seed.lower() not in {k.lower() for k in kw_value}:
            kw_value.append(seed)
            kw_sources.append(page["url"])
            kw_levels.append("high")
    for item in [i for i in (payload.get("keywords") or []) if isinstance(i, dict)]:
        kw = str(item.get("keyword") or "").strip()
        chk = _Check(by_id, item)
        if kw and kw.lower() not in {k.lower() for k in kw_value} and chk.exact(kw):
            kw_value.append(kw)
            kw_sources += chk.sources
            kw_levels.append(chk.confidence())
    if kw_value:
        out["keywords"] = _suggestion(kw_value[:20], kw_sources, _rank(kw_levels), None)
    return out


def run(brand: dict, pages: list[dict], *, seeds: list[str], invoke=None) -> dict[str, Any]:
    """One extraction call (plus at most one repair) over the fenced pages,
    then :func:`finalize`. ``{"suggestions", "usage", "notes"}``."""
    if not pages:
        return {"suggestions": {}, "usage": {"calls": 0}, "notes": ["no page on the brand's site could be read"]}
    result = pr_llm.call_json(STEP, STEP.system(), prompt_user(brand, pages, seeds), invoke=invoke)
    domain = own_domain(brand.get("domain") or "")
    suggestions = finalize(result.payload, pages, domain=domain, seeds=seeds)
    notes = [f"page text: {n}" for n in result.payload.get("injection_notes") or []]
    for p in pages:
        for sig in ev.injection_signals(p.get("text", "")):
            notes.append(f"{p['url']}: instruction-like text ({sig})")
    return {"suggestions": suggestions, "usage": result.usage, "notes": notes}

"""Deterministic QA over a PR draft (blueprint §7, §8, §9, §12).

Pure functions, no model, no I/O. Each check returns a list of
:class:`Finding`; :func:`run_qa` collects them into a :class:`QAReport`.

Severity is deliberate:

- ``error``   — blocks hand-back as "clean": compliance language, unsourced
                numbers, missing pending markers, boilerplate drift, required
                structure, hard lengths from the anatomy.
- ``warning`` — editorial: the AP pass, read-aloud sentence length, a short
                lede, SEO placement. Conservative on purpose: a check that
                cries wolf teaches reviewers to ignore the panel, so each AP
                rule matches only shapes that are wrong almost every time.

Drafts are structured (see render.py); inline citation markers like
``[ev-3]`` sit at the end of the sentence they support, which is what lets
the number and superlative checks work per sentence rather than per block.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date

from pr_writer_agent import evidence as ev
from pr_writer_agent import render
from pr_writer_agent.content_types import ContentType


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str  # "error" | "warning"
    message: str
    where: str = ""
    excerpt: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QAReport:
    findings: list[Finding] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"passed": self.passed, "counts": self.counts,
                "findings": [f.to_dict() for f in self.findings]}


def _e(check, message, where="", excerpt="") -> Finding:
    return Finding(check, "error", message, where, excerpt[:160])


def _w(check, message, where="", excerpt="") -> Finding:
    return Finding(check, "warning", message, where, excerpt[:160])


# --------------------------------------------------------------------------- #
# Text utilities
# --------------------------------------------------------------------------- #

VERIFY = re.compile(r"\[VERIFY(?::[^\]]*)?\]")
# The speaker inside the marker may itself be a placeholder ("[VERIFY: exact
# title]"), so one level of nested brackets is part of the marker, not its end.
QUOTE_PENDING = re.compile(r"\[QUOTE\s*[–—-]\s*PENDING APPROVAL:\s*((?:[^\[\]]|\[[^\[\]]*\])+)\]")
_URL = re.compile(r"https?://\S+|www\.\S+")

# Periods that do not end a sentence. Months, titles and "No." never end
# one; a state, "Inc." or "a.m." ends one only when a capitalized word follows
# ("...Woodland Hills, Calif. The team...") — except a time zone after a.m./p.m.
_ALWAYS = "Jan. Feb. Mar. Apr. Aug. Sep. Sept. Oct. Nov. Dec. Mr. Mrs. Ms. Dr. Jr. Sr. St. No. vs. e.g. i.e.".split()
_ENDABLE = (
    "Ave. Blvd. Inc. Corp. Co. Ltd. Bros. etc. a.m. p.m. U.S. U.K. Ph.D. "
    "Ala. Ariz. Ark. Calif. Colo. Conn. Del. Fla. Ga. Ill. Ind. Kan. Ky. La. Md. Mass. Mich. "
    "Minn. Miss. Mo. Mont. Neb. Nev. N.H. N.J. N.M. N.Y. N.C. N.D. Okla. Ore. Pa. R.I. S.C. "
    "S.D. Tenn. Vt. Va. Wash. W.Va. Wis. Wyo."
).split()


def _alts(words: list[str]) -> str:
    return "|".join(re.escape(a) for a in sorted(words, key=len, reverse=True))


_ALWAYS_RE = re.compile(rf"(?<![A-Za-z.])(?:{_alts(_ALWAYS)})")
_ENDABLE_RE = re.compile(
    rf"(?<![A-Za-z.])(?:{_alts(_ENDABLE)})"
    r"(?:(?=\s+(?:[ECMP][SD]?T|AKS?T|HST|UTC|GMT)\b)|(?!\s+[A-Z\"“]))")
_HIDDEN_DOT = "\u2024"  # one-dot leader: stands in for a protected period while splitting
_SPLIT = re.compile(r"(?<=[.!?])[\"”’']?\s+(?=[A-Z0-9\"“($])")
_TRAILING_CITE = re.compile(r"([.!?])([\"”’']?)\s*(\[\s*(?:br|bc|ev|rv)-\d+(?:\s*,\s*(?:br|bc|ev|rv)-\d+)*\s*\])")


def sentences(text: str) -> list[str]:
    """Split prose into sentences without breaking on AP abbreviations. A
    citation marker written after the period is moved inside it first, so it
    stays with the sentence it supports."""
    t = _TRAILING_CITE.sub(lambda m: f" {m.group(3)}{m.group(1)}{m.group(2)}", text or "")
    hide = lambda m: m.group(0).replace(".", _HIDDEN_DOT)
    t = _ENDABLE_RE.sub(hide, _ALWAYS_RE.sub(hide, t))
    parts = []
    for para in re.split(r"\n\s*\n|\n", t):
        parts.extend(p for p in _SPLIT.split(para) if p.strip())
    return [p.replace(_HIDDEN_DOT, ".").strip() for p in parts]


def scan_text(text: str) -> str:
    """Prose as a reader sees it: no cite markers, placeholders or URLs."""
    t = ev.strip_cites(text or "")
    t = VERIFY.sub(" ", t)
    t = QUOTE_PENDING.sub(" ", t)
    return _URL.sub(" ", t)


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$%][\w$%'’.,/:-]*", scan_text(text)))


def _body_blocks(draft: dict) -> list[dict]:
    return [b for b in draft.get("blocks", []) if b.get("kind") != "bio"]


# --------------------------------------------------------------------------- #
# Anatomy: headline, subhead, lede, length, release line, dateline
# --------------------------------------------------------------------------- #

def check_headline(headline: str, ct: ContentType) -> list[Finding]:
    if not ct.headline_chars:
        return [] if (headline or "").strip() else [_e("headline", "title is missing", "headline")]
    n, (lo, hi) = len((headline or "").strip()), ct.headline_chars
    if not lo <= n <= hi:
        return [_e("headline_length", f"headline is {n} characters; the anatomy calls for {lo}–{hi}",
                   "headline", headline)]
    return []


def check_subhead(subhead: str, ct: ContentType) -> list[Finding]:
    if not ct.subhead_chars:
        return []
    n, (lo, hi) = len((subhead or "").strip()), ct.subhead_chars
    if not lo <= n <= hi:
        return [_e("subhead_length", f"subheadline is {n} characters; the anatomy calls for {lo}–{hi}",
                   "subhead", subhead)]
    return []


def check_lede(draft: dict, ct: ContentType) -> list[Finding]:
    if not ct.lede_words:
        return []
    lede = next((b for b in draft.get("blocks", []) if b.get("kind") == "lede"), None)
    if not lede:
        return []  # check_structure reports the missing part
    n, (lo, hi) = word_count(lede.get("text", "")), ct.lede_words
    if n > hi:
        return [_e("lede_length", f"lede is {n} words; keep it to {hi} or fewer", lede.get("id", "lede"),
                   lede.get("text", ""))]
    if n < lo:
        return [_w("lede_length", f"lede is {n} words; {lo}–{hi} usually covers who/what/when/where/why",
                   lede.get("id", "lede"), lede.get("text", ""))]
    return []


def check_body_length(draft: dict, ct: ContentType) -> list[Finding]:
    n = sum(word_count(b.get("text", "")) for b in _body_blocks(draft))
    lo, hi = ct.words
    if not lo <= n <= hi:
        return [_e("body_length", f"body is {n} words; a {ct.label.lower()} runs {lo}–{hi}", "body")]
    return []


_AP_DATE = r"(?:Jan\.|Feb\.|March|April|May|June|July|Aug\.|Sept\.|Oct\.|Nov\.|Dec\.) \d{1,2}, \d{4}"
_AP_TIME = r"(?:\d{1,2}(?::[0-5]\d)? (?:a\.m\.|p\.m\.)|noon|midnight)"
_RELEASE = re.compile(
    rf"^(?:FOR IMMEDIATE RELEASE|MEDIA ADVISORY|EMBARGOED UNTIL {_AP_TIME} [A-Z]{{2,4}}, {_AP_DATE})$")


def check_release_line(line: str, ct: ContentType) -> list[Finding]:
    if not ct.release_line:
        return []
    line = (line or "").strip()
    if VERIFY.search(line):
        return []
    if not _RELEASE.match(line):
        return [_e("release_line", "release line must be FOR IMMEDIATE RELEASE, MEDIA ADVISORY or "
                   "EMBARGOED UNTIL <time> <zone>, <AP date> (e.g. 9 a.m. EST, Nov. 12, 2026)",
                   "release_line", line)]
    return []


AP_STATES = frozenset(
    "Ala. Ariz. Ark. Calif. Colo. Conn. Del. Fla. Ga. Ill. Ind. Kan. Ky. La. Md. Mass. Mich. Minn. "
    "Miss. Mo. Mont. Neb. Nev. N.H. N.J. N.M. N.Y. N.C. N.D. Okla. Ore. Pa. R.I. S.C. S.D. Tenn. "
    "Vt. Va. Wash. W.Va. Wis. Wyo. Alaska Hawaii Idaho Iowa Maine Ohio Texas Utah".split()
)
AP_STANDALONE = frozenset({
    "ATLANTA", "BALTIMORE", "BOSTON", "CHICAGO", "CINCINNATI", "CLEVELAND", "DALLAS", "DENVER",
    "DETROIT", "HONOLULU", "HOUSTON", "INDIANAPOLIS", "LAS VEGAS", "LOS ANGELES", "MIAMI",
    "MILWAUKEE", "MINNEAPOLIS", "NEW ORLEANS", "NEW YORK", "OKLAHOMA CITY", "PHILADELPHIA",
    "PHOENIX", "PITTSBURGH", "ST. LOUIS", "SALT LAKE CITY", "SAN ANTONIO", "SAN DIEGO",
    "SAN FRANCISCO", "SEATTLE", "WASHINGTON",
})
_DATELINE = re.compile(rf"^(?P<city>[A-Z][A-Z .'-]*?)(?:, (?P<state>[A-Z][A-Za-z.]*))? — (?P<date>.+?) —$")


def check_dateline(text: str, ct: ContentType) -> list[Finding]:
    if not ct.dateline:
        return []
    text = (text or "").strip()
    if VERIFY.search(text):
        return []  # counted as an open [VERIFY]
    m = _DATELINE.match(text)
    if not m:
        return [_e("dateline", "dateline must read CITY, St. — Month Day, Year — "
                   "(e.g. WOODLAND HILLS, Calif. — Nov. 12, 2026 —)", "dateline", text)]
    out = []
    city, state = m.group("city").strip(), m.group("state")
    if state and state not in AP_STATES:
        out.append(_e("dateline", f"{state!r} is not an AP state abbreviation", "dateline", text))
    if not state and city not in AP_STANDALONE:
        out.append(_e("dateline", f"{city} is not an AP standalone city; add the state abbreviation",
                      "dateline", text))
    if not re.fullmatch(_AP_DATE, m.group("date")):
        out.append(_e("dateline", "dateline date must be AP form (Nov. 12, 2026; March 3, 2026)",
                      "dateline", text))
    return out


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #

_TZ = re.compile(r"\b(?:[ECMP][SD]?T|AKS?T|HST|UTC|GMT)\b")


def check_structure(draft: dict, ct: ContentType) -> list[Finding]:
    out: list[Finding] = []
    blocks = draft.get("blocks", [])
    kinds = [b.get("kind") for b in blocks]
    allowed = set(ct.allowed_kinds())
    for b in blocks:
        if b.get("kind") not in allowed:
            out.append(_e("structure", f"block kind {b.get('kind')!r} does not belong in a {ct.label.lower()}",
                          b.get("id", "")))
        if not (b.get("text") or "").strip():
            out.append(_e("structure", "block is empty", b.get("id", "")))
    for part in ct.parts:
        if part not in kinds:
            out.append(_e("structure", f"missing required part: {part}", "body"))

    speakers = {s["id"]: s for s in draft.get("speakers", [])}
    quotes = [b for b in blocks if b.get("kind") == "quote"]
    lo, hi = ct.quotes
    if not lo <= len(quotes) <= hi:
        out.append(_e("quote_count", f"{len(quotes)} quote(s); a {ct.label.lower()} carries {lo}–{hi}", "body"))
    for q in quotes:
        if q.get("speaker_id") not in speakers:
            out.append(_e("quote_speaker", f"quote has no known speaker ({q.get('speaker_id')!r})", q.get("id", "")))
        n = word_count(q.get("text", ""))
        if not 30 <= n <= 60:
            out.append(_w("quote_length", f"quote is {n} words; 30–60 sounds spoken", q.get("id", "")))
    if "quotes_from_both_parties" in ct.extra_rules:
        orgs = {speakers[q["speaker_id"]].get("organization", "").lower()
                for q in quotes if q.get("speaker_id") in speakers}
        if len(orgs) < 2:
            out.append(_e("quote_count", "a partnership needs a quote from each party", "body"))

    for part in ct.url_parts:
        for b in (b for b in blocks if b.get("kind") == part):
            if not (ev.urls_in(b.get("text", "")) or VERIFY.search(b.get("text", ""))):
                out.append(_e("structure", f"the {part} block needs a URL (or a [VERIFY] placeholder)", b.get("id", "")))
    for rule, kind in (("event_has_timezone", "event_details"), ("when_has_timezone", "when")):
        if rule in ct.extra_rules:
            for b in (b for b in blocks if b.get("kind") == kind):
                if not (_TZ.search(b.get("text", "")) or VERIFY.search(b.get("text", ""))):
                    out.append(_e("structure", "date and time need a time zone (e.g. 2 p.m. EST)", b.get("id", "")))
    return out


# --------------------------------------------------------------------------- #
# Banned and restricted language (blueprint §7 table + §8 guardrails)
# --------------------------------------------------------------------------- #

# (pattern, category, message). Superlatives are the one family a third-party
# citation in the same sentence makes acceptable.
HYPE = (
    (r"\brevolution(?:ary|ize|izes|izing)\b", "hype", "hype word; name the capability or result"),
    (r"\bgame[- ]chang(?:ing|er|ers)\b", "hype", "hype word; name the capability or result"),
    (r"\bcutting[- ]edge\b", "hype", "hype word; name the capability or result"),
    (r"\bbest[- ]in[- ]class\b", "hype", "hype word; name the capability or result"),
)
SUPERLATIVES = (
    r"\bfirst[- ](?:ever|of[- ]its[- ]kind)\b",
    r"\bfirst to\b",
    r"\b(?:the|its|our|industry'?s|nation'?s|world'?s|market'?s|country'?s)\s+first\s+(?!(?:quarter|half|time|step|steps|phase|round|day|week|month|year|reference|name|of)\b)[a-z]",
    r"\b(?:the|its|our)\s+only\s+(?!(?:way|thing|time|one|when|if)\b)[a-z]+\s+(?:to|that|which|in|on|for|with)\b",
    r"\b(?:a|the|industry|market|world|nation|category)[- ]leading\b",
    r"\bleading (?:provider|platform|company|firm|vendor|solution|supplier|expert|authority)\b",
    r"\bNo\. ?1\b|\bnumber one\b|#1\b",
    r"\b(?:the|our|its)\s+best\s+(?!practices?\b|way\b|interests?\b)[a-z]",
)
RESTRICTED = (
    (r"\bguarantee(?:s|d|ing)?\b", "legal", "outcome promise; bar advertising rules — use \"helps firms\" / \"designed to\""),
    (r"\bensures? results\b", "legal", "outcome promise; use \"helps firms\" / \"designed to\""),
    (r"\b(?:wins?|winning|won) (?:more )?(?:cases|lawsuits|settlements|verdicts)\b", "legal", "case-outcome claim; bar advertising rules"),
    (r"\bcures?\b|\bcured\b", "healthcare", "clinical claim; describe the administrative or workflow benefit"),
    (r"\bdiagnos(?:e|es|ed|ing)\b", "healthcare", "clinical claim; describe the administrative or workflow benefit"),
    (r"\b(?:clinically|medically) (?:proven|validated|tested)\b", "healthcare", "clinical efficacy claim"),
    (r"\bfully autonomous\b", "ai", "autonomy claim; say \"AI-assisted, with human review\""),
    (r"\breplac(?:e|es|ed|ing) (?:lawyers|attorneys|paralegals|legal professionals|doctors|physicians|clinicians)\b",
     "ai", "inaccurate and ethically sensitive; say \"AI-assisted, with human review\""),
    (r"\bno human (?:review|oversight|involvement)\b", "ai", "contradicts the human-review guardrail"),
    (r"\b100% accura(?:te|cy)\b|\berror[- ]free\b", "ai", "accuracy claim without method"),
)
_ANNOUNCE = re.compile(r"\b(?:excited|pleased|thrilled|proud|delighted|happy) to (?:announce|share|unveil)\b", re.I)


def _third_party_cited(sentence: str, ev_index: dict) -> bool:
    return any(ev_index.get(i, {}).get("third_party") for i in ev.cites_in(sentence))


def check_language(draft: dict, ev_index: dict, avoid: list[str] | None = None) -> list[Finding]:
    out: list[Finding] = []
    pieces = [("headline", draft.get("headline", "")), ("subhead", draft.get("subhead", ""))]
    pieces += [(b.get("id", ""), b.get("text", "")) for b in draft.get("blocks", [])]
    avoid_res = [(re.compile(rf"\b{re.escape(a.strip())}\b", re.I), a.strip()) for a in (avoid or []) if a and a.strip()]
    for where, text in pieces:
        kind = next((b.get("kind") for b in draft.get("blocks", []) if b.get("id") == where), where)
        for sent in sentences(text):
            plain = scan_text(sent)
            low = plain.lower()
            for pat, cat, msg in HYPE + RESTRICTED:
                m = re.search(pat, low)
                if m:
                    out.append(_e(f"language_{cat}", f"\"{m.group(0)}\": {msg}", where, plain))
            for pat in SUPERLATIVES:
                m = re.search(pat, plain, re.I)
                if m and not _third_party_cited(sent, ev_index):
                    out.append(_e("language_superlative",
                                  f"\"{m.group(0).strip()}\" needs a cited third-party source in the same sentence",
                                  where, plain))
            m = _ANNOUNCE.search(plain)
            if m:
                make = _e if kind in ("lede", "headline", "subhead") else _w
                out.append(make("language_announce", f"\"{m.group(0)}\" wastes words; lead with the news", where, plain))
            for rx, term in avoid_res:
                if rx.search(plain):
                    out.append(_e("language_brand_avoid", f"\"{term}\" is on the brand card's avoid list", where, plain))
    return out


# --------------------------------------------------------------------------- #
# Grounding: numbers, URLs
# --------------------------------------------------------------------------- #

_NUMERIC = re.compile(r"[$%]|\d")
_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Names that carry digits but are not figures (rule and section references).
_NAMED_DIGITS = re.compile(r"\b(?:Model Rules?|Rules?|Section|Title|Chapter|COVID)[- ]?\d[\d.\-–]*", re.I)


def _numeric_sentence(sentence: str) -> bool:
    return bool(_NUMERIC.search(_NAMED_DIGITS.sub(" ", scan_text(sentence))))


def check_numbers(draft: dict, ev_index: dict) -> list[Finding]:
    """Every sentence carrying a number, % or $ cites an evidence id that
    exists, or the figure sits inside a [VERIFY] placeholder. Headline and
    subhead figures must appear in a cited body sentence."""
    out: list[Finding] = []
    cited_numbers: set[str] = set()
    for b in draft.get("blocks", []):
        for sent in sentences(b.get("text", "")):
            ids = ev.cites_in(sent)
            unknown = [i for i in ids if i not in ev_index]
            if unknown:
                out.append(_e("citation_unknown", f"cites {', '.join(unknown)}, which is not in the evidence list",
                              b.get("id", ""), sent))
            if not _numeric_sentence(sent):
                continue
            if any(i in ev_index for i in ids):
                cited_numbers.update(n.replace(",", "") for n in _NUM_TOKEN.findall(scan_text(sent)))
            else:
                out.append(_e("uncited_number", "figure without an evidence citation or [VERIFY]",
                              b.get("id", ""), scan_text(sent).strip()))
    for where in ("headline", "subhead"):
        for n in _NUM_TOKEN.findall(_NAMED_DIGITS.sub(" ", scan_text(draft.get(where, "")))):
            if n.replace(",", "") not in cited_numbers:
                out.append(_e("uncited_number", f"{n} in the {where} is not backed by a cited body sentence",
                              where, draft.get(where, "")))
    return out


def _url_ok(url: str, allowed: list[str]) -> bool:
    norm = lambda u: re.sub(r"^https?://(?:www\.)?|^www\.", "", u.lower()).rstrip("/")
    u = norm(url)
    return any(u == norm(a) or u.startswith(norm(a) + "/") for a in allowed)


def check_urls(draft: dict, allowed: list[str]) -> list[Finding]:
    out = []
    for b in draft.get("blocks", []):
        for url in ev.urls_in(b.get("text", "")):
            if not _url_ok(url, allowed):
                out.append(_e("url_unsourced", f"{url} is not from the brief, brand card or evidence",
                              b.get("id", ""), url))
    return out


# --------------------------------------------------------------------------- #
# Read-aloud and the light AP pass
# --------------------------------------------------------------------------- #

def check_long_sentences(draft: dict, ct: ContentType, limit: int = 30) -> list[Finding]:
    out = []
    for b in draft.get("blocks", []):
        if b.get("kind") == "lede" and ct.lede_words:
            continue  # the lede's own 25–35 range governs it
        for sent in sentences(b.get("text", "")):
            n = word_count(sent)
            if n > limit:
                out.append(_w("long_sentence", f"{n}-word sentence; read-aloud limit is {limit}", b.get("id", ""),
                              scan_text(sent).strip()))
    return out


_FULL_MONTH_WITH_DAY = re.compile(r"\b(January|February|August|September|October|November|December) \d{1,2}\b")
_ABBR_SPRING = re.compile(r"\b(Mar|Apr|Jun|Jul)\.? \d{1,2}\b")
_SEP = re.compile(r"\bSep\.? \d{1,2}\b")
_DATE_ORDINAL = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)\b")
_NOON = re.compile(r"\b12(?::00)? ?p\.?m\.?(?![a-z])", re.I)
_MIDNIGHT = re.compile(r"\b12(?::00)? ?a\.?m\.?(?![a-z])", re.I)
_AMPM = re.compile(r"\b(?!12\b)\d{1,2}(?::\d{2})? ?(?:AM|PM|am|pm|A\.M\.|P\.M\.)(?![A-Za-z.])")
_ZERO_MIN = re.compile(r"\b\d{1,2}:00 ?[ap]\.m\.")
_PERCENT_WORD = re.compile(r"\d ?(?:percent|per cent)\b", re.I)
_PERCENT_SPACE = re.compile(r"\d +%")
_SMALL_NUMERAL = re.compile(
    r"(?<![\d$.,:/#-])\b[1-9] (?:new )?(?:offices|employees|states|firms|clients|customers|locations|"
    r"partners|products|features|speakers|sessions|countries|cities|team members)\b")
_ABBR_FOR = {"January": "Jan.", "February": "Feb.", "August": "Aug.", "September": "Sept.",
             "October": "Oct.", "November": "Nov.", "December": "Dec."}
_FULL_FOR = {"Mar": "March", "Apr": "April", "Jun": "June", "Jul": "July"}


def ap_findings(text: str, where: str) -> list[Finding]:
    plain = scan_text(text)
    out = []
    for m in _FULL_MONTH_WITH_DAY.finditer(plain):
        out.append(_w("ap_month", f"{m.group(0)} → {_ABBR_FOR[m.group(1)]} with a specific date", where, m.group(0)))
    for m in _ABBR_SPRING.finditer(plain):
        out.append(_w("ap_month", f"{m.group(0)} → spell out {_FULL_FOR[m.group(1)]}", where, m.group(0)))
    for m in _SEP.finditer(plain):
        out.append(_w("ap_month", f"{m.group(0)} → Sept.", where, m.group(0)))
    for m in _DATE_ORDINAL.finditer(plain):
        out.append(_w("ap_ordinal", f"{m.group(0)} → no ordinal in dates", where, m.group(0)))
    for m in _NOON.finditer(plain):
        out.append(_w("ap_time", f"{m.group(0)} → noon", where, m.group(0)))
    for m in _MIDNIGHT.finditer(plain):
        out.append(_w("ap_time", f"{m.group(0)} → midnight", where, m.group(0)))
    for m in _AMPM.finditer(plain):
        out.append(_w("ap_time", f"{m.group(0)} → a.m./p.m. lowercase with periods", where, m.group(0)))
    for m in _ZERO_MIN.finditer(plain):
        out.append(_w("ap_time", f"{m.group(0)} → drop :00 (9 a.m.)", where, m.group(0)))
    for m in _PERCENT_WORD.finditer(plain):
        out.append(_w("ap_percent", f"\"{m.group(0)}\" → use the % sign (5%)", where, m.group(0)))
    for m in _PERCENT_SPACE.finditer(plain):
        out.append(_w("ap_percent", f"\"{m.group(0)}\" → no space before %", where, m.group(0)))
    for m in _SMALL_NUMERAL.finditer(plain):
        out.append(_w("ap_numbers", f"\"{m.group(0)}\" → spell out one through nine", where, m.group(0)))
    return out


def check_ap(draft: dict) -> list[Finding]:
    out = ap_findings(draft.get("headline", ""), "headline") + ap_findings(draft.get("subhead", ""), "subhead")
    for b in draft.get("blocks", []):
        out += ap_findings(b.get("text", ""), b.get("id", ""))
    return out


# --------------------------------------------------------------------------- #
# Rendered-text checks — run on markdown so a reviewer's hand edit is caught too
# --------------------------------------------------------------------------- #

_DIRECT_QUOTE = re.compile(r"[\"“][^\"”]{15,}[\"”]\s*,?\s*(?:said|says)\b")


def check_quote_markers(markdown: str, ct: ContentType) -> list[Finding]:
    """Every paragraph carrying a direct, attributed quote is followed by a
    pending marker naming the same speaker."""
    if ct.quotes[1] == 0:
        return []
    out = []
    for para in re.split(r"\n\s*\n", markdown or ""):
        if not _DIRECT_QUOTE.search(para):
            continue
        m = QUOTE_PENDING.search(para)
        if not m:
            out.append(_e("quote_pending_marker", "quote has no [QUOTE – PENDING APPROVAL: Name, Title] marker",
                          "draft", para))
            continue
        name = m.group(1).split(",")[0].strip()
        attribution = para[: m.start()]
        if name and name not in attribution:
            out.append(_e("quote_pending_marker", f"marker names {name!r}, who is not the quoted speaker",
                          "draft", para))
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def check_boilerplate(markdown: str, expected: list[dict], *, approved_at: str = "",
                      today: date | None = None) -> list[Finding]:
    out = []
    md = _norm(markdown)
    for bp in expected:
        if VERIFY.search(bp["text"]):
            out.append(_e("boilerplate", f"no approved boilerplate on file for {bp['brand']}", "boilerplate"))
            continue
        if _norm(f"About {bp['brand']} {bp['text']}") not in md:
            out.append(_e("boilerplate", f"\"About {bp['brand']}\" does not match the approved text verbatim",
                          "boilerplate"))
    if expected and today and not VERIFY.search(expected[0]["text"]):
        stale = ev.is_stale(approved_at, today, months=12)
        if stale is None:
            out.append(_w("boilerplate_current", "boilerplate has no approval date on the brand card", "boilerplate"))
        elif stale:
            out.append(_w("boilerplate_current", f"boilerplate last approved {approved_at}; over 12 months old",
                          "boilerplate"))
    return out


# --------------------------------------------------------------------------- #
# Byline and SEO placement
# --------------------------------------------------------------------------- #

_FIRST_PERSON = re.compile(r"\b(?:I|I'm|I've|I'd|my|me|we|we're|we've|our|us)\b")


def check_byline(draft: dict, ct: ContentType, brand_names: list[str]) -> list[Finding]:
    if not ct.first_person:
        return []
    out = []
    body = " ".join(scan_text(b.get("text", "")) for b in _body_blocks(draft))
    if not _FIRST_PERSON.search(body):
        out.append(_e("byline_voice", "a byline is written in the executive's first person", "body"))
    names = [n for n in dict.fromkeys(brand_names) if n]
    rx = re.compile("|".join(rf"\b{re.escape(n)}\b" for n in sorted(names, key=len, reverse=True))) if names else None
    if rx:
        in_body = len(rx.findall(body + " " + draft.get("headline", "")))
        if in_body:
            out.append(_e("byline_company", f"company named {in_body} time(s) outside the bio; bylines name it "
                          "only in the bio", "body"))
        bio = " ".join(scan_text(b.get("text", "")) for b in draft.get("blocks", []) if b.get("kind") == "bio")
        if len(rx.findall(bio)) > (ct.company_mentions_max or 1):
            out.append(_e("byline_company", "company named more than once in the bio", "bio"))
    return out


def check_keyword(draft: dict, primary: str | None) -> list[Finding]:
    if not primary:
        return []
    kw = primary.lower().strip()
    out = []
    if kw not in (draft.get("headline", "") + " " + draft.get("subhead", "")).lower():
        out.append(_w("keyword_placement", f"primary keyword \"{primary}\" is not in the headline or subheadline", "headline"))
    words = " ".join(scan_text(b.get("text", "")) for b in _body_blocks(draft)).split()
    if kw not in " ".join(words[:100]).lower():
        out.append(_w("keyword_placement", f"primary keyword \"{primary}\" is not in the first 100 words", "body"))
    if words:
        hits = " ".join(words).lower().count(kw)
        density = hits * len(kw.split()) / len(words)
        if density > 0.02:
            out.append(_w("keyword_density", f"primary keyword density {density:.1%}; keep it at or under 2%", "body"))
    return out


# --------------------------------------------------------------------------- #
# Package checks (SEO block, snippets)
# --------------------------------------------------------------------------- #

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){2,5}$")


def check_seo_block(seo: dict) -> list[Finding]:
    out = []
    n = len(seo.get("seo_title", ""))
    if not 0 < n < 60:
        out.append(_w("seo_title", f"SEO title is {n} characters; keep it under 60", "seo"))
    n = len(seo.get("meta_description", ""))
    if not 150 <= n <= 160:
        out.append(_w("seo_meta", f"meta description is {n} characters; aim for 150–160", "seo"))
    if not _SLUG.match(seo.get("slug", "")):
        out.append(_w("seo_slug", "slug should be 3–6 lowercase hyphenated words", "seo", seo.get("slug", "")))
    n = len(seo.get("social_summary", ""))
    if n > 280:
        out.append(_e("seo_social", f"social summary is {n} characters; the limit is 280", "seo"))
    return out


def check_snippets(snippets: dict, draft_markdown: str) -> list[Finding]:
    """Snippets may not introduce figures the draft does not carry."""
    out = []
    in_draft = {n.replace(",", "") for n in _NUM_TOKEN.findall(scan_text(draft_markdown))}
    for key, text in snippets.items():
        if not isinstance(text, str):
            continue
        for n in _NUM_TOKEN.findall(scan_text(text)):
            if n.replace(",", "") not in in_draft:
                out.append(_e("snippet_new_figure", f"{n} is not in the draft", key, text))
        low = scan_text(text).lower()
        for pat, cat, msg in HYPE + RESTRICTED:
            m = re.search(pat, low)
            if m:
                out.append(_e(f"language_{cat}", f"\"{m.group(0)}\": {msg}", key, text))
    if len(snippets.get("social_summary", "")) > 280:
        out.append(_e("snippet_length", "social summary is over 280 characters", "social_summary"))
    return out


# --------------------------------------------------------------------------- #
# The whole pass
# --------------------------------------------------------------------------- #

def run_qa(draft: dict, ct: ContentType, *, card: dict, evidence: list[dict],
           primary_keyword: str | None = None, allowed_urls: list[str] | None = None,
           today: date | None = None) -> QAReport:
    card = ev.normalize_card(card)
    ev_index = ev.index(evidence)
    markdown = render.render_markdown(draft, ct)
    findings: list[Finding] = []
    findings += check_release_line(draft.get("release_line", ""), ct)
    findings += check_headline(draft.get("headline", ""), ct)
    findings += check_subhead(draft.get("subhead", ""), ct)
    findings += check_dateline(draft.get("dateline", ""), ct)
    findings += check_lede(draft, ct)
    findings += check_body_length(draft, ct)
    findings += check_structure(draft, ct)
    findings += check_language(draft, ev_index, card["avoid"])
    findings += check_numbers(draft, ev_index)
    if allowed_urls is not None:
        findings += check_urls(draft, allowed_urls)
    findings += check_long_sentences(draft, ct)
    findings += check_ap(draft)
    findings += check_quote_markers(markdown, ct)
    findings += check_boilerplate(markdown, draft.get("boilerplates", []),
                                  approved_at=str((card["boilerplate"] or {}).get("approved_at") or ""),
                                  today=today)
    findings += check_byline(draft, ct, [card["short_name"], card["legal_name"]])
    findings += check_keyword(draft, primary_keyword)
    counts = {
        "body_words": sum(word_count(b.get("text", "")) for b in _body_blocks(draft)),
        "verify_open": len(VERIFY.findall(markdown)),
        "quotes_pending": len(QUOTE_PENDING.findall(markdown)),
        "ap_warnings": sum(1 for f in findings if f.check.startswith("ap_")),
        "errors": sum(1 for f in findings if f.severity == "error"),
        "warnings": sum(1 for f in findings if f.severity == "warning"),
    }
    return QAReport(findings=findings, counts=counts)


# Findings a model rewrite can fix without new facts. Everything else (missing
# evidence, boilerplate, structure the brief cannot fill) goes to reviewer
# notes, not to another model call.
FIXABLE = frozenset({
    "headline_length", "subhead_length", "lede_length", "body_length", "long_sentence",
    "language_hype", "language_superlative", "language_legal", "language_healthcare",
    "language_ai", "language_announce", "language_brand_avoid", "ap_month", "ap_ordinal",
    "ap_time", "ap_percent", "ap_numbers", "quote_length", "keyword_placement", "byline_voice",
    "byline_company",
})

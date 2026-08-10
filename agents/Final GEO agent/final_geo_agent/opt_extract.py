"""Layer 2 — content extraction & structural measurement. HTML in, CleanDoc out.

Extraction quality is the single biggest determinant of recommendation
quality: boilerplate that survives here poisons every downstream layer.
Pipeline per document: trafilatura first, text-density fallback when it comes
back thin; JSON-LD (Recipe/FAQPage/HowTo) parsed separately and merged back;
comment sections cut by class heuristics; interstitial pages and language
mismatches flagged unusable instead of poisoning the corpus.

No network here — callers hand us HTML strings, so everything is offline-testable.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import trafilatura
from lxml import html as lxml_html
from pydantic import BaseModel, Field

from .opt_config import ExtractCfg

_QUESTION_RE = re.compile(
    r"^(how|what|why|when|where|which|who|can|could|should|do|does|did|is|are|will)\b",
    re.IGNORECASE,
)
# Numbers with their units/ratios kept whole: '1:8', '24 hours', '200g', '70°F', '3.5%'
_NUMERIC_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?::\s*\d+|°\s*[cf]\b|%|g\b|kg\b|ml\b|l\b|oz\b|lbs?\b|"
    r"hours?\b|hrs?\b|minutes?\b|mins?\b|days?\b|weeks?\b|cups?\b|tbsp\b|tsp\b)?",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s|$)")

_BOILERPLATE_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form", "noscript", "iframe", "svg")

# Tiny function-word sets for language sanity checks (hreflang-mismatch guard).
_LANG_MARKERS: dict[str, set[str]] = {
    "en": {"the", "and", "of", "to", "in", "is", "for", "with", "you", "your", "it", "that"},
    "es": {"el", "la", "los", "las", "de", "que", "en", "una", "por", "para", "con", "es"},
    "de": {"der", "die", "das", "und", "ist", "mit", "für", "ein", "eine", "nicht", "auf", "sie"},
    "fr": {"le", "la", "les", "des", "est", "pour", "avec", "une", "que", "vous", "dans", "pas"},
}


class Section(BaseModel):
    heading: str = ""
    level: int = 0          # 0 = pre-heading intro
    text: str = ""


class CleanDoc(BaseModel):
    url: str = ""
    lang: str = ""
    text: str = ""
    sections: list[Section] = []
    features: dict[str, float] = {}
    schema_types: list[str] = []
    flags: list[str] = []          # thin | interstitial | language_mismatch | truncated | density_fallback
    usable: bool = True            # False -> excluded from term/structure profiling

    @property
    def word_count(self) -> int:
        return int(self.features.get("word_count", 0))


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.") if url else ""


def _link_density(el) -> float:
    total = len(" ".join(el.itertext()).strip())
    if total == 0:
        return 1.0
    linked = sum(len(" ".join(a.itertext()).strip()) for a in el.iter("a"))
    return min(1.0, linked / total)


def _drop_boilerplate(root, cfg: ExtractCfg) -> None:
    for tag in _BOILERPLATE_TAGS:
        for el in root.findall(f".//{tag}"):
            el.getparent().remove(el)
    # comment sections: any element whose id/class carries a comment-ish token
    for el in list(root.iter()):
        marker = f"{el.get('id', '')} {el.get('class', '')}".lower()
        if any(tok in marker for tok in cfg.comment_class_tokens):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)


def _density_extract(root, cfg: ExtractCfg) -> str:
    """Fallback extractor: keep long, link-sparse blocks in document order."""
    kept: list[str] = []
    for el in root.iter("p", "li", "td", "div", "blockquote", "pre"):
        if el.tag == "div" and any(child.tag in ("p", "div") for child in el):
            continue  # container div — its leaves will be visited on their own
        text = " ".join(" ".join(el.itertext()).split())
        if len(text) < cfg.min_block_chars:
            continue
        if _link_density(el) > cfg.link_density_max:
            continue
        kept.append(text)
    return "\n".join(dict.fromkeys(kept))  # de-dupe nested repeats, keep order


def _jsonld_extract(root) -> tuple[list[str], str]:
    """Schema types + text content stored only in JSON-LD (Recipe/FAQ/HowTo)."""
    types: list[str] = []
    texts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        node_type = node.get("@type", "")
        for t in node_type if isinstance(node_type, list) else [node_type]:
            if t and t not in types:
                types.append(t)
        for key in ("name", "text", "description", "recipeInstructions", "recipeIngredient", "step", "itemListElement", "mainEntity", "acceptedAnswer", "@graph"):
            value = node.get(key)
            if isinstance(value, str) and len(value) > 15:
                texts.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and len(item) > 15:
                        texts.append(item)
                    else:
                        walk(item)
            elif isinstance(value, dict):
                walk(value)

    # note: scripts are read from the ORIGINAL tree before boilerplate stripping
    for script in root.findall('.//script[@type="application/ld+json"]'):
        try:
            walk(json.loads(script.text or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return types, "\n".join(texts)


def detect_language(text: str) -> str:
    """Cheap function-word-ratio detector — enough to catch hreflang mismatches."""
    words = re.findall(r"[a-zäöüéèàçñ]+", text.lower())[:400]
    if not words:
        return ""
    best_lang, best_hits = "", 0
    for lang, markers in _LANG_MARKERS.items():
        hits = sum(1 for w in words if w in markers)
        if hits > best_hits:
            best_lang, best_hits = lang, hits
    return best_lang if best_hits >= max(3, len(words) // 50) else ""


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _measure_features(root, text: str, base_url: str, cfg: ExtractCfg) -> dict[str, float]:
    words = text.split()
    sentences = _sentences(text)
    headings = {level: [(" ".join(h.itertext())).strip() for h in root.findall(f".//h{level}")] for level in (1, 2, 3)}
    question_headings = sum(
        1 for hs in headings.values() for h in hs if h.endswith("?") or _QUESTION_RE.match(h)
    )

    images = 0
    for img in root.iter("img"):
        src = img.get("src", "")
        if src.startswith("data:"):
            continue
        if img.get("alt", "").strip():
            images += 1
            continue
        try:
            w, h = int(img.get("width", 0)), int(img.get("height", 0))
        except ValueError:
            w = h = 0
        if w >= cfg.min_content_image_px and h >= cfg.min_content_image_px:
            images += 1

    own_domain = _domain(base_url)
    internal = external = 0
    for a in root.iter("a"):
        href = a.get("href", "")
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        target = _domain(href) if href.startswith(("http://", "https://")) else ""
        if target and own_domain and target != own_domain:
            external += 1
        else:
            internal += 1

    numeric_hits = sum(1 for m in _NUMERIC_RE.finditer(text) if m.group().strip())
    return {
        "word_count": float(len(words)),
        "h1_count": float(len(headings[1])),
        "h2_count": float(len(headings[2])),
        "h3_count": float(len(headings[3])),
        "paragraph_count": float(len(root.findall(".//p"))),
        "avg_sentence_len": (len(words) / len(sentences)) if sentences else 0.0,
        "image_count": float(images),
        "list_count": float(len(root.findall(".//ul")) + len(root.findall(".//ol"))),
        "table_count": float(len(root.findall(".//table"))),
        "internal_links": float(internal),
        "external_links": float(external),
        "question_headings": float(question_headings),
        "numeric_density": (numeric_hits / len(words) * 100) if words else 0.0,
    }


def _split_sections(root) -> list[Section]:
    """Heading-bounded sections for Layer 4 chunking, read from the stripped
    DOM (boilerplate and comments are already gone)."""
    sections: list[Section] = [Section()]
    for el in root.iter():
        if isinstance(el.tag, str) and re.fullmatch(r"h[1-3]", el.tag):
            heading = " ".join(" ".join(el.itertext()).split())
            sections.append(Section(heading=heading, level=int(el.tag[1])))
        elif el.tag in ("p", "li", "blockquote"):
            text = " ".join(" ".join(el.itertext()).split())
            if len(text) >= 25 and _link_density(el) <= 0.5:
                sections[-1].text = f"{sections[-1].text} {text}".strip()
    return [s for s in sections if s.text]


def extract_document(
    html: str, cfg: ExtractCfg, url: str = "", expected_lang: str = "en"
) -> CleanDoc:
    doc = CleanDoc(url=url)
    try:
        original_root = lxml_html.fromstring(html)
    except Exception:
        doc.flags.append("unparseable")
        doc.usable = False
        return doc

    schema_types, jsonld_text = _jsonld_extract(original_root)
    doc.schema_types = schema_types

    root = lxml_html.fromstring(html)
    _drop_boilerplate(root, cfg)

    # trafilatura runs on the ALREADY-STRIPPED tree so comment sections and
    # known boilerplate can never leak through, whatever its own heuristics do
    stripped_html = lxml_html.tostring(root, encoding="unicode")
    text = trafilatura.extract(
        stripped_html, include_comments=False, include_tables=True, favor_recall=False
    ) or ""
    density_text = _density_extract(root, cfg)
    # trafilatura wins unless it came back suspiciously short vs the density view
    if len(text.split()) < max(cfg.thin_doc_words, len(density_text.split()) // 2):
        if len(density_text.split()) > len(text.split()):
            text = density_text
            doc.flags.append("density_fallback")

    # merge JSON-LD-only substance (recipe steps, FAQ answers) not already visible
    if jsonld_text:
        missing = [
            line for line in jsonld_text.split("\n")
            if len(line) > 30 and line[:60] not in text
        ]
        if missing:
            text = text + "\n" + "\n".join(missing) if text else "\n".join(missing)

    words = text.split()
    if len(words) > cfg.max_doc_words:
        text = " ".join(words[:cfg.max_doc_words])
        doc.flags.append("truncated")

    # interstitial guard: cookie/consent/newsletter walls masquerading as content
    sentences = _sentences(text.lower())
    if sentences:
        hits = sum(
            1 for s in sentences if any(p in s for p in cfg.interstitial_phrases)
        )
        if hits / len(sentences) > cfg.interstitial_max_share:
            doc.flags.append("interstitial")
            doc.usable = False

    doc.text = text
    doc.lang = (original_root.get("lang") or "")[:2].lower() or detect_language(text)
    if expected_lang and doc.lang and doc.lang != expected_lang:
        doc.flags.append("language_mismatch")
        doc.usable = False

    doc.features = _measure_features(root, text, url, cfg)
    doc.sections = _split_sections(root)

    if doc.usable and doc.word_count < cfg.thin_doc_words:
        doc.flags.append("thin")
        doc.usable = False
    return doc


# ------------------------------------------------------------ near-duplicates

def _shingles(text: str, size: int) -> set[int]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {hash(" ".join(words[i:i + size])) for i in range(max(0, len(words) - size + 1))}


def near_duplicates(docs: list[CleanDoc], cfg: ExtractCfg) -> list[tuple[int, int]]:
    """Pairs (kept_idx, dropped_idx) of syndicated copies. Corpus is <= 50 docs,
    so exact pairwise Jaccard beats MinHash approximation here."""
    sets = [_shingles(d.text, cfg.shingle_size) for d in docs]
    pairs: list[tuple[int, int]] = []
    dropped: set[int] = set()
    for i in range(len(docs)):
        if i in dropped or not sets[i]:
            continue
        for j in range(i + 1, len(docs)):
            if j in dropped or not sets[j]:
                continue
            union = len(sets[i] | sets[j])
            if union and len(sets[i] & sets[j]) / union >= cfg.duplicate_jaccard:
                pairs.append((i, j))
                dropped.add(j)
    return pairs


def drop_duplicates(docs: list[CleanDoc], cfg: ExtractCfg) -> list[CleanDoc]:
    """Mark lower-ranked syndicated copies unusable; return the same list."""
    for _, j in near_duplicates(docs, cfg):
        docs[j].flags.append("duplicate")
        docs[j].usable = False
    return docs

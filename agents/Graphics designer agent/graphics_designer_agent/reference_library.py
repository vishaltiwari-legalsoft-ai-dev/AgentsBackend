"""Brand Reference Library — ingest + retrieval rail.

This is the agent's *memory*: it turns a brand's real, human-approved creatives
(organised by creative TYPE) into a searchable, understood index that future
generations can take reference from.

Two jobs live here:

1. **Ingestion** — walk a directory laid out as ``<base>/<Brand>/<creative_type>/*``,
   *understand* each creative (real dimensions + dominant palette via Pillow,
   tags/summary from the filename and folder, optional LLM enrichment), and write
   a flat ``reference_index.json`` next to the assets.

2. **Retrieval** — given a job (brand + creative type + a free-text brief), rank
   the indexed creatives by how well they match and return the best examples,
   each with a human-readable *why*, plus a prompt-ready text block the generator
   can later be grounded on.

Deliberate design choices:
- **No GCP required.** The index is a local JSON file and understanding uses only
  Pillow + stdlib, so the whole rail runs and tests offline. (A cloud-backed store
  can mirror this later — the record shape is storage-agnostic.)
- **Creative type is first-class.** Each type carries its own format rules
  (dimensions, aspect, orientation), because a social story, a carousel and a
  brochure are fundamentally different jobs.
- **Self-contained + additive.** Nothing here mutates existing pipeline behaviour;
  it only reads images and reads/writes its own index file.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from PIL import Image

logger = logging.getLogger("graphics_designer.reference_library")

INDEX_FILENAME = "reference_index.json"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
# Document references (brochures/decks) the team has produced before. These are
# *understood* by rendering page 1 to an image (real dims + palette) and reading
# the page count — see ``understand_document``. PowerPoint decks are rendered the
# same way when LibreOffice/unoconv is available; otherwise they are indexed with
# metadata only. The whole rail stays additive and offline-first.
DOC_EXTS = {".pdf", ".pptx"}
REF_EXTS = IMAGE_EXTS | DOC_EXTS


# --------------------------------------------------------------------------- #
# Creative-type taxonomy — each type is its own job, with its own format rules
# --------------------------------------------------------------------------- #
#
# Beyond format rules, each type now carries two routing facts the Creative Agent
# depends on:
#   * ``output_format`` — what the finished asset is (image / image_set / pdf / pptx).
#   * ``routes_to``     — ``"studio"`` for standard social posts (the existing
#     4-stage editor) or ``"creative_agent"`` for brochures/decks/carousels/blogs,
#     which the dedicated Creative Agent plans and produces end-to-end.

CREATIVE_TYPES: dict[str, dict[str, Any]] = {
    "social_story": {
        "label": "Social Story",
        "aspect_ratio": "9:16",
        "target_dims": (1080, 1920),
        "orientation": "portrait",
        "multi_frame": False,
        "output_format": "image",
        "routes_to": "studio",
        "notes": (
            "Vertical full-screen story/reel. Hook in the first beat; keep the "
            "top and bottom ~14% clear of key content for platform UI chrome."
        ),
    },
    "carousel": {
        "label": "Carousel",
        "aspect_ratio": "1:1",
        "target_dims": (1080, 1080),
        "orientation": "square",
        "multi_frame": True,
        "output_format": "image_set",
        "routes_to": "creative_agent",
        "notes": (
            "Multi-frame square sequence. Slide 1 is the hook, middle slides carry "
            "the payload, the final slide is the CTA. Keep one consistent system "
            "(type, colour, layout) across every slide."
        ),
    },
    "brochure": {
        "label": "Brochure",
        "aspect_ratio": "3:4",
        "target_dims": (1240, 1754),
        "orientation": "portrait",
        "multi_frame": False,
        "output_format": "pdf",
        "routes_to": "creative_agent",
        "notes": (
            "Print-oriented, copy-dense. Multi-column layout, generous margins, "
            "typography-led rather than image-led."
        ),
    },
    "presentation": {
        "label": "Presentation",
        "aspect_ratio": "16:9",
        "target_dims": (1920, 1080),
        "orientation": "landscape",
        "multi_frame": True,
        "output_format": "pptx",
        "routes_to": "creative_agent",
        "notes": (
            "Slide deck (PPTX). Title slide, then one idea per slide with a short "
            "heading and 3–5 supporting bullets; speaker notes carry the detail. "
            "Consistent master across every slide."
        ),
    },
    "blog": {
        "label": "Blog Visuals",
        "aspect_ratio": "16:9",
        "target_dims": (1600, 900),
        "orientation": "landscape",
        "multi_frame": True,
        "output_format": "image_set",
        "routes_to": "creative_agent",
        "notes": (
            "Article visuals: one wide cover image plus in-article images that "
            "illustrate the post's sections. Cover leads with the title; in-article "
            "images are quieter and support the surrounding copy."
        ),
    },
}

DEFAULT_CREATIVE_TYPE = "social_story"


# --------------------------------------------------------------------------- #
# Reference-only style categories — indexed as on-brand precedent but NOT
# generation output types (no routing/format rules). These let folders like a
# brand's gradient swatches or newsletter graphics enrich the library as style
# memory without pretending to be a creative the agent renders end-to-end.
# --------------------------------------------------------------------------- #

REFERENCE_CATEGORIES: dict[str, dict[str, Any]] = {
    "brand_gradient": {
        "label": "Brand Gradient",
        "reference_only": True,
        "orientation": None,  # any orientation is fine for a style swatch
        "notes": "Signature brand gradient/colour-field. Use for palette + mood.",
    },
    "newsletter": {
        "label": "Newsletter Graphic",
        "reference_only": True,
        "orientation": None,
        "notes": "Email/newsletter graphics. Use for layout + typographic style.",
    },
}

# Drive (or any source) subfolder names that should resolve to a canonical type
# even though they are spelled differently. Keys are lowercased folder names.
FOLDER_ALIASES: dict[str, str] = {
    "story": "social_story",
    "stories": "social_story",
    "ls gradients": "brand_gradient",
    "gradients": "brand_gradient",
    "newsletter graphics": "newsletter",
    "newsletters": "newsletter",
    "brochure and flyer": "brochure",
    "brochures and flyers": "brochure",
    "brochures": "brochure",
    "flyers": "brochure",
    "blog covers": "blog",
    "blog": "blog",
    "blogs": "blog",
}


def creative_type_keys() -> list[str]:
    return list(CREATIVE_TYPES.keys())


def is_known_type(creative_type: str) -> bool:
    return creative_type in CREATIVE_TYPES


def is_reference_category(name: str) -> bool:
    return name in REFERENCE_CATEGORIES


def is_ingestible_type(name: str) -> bool:
    """True for any type the library will index — generation types OR
    reference-only style categories."""
    return is_known_type(name) or is_reference_category(name)


def resolve_folder_type(folder_name: str) -> Optional[str]:
    """Map a source subfolder name to a canonical ingestible type.

    Tries (1) the alias table, (2) a direct case-normalised match against
    generation types or reference categories. Returns ``None`` when the folder
    is not something we index (caller decides whether to skip + log)."""
    key = (folder_name or "").strip().lower()
    if key in FOLDER_ALIASES:
        return FOLDER_ALIASES[key]
    if is_ingestible_type(key):
        return key
    # Try a slug form so "Brand Gradient" -> "brand_gradient".
    slug = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    if is_ingestible_type(slug):
        return slug
    return None


def type_label(name: str) -> str:
    """Human label for a generation type OR a reference-only category."""
    spec = CREATIVE_TYPES.get(name) or REFERENCE_CATEGORIES.get(name) or {}
    return spec.get("label", name)


def type_spec(creative_type: str) -> dict[str, Any]:
    """The format/routing rules for a type.

    Looks in the generation taxonomy first, then the reference-only categories,
    so callers can ask about either with one function (empty dict if unknown)."""
    return CREATIVE_TYPES.get(creative_type) or REFERENCE_CATEGORIES.get(creative_type, {})


def routes_to_creative_agent(creative_type: str) -> bool:
    """True for brochures/decks/carousels/blogs — the types the spec sends to the
    dedicated Creative Agent rather than the standard 4-stage social editor."""
    return type_spec(creative_type).get("routes_to") == "creative_agent"


def output_format_for(creative_type: str) -> str:
    """``image`` | ``image_set`` | ``pdf`` | ``pptx`` (``image`` if unknown)."""
    return type_spec(creative_type).get("output_format", "image")


# --------------------------------------------------------------------------- #
# Small text helpers
# --------------------------------------------------------------------------- #

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "with", "in", "on", "at",
    "by", "our", "your", "we", "us", "is", "are", "be", "this", "that", "from",
    "png", "jpg", "jpeg", "webp", "final", "copy", "v1", "v2", "draft", "new",
}


def slugify(text: str) -> str:
    """A filesystem/id-safe lowercase slug (``Legal Soft`` -> ``legal-soft``)."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return cleaned or "untitled"


def brand_slug(brand_name: str) -> str:
    """Brand id slug with separators removed (``Legal Soft`` -> ``legalsoft``).

    Matches the templated brand ids used elsewhere (``legalsoft``,
    ``medvirtual``, ``remote_attorneys``) closely enough to cross-reference.
    """
    return re.sub(r"[^a-z0-9]+", "", (brand_name or "").lower()) or "brand"


def tokenize(text: str) -> list[str]:
    """Lowercase content words from arbitrary text (drops stopwords/short bits)."""
    words = re.split(r"[^a-z0-9]+", (text or "").lower())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS]


# --------------------------------------------------------------------------- #
# Image understanding (Pillow only — no model required)
# --------------------------------------------------------------------------- #

def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a or 1


def aspect_ratio_str(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "0:0"
    g = _gcd(width, height)
    return f"{width // g}:{height // g}"


def orientation_of(width: int, height: int) -> str:
    if height > width:
        return "portrait"
    if width > height:
        return "landscape"
    return "square"


def dominant_palette(img: Image.Image, colors: int = 5) -> list[str]:
    """Top ``colors`` dominant hex colours, most frequent first.

    Downscales then median-cut quantises so this is fast and stable regardless of
    source resolution.
    """
    rgb = img.convert("RGB")
    rgb.thumbnail((120, 120))
    quant = rgb.quantize(colors=max(2, colors * 2), method=Image.Quantize.MEDIANCUT)
    palette = quant.getpalette() or []
    counts = quant.getcolors() or []  # list of (count, palette_index)
    counts.sort(key=lambda c: c[0], reverse=True)
    hexes: list[str] = []
    for _count, idx in counts[:colors]:
        base = idx * 3
        if base + 2 < len(palette):
            r, g, b = palette[base], palette[base + 1], palette[base + 2]
            hexes.append(f"#{r:02x}{g:02x}{b:02x}")
    return hexes


def understand_image(path: Path, creative_type: str) -> dict[str, Any]:
    """Extract everything we can know about one creative from the file alone."""
    with Image.open(path) as img:
        width, height = img.size
        palette = dominant_palette(img)

    ar = aspect_ratio_str(width, height)
    orient = orientation_of(width, height)
    spec = type_spec(creative_type)
    expected_orient = spec.get("orientation")
    format_match = expected_orient is None or orient == expected_orient

    return {
        "width": width,
        "height": height,
        "aspect_ratio": ar,
        "orientation": orient,
        "expected_aspect_ratio": spec.get("aspect_ratio"),
        "format_match": format_match,
        "palette": palette,
    }


def _render_pdf_first_page(path: Path) -> tuple[Optional[Image.Image], int]:
    """Render page 1 of a PDF to a Pillow image + return the page count.

    Uses PyMuPDF (``fitz``) when available — it ships as a wheel with no system
    dependency, so this stays offline-friendly. Returns ``(None, pages)`` if
    PyMuPDF is not installed (we still know the page count via pypdf) or
    ``(None, 0)`` if neither library can read the file.
    """
    try:
        import fitz  # PyMuPDF — lazy so the package imports without it
    except Exception:  # pragma: no cover - exercised only when PyMuPDF is absent
        # Fall back to pypdf purely for the page count (no raster → no palette).
        try:
            from pypdf import PdfReader

            return None, len(PdfReader(str(path)).pages)
        except Exception:
            return None, 0
    try:
        with fitz.open(str(path)) as doc:
            pages = doc.page_count
            if pages == 0:
                return None, 0
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            return img, pages
    except Exception as exc:  # noqa: BLE001 - a bad doc shouldn't break ingest
        logger.warning("could not render PDF '%s': %s", path, exc)
        return None, 0


def understand_document(path: Path, creative_type: str) -> dict[str, Any]:
    """Understand a *document* reference (PDF/PPTX) the way we understand images.

    Renders the first page to read real dimensions + dominant palette (so a
    brochure's look can ground future generations), and records the page/slide
    count. PPTX has no portable offline renderer here, so it is indexed with
    metadata only (page count via python-pptx when present) — enough to retrieve
    it as precedent; the page render is a best-effort enhancement.
    """
    suffix = path.suffix.lower()
    spec = type_spec(creative_type)
    img: Optional[Image.Image] = None
    pages = 0

    if suffix == ".pdf":
        img, pages = _render_pdf_first_page(path)
    elif suffix == ".pptx":
        try:
            from pptx import Presentation  # lazy

            prs = Presentation(str(path))
            pages = len(prs.slides)
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not read pptx '%s': %s", path, exc)

    if img is not None:
        width, height = img.size
        palette = dominant_palette(img)
        img.close()
    else:
        # No raster available — fall back to the type's nominal dimensions so the
        # record still has sensible format metadata.
        width, height = spec.get("target_dims", (0, 0))
        palette = []

    ar = aspect_ratio_str(width, height) if width and height else (spec.get("aspect_ratio") or "0:0")
    orient = orientation_of(width, height) if width and height else (spec.get("orientation") or "portrait")
    expected_orient = spec.get("orientation")
    format_match = expected_orient is None or orient == expected_orient

    return {
        "width": width,
        "height": height,
        "aspect_ratio": ar,
        "orientation": orient,
        "expected_aspect_ratio": spec.get("aspect_ratio"),
        "format_match": format_match,
        "palette": palette,
        "pages": pages,
        "is_document": True,
    }


def understand_asset(path: Path, creative_type: str) -> dict[str, Any]:
    """Dispatch to image- or document-understanding by file extension."""
    if path.suffix.lower() in DOC_EXTS:
        return understand_document(path, creative_type)
    understood = understand_image(path, creative_type)
    understood["is_document"] = False
    return understood


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

@dataclass
class ReferenceRecord:
    id: str
    brand_id: str
    brand_name: str
    creative_type: str
    file_name: str
    relative_path: str
    abs_path: str
    width: int
    height: int
    aspect_ratio: str
    orientation: str
    format_match: bool
    palette: list[str]
    tags: list[str]
    summary: str
    source: str  # "deterministic" | "agent+llm"
    ingested_at: str
    extra: dict[str, Any] = field(default_factory=dict)
    # Durable storage URI (``gs://bucket/object``) when the asset has been
    # mirrored to Cloud Storage; empty for a purely local index.
    gs_uri: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "brand_id": self.brand_id,
            "brand_name": self.brand_name,
            "creative_type": self.creative_type,
            "file_name": self.file_name,
            "relative_path": self.relative_path,
            "abs_path": self.abs_path,
            "gs_uri": self.gs_uri,
            "width": self.width,
            "height": self.height,
            "aspect_ratio": self.aspect_ratio,
            "orientation": self.orientation,
            "format_match": self.format_match,
            "palette": self.palette,
            "tags": self.tags,
            "summary": self.summary,
            "source": self.source,
            "ingested_at": self.ingested_at,
            "extra": self.extra,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_id(brand_id: str, relative_path: str) -> str:
    return slugify(f"{brand_id}-{relative_path}")


def _deterministic_summary(brand_name: str, creative_type: str, tags: list[str]) -> str:
    label = type_label(creative_type)
    theme = ", ".join(tags[:4]) if tags else "general brand"
    return f"{brand_name} {label.lower()} — themes: {theme}."


# --------------------------------------------------------------------------- #
# Optional LLM enrichment (mirrors suggestions.py — lazy, never required)
# --------------------------------------------------------------------------- #

def _enrich_with_llm(record: ReferenceRecord) -> Optional[dict[str, Any]]:
    """Best-effort LLM pass that sharpens tags + writes a richer summary.

    Returns ``{"tags": [...], "summary": "..."}`` or ``None`` if the LLM is not
    available (no OpenRouter key / app not importable). The deterministic result
    always stands on its own; this only refines it.
    """
    try:
        from app.services.openrouter import get_llm  # lazy — package works without the app
    except Exception:
        logger.debug("OpenRouter not importable; keeping deterministic understanding")
        return None

    spec = type_spec(record.creative_type)
    prompt = (
        "You label marketing creatives for a brand reference library. "
        f"Brand: {record.brand_name}. Creative type: {spec.get('label', record.creative_type)} "
        f"({spec.get('notes', '')}). File: {record.file_name}. "
        f"Dominant colours: {', '.join(record.palette)}. "
        "Return STRICT JSON: {\"tags\": [up to 8 short lowercase theme/intent words], "
        "\"summary\": \"one sentence describing what this creative communicates\"}."
    )
    try:
        msg = get_llm(temperature=0.4, fast=True).invoke(prompt)
        content = getattr(msg, "content", "") or ""
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        tags = [str(t).lower().strip() for t in data.get("tags", []) if str(t).strip()]
        summary = str(data.get("summary", "")).strip()
        if not tags and not summary:
            return None
        return {"tags": tags or record.tags, "summary": summary or record.summary}
    except Exception as exc:  # noqa: BLE001 - enrichment must never break ingestion
        logger.debug("LLM enrichment failed (%s); keeping deterministic result", exc)
        return None


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #

def _iter_creative_files(brand_dir: Path) -> Iterable[tuple[str, Path]]:
    """Yield (creative_type, file_path) for every image under a brand folder.

    Layout: ``<brand_dir>/<creative_type>/<file>``. Folder names are resolved
    through ``resolve_folder_type`` so aliases (``Story`` -> ``social_story``) and
    reference-only categories (``LS Gradients`` -> ``brand_gradient``) are indexed.
    Folders that resolve to nothing are skipped with a log line.
    """
    for type_dir in sorted(p for p in brand_dir.iterdir() if p.is_dir()):
        creative_type = resolve_folder_type(type_dir.name)
        if creative_type is None:
            logger.warning("skipping unknown creative-type folder: %s", type_dir.name)
            continue
        for file_path in sorted(type_dir.rglob("*")):
            if file_path.is_file() and file_path.suffix.lower() in REF_EXTS:
                yield creative_type, file_path


def build_record(
    brand_dir: Path,
    brand_name: str,
    creative_type: str,
    file_path: Path,
    *,
    use_llm: bool = False,
) -> ReferenceRecord:
    bslug = brand_slug(brand_name)
    relative = file_path.relative_to(brand_dir).as_posix()
    understood = understand_asset(file_path, creative_type)
    tags = tokenize(file_path.stem) + [creative_type]
    if understood.get("is_document"):
        tags.append("document")
    # de-dup, keep order
    seen: set[str] = set()
    tags = [t for t in tags if not (t in seen or seen.add(t))]

    extra: dict[str, Any] = {"expected_aspect_ratio": understood["expected_aspect_ratio"]}
    if understood.get("is_document"):
        extra["is_document"] = True
        extra["pages"] = understood.get("pages", 0)
        extra["kind"] = file_path.suffix.lower().lstrip(".")

    record = ReferenceRecord(
        id=_record_id(bslug, relative),
        brand_id=bslug,
        brand_name=brand_name,
        creative_type=creative_type,
        file_name=file_path.name,
        relative_path=relative,
        abs_path=str(file_path.resolve()),
        width=understood["width"],
        height=understood["height"],
        aspect_ratio=understood["aspect_ratio"],
        orientation=understood["orientation"],
        format_match=understood["format_match"],
        palette=understood["palette"],
        tags=tags,
        summary=_deterministic_summary(brand_name, creative_type, tags),
        source="deterministic",
        ingested_at=_now_iso(),
        extra=extra,
    )

    if use_llm:
        enriched = _enrich_with_llm(record)
        if enriched:
            record.tags = enriched["tags"]
            record.summary = enriched["summary"]
            record.source = "agent+llm"
    return record


def ingest_brand(
    base_dir: Path,
    brand_name: str,
    *,
    use_llm: bool = False,
) -> list[ReferenceRecord]:
    """Understand every creative for one brand under ``base_dir/<brand_name>``."""
    brand_dir = base_dir / brand_name
    if not brand_dir.is_dir():
        raise FileNotFoundError(f"Brand folder not found: {brand_dir}")
    records: list[ReferenceRecord] = []
    for creative_type, file_path in _iter_creative_files(brand_dir):
        try:
            records.append(
                build_record(brand_dir, brand_name, creative_type, file_path, use_llm=use_llm)
            )
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't stop ingest
            logger.warning("skipped '%s': %s", file_path, exc)
    return records


def ingest_all(base_dir: Path, *, use_llm: bool = False) -> list[ReferenceRecord]:
    """Ingest every brand folder under ``base_dir`` into one flat record list."""
    if not base_dir.is_dir():
        raise FileNotFoundError(f"Reference base directory not found: {base_dir}")
    records: list[ReferenceRecord] = []
    for brand_dir in sorted(p for p in base_dir.iterdir() if p.is_dir()):
        records.extend(ingest_brand(base_dir, brand_dir.name, use_llm=use_llm))
    return records


# --------------------------------------------------------------------------- #
# Index persistence (local JSON — storage-agnostic record shape)
# --------------------------------------------------------------------------- #

def default_base_dir() -> Path:
    """Where reference assets + the index live by default.

    ``GD_REFERENCE_DIR`` overrides; otherwise the repo's ``Data/_reference_mock``.
    Shared by the API router and the generation pipeline so both read/write the
    same index."""
    import os

    override = os.environ.get("GD_REFERENCE_DIR")
    if override:
        return Path(override)
    # .../backend/agents/Graphics designer agent/graphics_designer_agent/reference_library.py
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "Data" / "_reference_mock"


def index_path(base_dir: Path) -> Path:
    return base_dir / INDEX_FILENAME


def _gcs_storage():
    """Return the app's GCS storage module when it is importable AND configured,
    else ``None`` — so the rail stays fully offline by default and only reaches
    for Cloud Storage when the backend env actually has a bucket set."""
    try:
        from app.services import storage  # lazy — package works without the app
    except Exception:
        return None
    try:
        return storage if storage.is_configured() else None
    except Exception:  # noqa: BLE001 - never let storage probing break the rail
        return None


def _index_payload(records: list[ReferenceRecord]) -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": _now_iso(),
        "count": len(records),
        "creative_types": creative_type_keys(),
        "reference_categories": list(REFERENCE_CATEGORIES.keys()),
        "records": [r.to_dict() for r in records],
    }


def write_index(base_dir: Path, records: list[ReferenceRecord]) -> Path:
    """Write the index to local disk, and ALSO to GCS when configured.

    The local file is always written (keeps dev + tests offline). When the
    backend has a bucket, the same payload is mirrored to GCS so retrieval works
    on Cloud Run after the ephemeral disk is gone."""
    path = index_path(base_dir)
    payload = _index_payload(records)
    raw = json.dumps(payload, indent=2)
    path.write_text(raw, encoding="utf-8")

    storage = _gcs_storage()
    if storage is not None:
        try:
            storage.write_reference_index(raw.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - local write already succeeded
            logger.warning("could not mirror reference index to GCS: %s", exc)
    return path


def load_index(base_dir: Path) -> list[dict[str, Any]]:
    """Load the records list (empty list if not built yet).

    Prefers the durable GCS copy when configured (so a restarted Cloud Run
    instance still sees the index), falling back to the local file."""
    storage = _gcs_storage()
    if storage is not None:
        try:
            raw = storage.read_reference_index()
            if raw:
                data = json.loads(raw.decode("utf-8"))
                return list(data.get("records", []))
        except Exception as exc:  # noqa: BLE001 - fall back to local on any error
            logger.warning("could not read reference index from GCS: %s", exc)

    path = index_path(base_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("records", []))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read reference index %s: %s", path, exc)
        return []


def mirror_to_gcs(records: list[ReferenceRecord]) -> int:
    """Upload each record's local bytes to Cloud Storage and stamp ``gs_uri``.

    No-op (returns 0) when GCS is not configured. Best-effort per file: a single
    failed upload is logged and skipped so one bad asset can't abort the sync."""
    storage = _gcs_storage()
    if storage is None:
        return 0
    mirrored = 0
    for r in records:
        try:
            data = Path(r.abs_path).read_bytes()
            mime = _mime_for_suffix(Path(r.abs_path).suffix)
            gs_uri, _ = storage.upload_reference_library_asset(
                r.brand_id, r.creative_type, r.file_name, data, mime
            )
            r.gs_uri = gs_uri
            mirrored += 1
        except Exception as exc:  # noqa: BLE001 - skip one bad file, keep going
            logger.warning("could not mirror '%s' to GCS: %s", r.abs_path, exc)
    return mirrored


_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _mime_for_suffix(suffix: str) -> str:
    return _MIME_BY_SUFFIX.get(suffix.lower(), "application/octet-stream")


# --------------------------------------------------------------------------- #
# Retrieval — rank indexed creatives against a job (brand + type + brief)
# --------------------------------------------------------------------------- #

def score_record(
    record: dict[str, Any],
    *,
    brief_tokens: list[str],
    want_format_match: bool = True,
) -> tuple[float, list[str]]:
    """Score one record for a brief; returns (score, human-readable reasons)."""
    reasons: list[str] = []
    score = 0.0

    record_tags = set(record.get("tags", []))
    summary_tokens = set(tokenize(record.get("summary", "")))
    haystack = record_tags | summary_tokens

    overlap = [t for t in brief_tokens if t in haystack]
    if overlap:
        score += 3.0 * len(overlap)
        reasons.append(f"matches brief: {', '.join(sorted(set(overlap)))}")

    if want_format_match and record.get("format_match"):
        score += 1.0
        reasons.append("correct format for this creative type")

    # A small constant so well-formed references still surface for an empty/novel
    # brief (precedent beats nothing), without overpowering real matches.
    score += 0.25
    if not reasons:
        reasons.append("on-brand precedent for this type")
    return score, reasons


def retrieve(
    records: list[dict[str, Any]],
    *,
    creative_type: Optional[str] = None,
    brief: str = "",
    brand_id: Optional[str] = None,
    k: int = 3,
) -> list[dict[str, Any]]:
    """Return the top-``k`` reference creatives for a job, each with score + why.

    Filters by brand and creative type when given, then ranks the rest by brief
    relevance and format fitness.
    """
    brief_tokens = tokenize(brief)
    pool = records
    if brand_id:
        bslug = brand_slug(brand_id) if " " in brand_id else brand_id
        pool = [r for r in pool if r.get("brand_id") == bslug or r.get("brand_id") == brand_slug(brand_id)]
    if creative_type:
        pool = [r for r in pool if r.get("creative_type") == creative_type]

    scored: list[dict[str, Any]] = []
    for record in pool:
        score, reasons = score_record(record, brief_tokens=brief_tokens)
        scored.append({**record, "_score": round(score, 3), "_why": reasons})

    scored.sort(key=lambda r: (r["_score"], r.get("ingested_at", "")), reverse=True)
    return scored[: max(0, k)]


def retrieve_for_generation(
    records: list[dict[str, Any]],
    *,
    brand_id: Optional[str] = None,
    creative_type: Optional[str] = None,
    brief: str = "",
    k: int = 3,
    style_k: int = 2,
) -> list[dict[str, Any]]:
    """Retrieval tuned for grounding a generation.

    Returns the top-``k`` references of the requested creative type, then appends
    up to ``style_k`` brand *style* references (gradients/newsletter precedent) so
    the generator is grounded on both the right format AND the brand's signature
    look. Style refs are de-duplicated against the primary hits."""
    primary = retrieve(records, creative_type=creative_type, brief=brief, brand_id=brand_id, k=k)
    seen = {r.get("id") for r in primary}

    style_pool = [
        r for r in records
        if r.get("creative_type") in REFERENCE_CATEGORIES and r.get("id") not in seen
    ]
    if brand_id:
        bslug = brand_slug(brand_id)
        style_pool = [r for r in style_pool if r.get("brand_id") == bslug]
    styles = retrieve(style_pool, brief=brief, k=style_k) if style_pool else []
    return primary + styles


def load_reference_bytes(record: dict[str, Any]) -> Optional[tuple[bytes, str]]:
    """Return ``(bytes, mime)`` for a reference record, or ``None``.

    Prefers the local file (``abs_path``); falls back to the GCS object
    (``gs_uri``) when the local copy is gone (e.g. on Cloud Run). Only raster
    images are returned — documents (PDF/PPTX) are skipped as visual references.
    """
    suffix = Path(record.get("file_name", "")).suffix.lower()
    if suffix not in IMAGE_EXTS:
        return None
    mime = _mime_for_suffix(suffix)
    abs_path = record.get("abs_path")
    if abs_path and Path(abs_path).is_file():
        try:
            return Path(abs_path).read_bytes(), mime
        except Exception:  # noqa: BLE001
            pass
    gs_uri = record.get("gs_uri")
    if gs_uri:
        try:
            from app.services import storage

            return storage.download_bytes(gs_uri), mime
        except Exception:  # noqa: BLE001
            return None
    return None


def reference_images_for(
    brand_id: Optional[str],
    creative_type: Optional[str],
    *,
    brief: str = "",
    base_dir: Optional[Path] = None,
    k: int = 2,
) -> list[tuple[bytes, str]]:
    """Top reference creatives for a job, loaded as ``(bytes, mime)`` image inputs.

    This is what the generation backbone shows the image model so output looks
    like real on-brand precedent. Best-effort: missing index / unreadable files
    just yield fewer (or zero) references, never an error.
    """
    records = load_index(base_dir if base_dir is not None else default_base_dir())
    if not records:
        return []
    hits = retrieve_for_generation(
        records, brand_id=brand_id, creative_type=creative_type, brief=brief, k=k, style_k=1
    )
    out: list[tuple[bytes, str]] = []
    for r in hits:
        loaded = load_reference_bytes(r)
        if loaded:
            out.append(loaded)
    return out


def summarize_for_prompt(retrieved: list[dict[str, Any]]) -> str:
    """Turn retrieved references into a compact, prompt-ready grounding block.

    This is the bridge to the (future) generation rail: drop this text into the
    creative brief so the model is grounded in real, on-brand precedent. It is a
    pure string builder and has no effect on generation by itself.
    """
    if not retrieved:
        return "No on-brand reference creatives were found for this job."
    lines = ["Reference creatives to take inspiration from (real, on-brand precedent):"]
    for i, r in enumerate(retrieved, 1):
        palette = ", ".join(r.get("palette", [])[:4])
        tags = ", ".join(r.get("tags", [])[:6])
        lines.append(
            f"{i}. [{r.get('creative_type')}] {r.get('file_name')} "
            f"({r.get('aspect_ratio')}) — {r.get('summary')} "
            f"Palette: {palette}. Themes: {tags}."
        )
    return "\n".join(lines)

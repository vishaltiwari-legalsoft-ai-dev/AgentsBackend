"""Attachment text extraction for the creative agent.

Reads uploaded PDFs, DOCX, and images (OCR via vision model) so their content
can be merged into the user's brief. Uploaded files are processed IN MEMORY and
are NEVER persisted to Firestore or Cloud Storage.
"""

from __future__ import annotations

import io
import logging

from app.services.openrouter import vision_extract_text

logger = logging.getLogger("agentos.extract")

# Per-attachment cap on extracted characters so a huge document can't blow the
# LLM context window.
MAX_EXTRACTED_CHARS = 8000

PDF_TYPES = {"application/pdf"}
DOCX_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

SUPPORTED_TYPES = PDF_TYPES | DOCX_TYPES | IMAGE_TYPES


def _by_extension(file_name: str) -> str:
    name = file_name.lower()
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith(".docx"):
        return "docx"
    if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "image"
    return "unknown"


def _kind(file_name: str, content_type: str) -> str:
    if content_type in PDF_TYPES:
        return "pdf"
    if content_type in DOCX_TYPES:
        return "docx"
    if content_type in IMAGE_TYPES:
        return "image"
    return _by_extension(file_name)


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def _extract_docx(data: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs if p.text).strip()


def extract_text(file_name: str, content_type: str, data: bytes) -> str:
    """Extract readable text from a single attachment.

    Dispatches by type: PDF/DOCX are parsed locally; images go through the
    vision OCR model. Returns trimmed text (possibly empty). Raises ValueError
    for unsupported types so the caller can return a clean 4xx.
    """
    kind = _kind(file_name, content_type)
    if kind == "pdf":
        text = _extract_pdf(data)
    elif kind == "docx":
        text = _extract_docx(data)
    elif kind == "image":
        text = vision_extract_text(data, content_type or "image/png")
    else:
        raise ValueError(f"Unsupported attachment type: {content_type or file_name}")

    return text[:MAX_EXTRACTED_CHARS].strip()

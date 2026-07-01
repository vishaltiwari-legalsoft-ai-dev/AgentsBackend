"""Fetch + normalize web pages for competitor monitoring.

The fetcher is injectable so competitor-intel logic can be unit-tested without
network access. The default fetcher uses httpx (already a backend dependency).
"""

from __future__ import annotations

import hashlib
import re
from typing import Callable


def _default_fetcher(url: str) -> str:
    import httpx

    resp = httpx.get(url, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _strip_html(html: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch(url: str, fetcher: Callable[[str], str] | None = None) -> tuple[str, str]:
    """Return ``(content_hash, normalized_text)`` for a page."""
    raw = (fetcher or _default_fetcher)(url)
    text = _strip_html(raw)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest, text

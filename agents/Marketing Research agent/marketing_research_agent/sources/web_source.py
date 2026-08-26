"""Fetch + normalize web pages for competitor monitoring.

The fetcher is injectable so competitor-intel logic can be unit-tested without
network access. The default fetcher uses httpx (already a backend dependency).
"""

from __future__ import annotations

import hashlib
import re
from typing import Callable


def _default_fetcher(url: str) -> str:
    """Fetch *url* with the shared SSRF guard applied to every redirect hop.

    The URL reaching here is competitor-monitoring configuration, not a
    constant, so it is exactly the shape of input that must never be allowed to
    address our own infrastructure. ``follow_redirects=False`` is deliberate and
    load-bearing: ``safe_fetch.safe_get`` follows them itself so each hop is
    re-checked, which a single pre-request check cannot do.
    """
    import httpx

    from app.services import safe_fetch

    with httpx.Client(timeout=20, follow_redirects=False) as client:
        resp = safe_fetch.safe_get(client, url)
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

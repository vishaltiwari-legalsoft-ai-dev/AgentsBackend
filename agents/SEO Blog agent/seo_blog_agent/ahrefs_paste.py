"""Tolerant parsers for Ahrefs exports the writer pastes in.

Never raise on messy input: unparseable lines are skipped. Empty results mean
"nothing pasted" and the pipeline degrades to serp_estimated (spec §6).
"""
from __future__ import annotations

import csv
import io
import re


def _num(s: str) -> int | None:
    s = s.strip().replace(",", "").lower()
    m = re.match(r"^([\d.]+)\s*([km]?)$", s)
    if not m:
        return None
    return int(float(m.group(1)) * {"k": 1_000, "m": 1_000_000}.get(m.group(2), 1))


def parse_metrics(text: str) -> dict:
    """Keyword-overview paste → {volume, kd, traffic_potential} (ints or None)."""
    out: dict = {"volume": None, "kd": None, "traffic_potential": None}
    for label, key in (("traffic potential", "traffic_potential"), ("keyword difficulty", "kd"),
                       ("volume", "volume"), ("kd", "kd")):
        m = re.search(rf"{label}\D{{0,12}}([\d.,]+\s*[km]?)", text, re.I)
        if m and out[key] is None:
            out[key] = _num(m.group(1))
    return out


def parse_competitor_csv(text: str) -> list[dict]:
    """Ahrefs organic-keywords export → [{keyword, volume, position, url}].
    Header row is located by the word "keyword"; columns matched by name-contains,
    so reordered or extra columns don't break it."""
    rows: list[dict] = []
    header: list[str] | None = None
    for raw in csv.reader(io.StringIO(text.strip())):
        if not raw:
            continue
        low = [c.strip().lower() for c in raw]
        if header is None:
            if "keyword" in low:
                header = low
            continue
        cells = {h: v for h, v in zip(header, raw)}
        kw = (cells.get("keyword") or "").strip()
        if not kw:
            continue

        def icol(name: str) -> int | None:
            for h, v in cells.items():
                if name in h:
                    return _num(v)
            return None

        rows.append({"keyword": kw, "volume": icol("volume"), "position": icol("position"),
                     "url": next((cells[h].strip() for h in cells if "url" in h), "")})
    return rows


def parse_dr(text: str) -> dict[str, int]:
    """DR paste — accepts "domain 91", "domain,91", "https://domain/path: 91" lines."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = re.search(r"([a-z0-9-]+(?:\.[a-z0-9-]+)+)\S*[\s,;:]+(\d{1,3})\b", line.strip().lower())
        if not m:
            continue
        dom, dr = m.group(1), int(m.group(2))
        dom = dom[4:] if dom.startswith("www.") else dom
        if 0 <= dr <= 100:
            out[dom] = dr
    return out

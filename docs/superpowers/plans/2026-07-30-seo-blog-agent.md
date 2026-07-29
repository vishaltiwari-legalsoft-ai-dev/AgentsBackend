# SEO Blog Writer Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new hub agent that runs the content team's 12-step SEO blog process as a 3-gate pipeline (Research → Outline → Draft) with Ahrefs paste-in data, verified citations, and a compliance-checked draft.

**Architecture:** New engine package `backend/agents/SEO Blog agent/seo_blog_agent/` that imports only `seo_geo_agent.sources` (Serper, page fetcher, LLM adapters) from the existing SEO agent. Thin FastAPI router `app/routers/seo_blog.py` orchestrates run lifecycle over per-run state docs (Firestore in cloud / local JSON offline). One new frontend tile + one view component.

**Tech Stack:** Python 3.11 / FastAPI / httpx / python-docx (already in requirements.txt) / pytest. Frontend: Next.js + TypeScript, no new deps.

**Spec:** `backend/docs/superpowers/specs/2026-07-30-seo-blog-agent-design.md` — read it first.

## Global Constraints

- Tests are offline-only: every test file/conftest sets `SEO_OFFLINE=1` and `SEO_BLOG_LOCAL_DIR` to a tmp dir. No live Serper/LLM/Firestore calls in tests, ever.
- Run backend tests from `c:\Users\ACER\Desktop\ghi\backend` with the repo venv if present (`.venv\Scripts\python.exe -m pytest`, else `python -m pytest`).
- Frontend bar is `npx tsc --noEmit` clean (run from `c:\Users\ACER\Desktop\ghi\newfrontend`). Lint is broken by design in this repo — do not run it.
- Data provenance is law (spec §6): every artifact carries `data_source: "ahrefs_pasted" | "serp_estimated"`; degraded operations append a plain-language note to a `degraded` list. Never present an estimate as Ahrefs data.
- An unverified citation never enters the outline (spec §3, §8).
- All tunables live in `seo_blog_agent/rules.py`: DR_THRESHOLD=70, KEYWORD_COUNT_BONUS=2, TARGET_UPLIFT=0.15, LSI_COUNT=10, EVALUATOR_MAX_ROUNDS=3, CITATION_MAX_ROUNDS=3, MIN_LINKS=3, TOP_N=3, FREQUENT_TERMS=15.
- Only these existing files may be modified: `app/__init__.py`, `app/main.py` (backend); `lib/console-data.ts`, `lib/api.ts`, `components/console/ConsoleApp.tsx`, `app/globals.css` (frontend). Do NOT touch any `seo_geo_agent` file.
- Commit prefix: `feat(blog): ...` / `test(blog): ...`. Commit locally only — do NOT push (unpushed GD work sits on both mains).
- Reused imports from a2 (exact signatures, do not redefine):
  - `seo_geo_agent.sources.serper_search(query: str) -> dict` — keys: `organic` (list of `{link, title, position}`), `related` (list[str]), `paa` (list[str]), `aio_present` (bool). Raises `CredentialMissing`.
  - `seo_geo_agent.sources.fetch_page(url: str) -> PageFacts` — fields: `url, status, title, meta_description, h1, h2, h3, schema_types, word_count, text`; property `questions`.
  - `seo_geo_agent.sources.fetch_text(url: str) -> dict` — `{status: int, text: str (first 20k chars of raw HTML), final_url: str}`. Raises `CredentialMissing` offline.
  - `seo_geo_agent.sources.llm_json(system: str, prompt: str) -> dict|list`, `llm_text(system: str, prompt: str) -> str` — both raise `CredentialMissing` on any failure.
  - `seo_geo_agent.sources.domain_of(url: str) -> str` (strips protocol + `www.`).
  - `seo_geo_agent.sources.CredentialMissing(Exception)`.

## Run document shape (the contract all tasks share)

Stored via `seo_blog_agent.state` as doc id `run-{id}`; index doc `runs-index` = `{"runs": [{"id","keyword","created","stage"}]}`.

```json
{
  "id": "ab12cd34ef",
  "keyword": "legal virtual assistant",
  "created": "2026-07-30",
  "stage": "research",
  "gates": {"keywords": false, "outline": false},
  "pasted": {"metrics": {"volume": null, "kd": null, "traffic_potential": null},
             "competitor_keywords": {"https://competitor.com/post": "<raw csv text>"},
             "dr": {}},
  "sheet": null,
  "outline_doc": null,
  "citations": null,
  "draft": null
}
```

- `sheet` (Gate 1 artifact, built by `research.build_research`): `{keyword, metrics, serp: {top3: [{url,title,position}], paa, related, aio_present}, competitors: [{url,intent,page_type,audience}], mixed_intent: bool, gap: [{keyword,tag,volume,overlap,source}], usage: {main_count_top1,target_min,target_max,frequent_terms:[{term,count}]}, lsi: [{term,fit_note}], data_source, degraded}`
- `outline_doc` (built by `outline.build_outline`): `{competitor_outlines: [profile], meta: {title,description,slug}, targets: {word_count,links}, outline: [{heading,level,note,keywords}], evaluator: {rounds,beats_all,scores,note}, degraded}`
- `citations` (built by `citations.source_citations`): `{items: [{id,claim,source_name,url,domain,dr,dr_status,section,verified}], short_by, rounds, degraded}`
- `draft` (built by `drafting.build_draft`): `{markdown, meta, compliance: {checks:[{id,label,pass,detail}], all_pass}, edited}`

---

### Task 1: Engine scaffold + state persistence

**Files:**
- Create: `agents/SEO Blog agent/seo_blog_agent/__init__.py`
- Create: `agents/SEO Blog agent/seo_blog_agent/state.py`
- Create: `agents/SEO Blog agent/seo_blog_agent/tests/__init__.py`
- Create: `agents/SEO Blog agent/seo_blog_agent/tests/conftest.py`
- Create: `agents/SEO Blog agent/seo_blog_agent/tests/test_state.py`
- Modify: `app/__init__.py` (add the agent root to `_AGENT_ROOTS`, currently lists the 3 existing agents around lines 13-15)

**Interfaces:**
- Produces: `seo_blog_agent.state.save(doc_id: str, data: dict) -> None`, `load(doc_id: str) -> dict | None`, `delete(doc_id: str) -> None`, `use_cloud() -> bool`. Local mode when `SEO_OFFLINE=1`; local dir from `SEO_BLOG_LOCAL_DIR`.

- [ ] **Step 1: Write the failing test**

`agents/SEO Blog agent/seo_blog_agent/tests/conftest.py`:

```python
"""Hard offline guard: these tests must never touch prod Firestore or paid APIs."""
import os
import pathlib
import sys

AGENT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))
# seo_blog_agent imports seo_geo_agent.sources — its root must be importable too.
GEO_ROOT = AGENT_ROOT.parent / "SEO GEO agent"
if str(GEO_ROOT) not in sys.path:
    sys.path.insert(0, str(GEO_ROOT))
BACKEND_ROOT = AGENT_ROOT.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ["SEO_OFFLINE"] = "1"

import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_BLOG_LOCAL_DIR", str(tmp_path))
    monkeypatch.delenv("SEO_SERPER_API_KEY", raising=False)
```

`agents/SEO Blog agent/seo_blog_agent/tests/test_state.py`:

```python
from seo_blog_agent import state


def test_save_load_roundtrip():
    state.save("run-abc", {"id": "abc", "keyword": "legal virtual assistant"})
    assert state.load("run-abc")["keyword"] == "legal virtual assistant"


def test_load_missing_returns_none():
    assert state.load("run-nope") is None


def test_delete_is_idempotent():
    state.save("run-x", {"id": "x"})
    state.delete("run-x")
    state.delete("run-x")
    assert state.load("run-x") is None


def test_offline_mode_active():
    assert state.use_cloud() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_state.py" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'seo_blog_agent'`

- [ ] **Step 3: Write minimal implementation**

`agents/SEO Blog agent/seo_blog_agent/__init__.py`:

```python
"""SEO Blog Writer agent — the content team's 12-step process as a 3-gate pipeline."""
```

`agents/SEO Blog agent/seo_blog_agent/state.py` (a2's proven pattern, own collection + own local-dir env var):

```python
"""Persistence gate: Firestore in cloud mode, local JSON when SEO_OFFLINE=1.

Doc ids use ``-`` separators only (``run-{id}``, ``runs-index``) so the local
fallback maps 1:1 to Windows-safe filenames.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_COLLECTION = "seo_blog"


def use_cloud() -> bool:
    return os.environ.get("SEO_OFFLINE", "0") != "1"


def _local_dir() -> Path:
    raw = os.environ.get("SEO_BLOG_LOCAL_DIR", "")
    base = Path(raw) if raw else Path(__file__).resolve().parent / "local_state"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _firestore_doc(doc_id: str):
    from app.services import firestore_repo

    return firestore_repo._db().collection(_COLLECTION).document(doc_id)


def save(doc_id: str, data: dict) -> None:
    # JSON round-trip keeps payloads Firestore-safe (no dataclasses, dates, sets).
    payload = json.loads(json.dumps(data, default=str))
    if use_cloud():
        _firestore_doc(doc_id).set(payload)
    else:
        (_local_dir() / f"{doc_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def load(doc_id: str) -> dict | None:
    if use_cloud():
        snap = _firestore_doc(doc_id).get()
        return snap.to_dict() if snap.exists else None
    path = _local_dir() / f"{doc_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def delete(doc_id: str) -> None:
    if use_cloud():
        _firestore_doc(doc_id).delete()
    else:
        path = _local_dir() / f"{doc_id}.json"
        if path.is_file():
            path.unlink()
```

In `app/__init__.py`, add to the `_AGENT_ROOTS` list (keep existing entries untouched):

```python
    _BACKEND_ROOT / "agents" / "SEO Blog agent",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_state.py" -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add "agents/SEO Blog agent" app/__init__.py
git commit -m "feat(blog): engine scaffold + offline-safe run persistence"
```

---

### Task 2: rules.py + Ahrefs paste parsers

**Files:**
- Create: `agents/SEO Blog agent/seo_blog_agent/rules.py`
- Create: `agents/SEO Blog agent/seo_blog_agent/ahrefs_paste.py`
- Test: `agents/SEO Blog agent/seo_blog_agent/tests/test_ahrefs_paste.py`

**Interfaces:**
- Produces: `rules.DR_THRESHOLD` etc. (values in Global Constraints); `ahrefs_paste.parse_metrics(text: str) -> dict` (`{volume, kd, traffic_potential}`, ints or None); `ahrefs_paste.parse_competitor_csv(text: str) -> list[dict]` (`[{keyword, volume, position, url}]`); `ahrefs_paste.parse_dr(text: str) -> dict[str, int]` (domain → DR). Parsers never raise on messy input — bad lines are skipped, empty text → empty result.

- [ ] **Step 1: Write the failing test**

`tests/test_ahrefs_paste.py`:

```python
from seo_blog_agent import ahrefs_paste


def test_metrics_from_overview_paste():
    text = "Keyword difficulty: 42\nVolume: 5.4K\nTraffic potential: 12,300"
    m = ahrefs_paste.parse_metrics(text)
    assert m == {"volume": 5400, "kd": 42, "traffic_potential": 12300}


def test_metrics_empty_and_garbage():
    assert ahrefs_paste.parse_metrics("") == {"volume": None, "kd": None, "traffic_potential": None}
    assert ahrefs_paste.parse_metrics("lorem ipsum")["volume"] is None


def test_competitor_csv_standard_export():
    text = (
        "Keyword,Current position,Volume,KD,Current URL\n"
        "legal virtual assistant,3,5400,42,https://x.com/post\n"
        "virtual paralegal services,7,880,21,https://x.com/post\n"
    )
    rows = ahrefs_paste.parse_competitor_csv(text)
    assert rows[0] == {"keyword": "legal virtual assistant", "volume": 5400,
                       "position": 3, "url": "https://x.com/post"}
    assert len(rows) == 2


def test_competitor_csv_skips_junk_and_reordered_headers():
    text = "junk line without commas\nVolume,Keyword\n1200,intake specialist\nnot,a,row,,\n"
    rows = ahrefs_paste.parse_competitor_csv(text)
    assert rows[0]["keyword"] == "intake specialist"
    assert rows[0]["volume"] == 1200


def test_dr_paste_variants():
    text = "clio.com 91\nwww.abajournal.com,88\nhttps://smokeball.com/blog: 74\nnot a line\nbaddr.com 999"
    dr = ahrefs_paste.parse_dr(text)
    assert dr == {"clio.com": 91, "abajournal.com": 88, "smokeball.com": 74}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_ahrefs_paste.py" -v`
Expected: FAIL — `ModuleNotFoundError` / `AttributeError`

- [ ] **Step 3: Write minimal implementation**

`rules.py`:

```python
"""Every tunable the pipeline enforces, in one place (spec §3: hardcode containment)."""
DR_THRESHOLD = 70          # citation domains below this are rejected (team step 9)
KEYWORD_COUNT_BONUS = 2    # main-keyword target = top-1 page's count .. count + 2 (team step 4)
TARGET_UPLIFT = 0.15       # word/link targets = competitor average * (1 + uplift) (team step 7)
LSI_COUNT = 10             # LSI/semantic keywords to select (team step 4)
EVALUATOR_MAX_ROUNDS = 3   # outline-vs-competitors revision loop cap (team step 8)
CITATION_MAX_ROUNDS = 3    # citation re-request rounds before honest "short by N" (team step 9)
MIN_LINKS = 3
TOP_N = 3                  # competitor pages analyzed
FREQUENT_TERMS = 15        # density benchmark terms from the #1 page
```

`ahrefs_paste.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_ahrefs_paste.py" -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add "agents/SEO Blog agent"
git commit -m "feat(blog): rules constants + tolerant Ahrefs paste parsers"
```

---

### Task 3: research.py — Stage 1 engine (team steps 1-4)

**Files:**
- Create: `agents/SEO Blog agent/seo_blog_agent/research.py`
- Test: `agents/SEO Blog agent/seo_blog_agent/tests/test_research.py`

**Interfaces:**
- Consumes: `rules`, `ahrefs_paste` output shapes, a2 sources (injectable).
- Produces: `research.build_research(keyword: str, pasted: dict, search=None, fetch=None, llm=None) -> dict` returning the **sheet** (shape in the header). `pasted` = `{"metrics": dict, "competitor_keywords": {url: [parsed rows]}, "dr": {}}` (competitor_keywords already parsed). Also `research.tokens(text: str) -> set[str]` (public — citations.py reuses it). `CredentialMissing` from `search` propagates (router → 503); LLM failures degrade with notes.

- [ ] **Step 1: Write the failing test**

`tests/test_research.py`:

```python
import pytest

from seo_geo_agent.sources import CredentialMissing, PageFacts
from seo_blog_agent import research

SERP = {
    "organic": [
        {"link": "https://a.com/post", "title": "Best Legal VAs", "position": 1},
        {"link": "https://b.com/post", "title": "Legal VA Guide", "position": 2},
        {"link": "https://c.com/post", "title": "What is a Legal VA", "position": 3},
    ],
    "related": ["legal virtual assistant cost", "what does a legal virtual assistant do"],
    "paa": ["What does a legal virtual assistant do?"],
    "aio_present": True,
}


def fake_search(query):
    return dict(SERP)


def fake_fetch(url):
    text = "legal virtual assistant " * 4 + "paralegal intake staffing law firm " * 30
    return PageFacts(url=url, status=200, title="T", h1=["H1"], h2=["Costs", "Hiring"],
                     word_count=1400, text=text)


def fake_llm(system, prompt):
    if '"pages"' in system:
        return {"pages": [{"url": "https://a.com/post", "intent": "commercial",
                           "page_type": "Best [Category]", "audience": "law firm owners"}]}
    return {"lsi": [{"term": "virtual paralegal", "fit_note": "hiring section"}]}


def no_llm(system, prompt):
    raise CredentialMissing("no key")


def test_sheet_core_fields():
    sheet = research.build_research("legal virtual assistant", {"metrics": {}, "competitor_keywords": {}},
                                    search=fake_search, fetch=fake_fetch, llm=fake_llm)
    assert [t["url"] for t in sheet["serp"]["top3"]] == ["https://a.com/post", "https://b.com/post", "https://c.com/post"]
    assert sheet["serp"]["aio_present"] is True
    assert sheet["competitors"][0]["intent"] == "commercial"
    assert sheet["usage"]["main_count_top1"] == 4
    assert sheet["usage"]["target_min"] == 4 and sheet["usage"]["target_max"] == 6
    assert sheet["lsi"][0]["term"] == "virtual paralegal"
    assert sheet["data_source"] == "serp_estimated"


def test_gap_from_pasted_ahrefs_rows():
    ck = {"https://a.com/post": [
        {"keyword": "legal virtual assistant", "volume": 5400, "position": 3, "url": ""},
        {"keyword": "virtual paralegal services pricing guide", "volume": 880, "position": 7, "url": ""},
        {"keyword": "what does a legal va do", "volume": 300, "position": 5, "url": ""},
    ]}
    sheet = research.build_research("legal virtual assistant", {"metrics": {"volume": 5400}, "competitor_keywords": ck},
                                    search=fake_search, fetch=fake_fetch, llm=fake_llm)
    tags = {g["keyword"]: g["tag"] for g in sheet["gap"]}
    assert tags["legal virtual assistant"] == "main"
    assert tags["virtual paralegal services pricing guide"] == "long_tail"
    assert tags["what does a legal va do"] == "aio"
    assert all(g["source"] == "ahrefs_pasted" for g in sheet["gap"])
    assert sheet["data_source"] == "ahrefs_pasted"


def test_gap_fallback_is_honestly_labeled():
    sheet = research.build_research("legal virtual assistant", {"metrics": {}, "competitor_keywords": {}},
                                    search=fake_search, fetch=fake_fetch, llm=fake_llm)
    assert sheet["gap"] and all(g["source"] == "serp_estimated" for g in sheet["gap"])
    assert any("SERP-estimated" in n for n in sheet["degraded"])


def test_llm_down_degrades_not_crashes():
    sheet = research.build_research("legal virtual assistant", {"metrics": {}, "competitor_keywords": {}},
                                    search=fake_search, fetch=fake_fetch, llm=no_llm)
    assert sheet["competitors"][0]["intent"] == ""
    assert len(sheet["lsi"]) >= 1  # SERP-derived fallback
    assert sheet["degraded"]


def test_serper_down_propagates():
    def down(q):
        raise CredentialMissing("SEO_SERPER_API_KEY not set")
    with pytest.raises(CredentialMissing):
        research.build_research("x", {"metrics": {}, "competitor_keywords": {}},
                                search=down, fetch=fake_fetch, llm=fake_llm)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_research.py" -v`
Expected: FAIL — module missing

- [ ] **Step 3: Write the implementation**

`research.py`:

```python
"""Stage 1 — team steps 1-4: SERP intel, competitor classification, keyword gap,
usage benchmarks, LSI. Output: the Keyword Target Sheet (Gate 1)."""
from __future__ import annotations

import re
from collections import Counter

from seo_geo_agent import sources
from seo_geo_agent.sources import CredentialMissing

from . import rules

_STOP = frozenset(
    "a an and are as at be but by can do for from has have how i if in is it of on or our "
    "the this that to was we what when where which who why will with you your".split()
)


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", text.lower()) if t not in _STOP and len(t) > 2}


def _token_list(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9']+", text.lower()) if t not in _STOP and len(t) > 2]


def phrase_count(text: str, phrase: str) -> int:
    return len(re.findall(re.escape(phrase.lower()), text.lower()))


_QUESTION_STARTS = ("what", "how", "why", "who", "can", "do", "does", "is", "are", "should")


def _tag_for(keyword: str, main: str) -> str:
    kl = keyword.lower().strip()
    if kl == main.lower():
        return "main"
    if kl.endswith("?") or (kl.split() and kl.split()[0] in _QUESTION_STARTS):
        return "aio"
    if len(kl.split()) >= 5:
        return "long_tail"
    return "secondary"


def _classify(pages: list[dict], keyword: str, llm) -> tuple[list[dict], list[str]]:
    """Intent / page type / audience per competitor (team step 3a)."""
    try:
        raw = llm(
            'You are an SEO analyst. JSON only: {"pages": [{"url": str, "intent": str, '
            '"page_type": str, "audience": str}]}. intent must be one of '
            "informational|commercial|transactional|navigational.",
            f"Classify these pages ranking for '{keyword}': "
            + "; ".join(f"{p['url']} (title: {p['title']}, h1: {p['h1']})" for p in pages),
        )
        by_url = {p.get("url"): p for p in raw.get("pages", []) if isinstance(p, dict)}
        out = [
            {**p, **{k: str(by_url.get(p["url"], {}).get(k, ""))[:80]
                     for k in ("intent", "page_type", "audience")}}
            for p in pages
        ]
        return out, []
    except CredentialMissing as exc:
        return ([{**p, "intent": "", "page_type": "", "audience": ""} for p in pages],
                [f"competitor classification skipped ({exc})"])


def _gap(keyword: str, serp: dict, competitor_rows: dict[str, list[dict]]) -> tuple[list[dict], list[str]]:
    """Keyword gap (team step 3b). Pasted Ahrefs rows when present; honest SERP fallback."""
    if any(competitor_rows.values()):
        merged: dict[str, dict] = {}
        for url, rows in competitor_rows.items():
            for r in rows:
                e = merged.setdefault(r["keyword"].lower(),
                                      {"keyword": r["keyword"], "volume": r["volume"], "in": set()})
                e["in"].add(url)
                e["volume"] = e["volume"] or r["volume"]
        out = [
            {"keyword": e["keyword"], "tag": _tag_for(e["keyword"], keyword),
             "volume": e["volume"], "overlap": len(e["in"]), "source": "ahrefs_pasted"}
            for e in sorted(merged.values(), key=lambda x: -(x["volume"] or 0))
        ]
        return out[:40], []
    est = [{"keyword": q, "tag": "aio", "volume": None, "overlap": 0, "source": "serp_estimated"}
           for q in serp["paa"][:8]]
    est += [{"keyword": r, "tag": _tag_for(r, keyword), "volume": None, "overlap": 0,
             "source": "serp_estimated"}
            for r in serp["related"][:12] if r.lower() != keyword.lower()]
    return est, ["no Ahrefs competitor keywords pasted — gap list is SERP-estimated (no volume data)"]


def _lsi(keyword: str, serp: dict, frequent: list[str], llm) -> tuple[list[dict], list[str]]:
    """LSI top-10, natural fits only (team step 4)."""
    notes: list[str] = []
    try:
        raw = llm(
            'JSON only: {"lsi": [{"term": str, "fit_note": str}]}.',
            f"Give the {rules.LSI_COUNT} best LSI/semantic keywords for an article targeting "
            f"'{keyword}'. Only terms that fit naturally — reject anything that would be a forced "
            f"insertion. One-line fit_note each on where it belongs. Candidates — related searches: "
            f"{serp['related'][:10]}; frequent competitor terms: {frequent[:10]}.",
        )
        items = [{"term": str(i.get("term", ""))[:60], "fit_note": str(i.get("fit_note", ""))[:160]}
                 for i in raw.get("lsi", []) if isinstance(i, dict) and i.get("term")]
        if items:
            return items[:rules.LSI_COUNT], notes
        notes.append("LLM returned no LSI terms — using SERP-derived terms")
    except CredentialMissing as exc:
        notes.append(f"LSI via LLM skipped ({exc}) — using SERP-derived terms")
    fallback = [r for r in serp["related"] if r.lower() != keyword.lower()][:rules.LSI_COUNT]
    return [{"term": t, "fit_note": "from related searches"} for t in fallback], notes


def build_research(keyword: str, pasted: dict, search=None, fetch=None, llm=None) -> dict:
    search = search or sources.serper_search
    fetch = fetch or sources.fetch_page
    llm = llm or sources.llm_json
    degraded: list[str] = []

    serp = search(keyword)  # CredentialMissing propagates — router turns it into a 503
    pages: list[dict] = []
    for r in serp["organic"][:rules.TOP_N]:
        f = fetch(r["link"])
        pages.append({"url": r["link"], "title": r["title"] or f.title, "position": r["position"],
                      "h1": (f.h1[:1] or [""])[0], "word_count": f.word_count, "text": f.text})
        if f.status != 200:
            degraded.append(f"could not fully fetch {r['link']} (status {f.status})")

    classified, notes = _classify(
        [{k: p[k] for k in ("url", "title", "h1")} for p in pages], keyword, llm)
    degraded += notes
    gap, notes = _gap(keyword, serp, pasted.get("competitor_keywords") or {})
    degraded += notes

    top1_text = pages[0]["text"] if pages else ""
    main_count = phrase_count(top1_text, keyword)
    frequent = [{"term": t, "count": c}
                for t, c in Counter(_token_list(top1_text)).most_common(rules.FREQUENT_TERMS)]
    lsi, notes = _lsi(keyword, serp, [f["term"] for f in frequent], llm)
    degraded += notes

    intents = {c["intent"] for c in classified if c["intent"]}
    metrics = {"volume": None, "kd": None, "traffic_potential": None, **(pasted.get("metrics") or {})}
    has_ahrefs = metrics.get("volume") is not None or any((pasted.get("competitor_keywords") or {}).values())
    return {
        "keyword": keyword,
        "metrics": metrics,
        "serp": {"top3": [{"url": p["url"], "title": p["title"], "position": p["position"]} for p in pages],
                 "paa": serp["paa"], "related": serp["related"], "aio_present": serp["aio_present"]},
        "competitors": [{k: c[k] for k in ("url", "intent", "page_type", "audience")} for c in classified],
        "mixed_intent": len(intents) > 1,
        "gap": gap,
        "usage": {"main_count_top1": main_count,
                  "target_min": max(main_count, 1),
                  "target_max": max(main_count, 1) + rules.KEYWORD_COUNT_BONUS,
                  "frequent_terms": frequent},
        "lsi": lsi,
        "data_source": "ahrefs_pasted" if has_ahrefs else "serp_estimated",
        "degraded": degraded,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_research.py" -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add "agents/SEO Blog agent"
git commit -m "feat(blog): Stage-1 research engine — SERP intel, gap, benchmarks, LSI"
```

---

### Task 4: Run lifecycle — router part 1 (kickoff, list, detail, Gate 1)

**Files:**
- Create: `app/routers/seo_blog.py`
- Modify: `app/main.py` (add `seo_blog` to the `from app.routers import (...)` tuple and to the `for router in (...)` registration tuple)
- Test: `app/routers/tests/test_seo_blog_router.py`

**Interfaces:**
- Consumes: `research.build_research`, `ahrefs_paste.*`, `state.save/load`.
- Produces HTTP API (all under `/api`, auth = `get_current_user`):
  - `POST /seo-blog/runs` body `{keyword, metrics_paste?, competitor_keywords_paste?: {url: csv_text}}` → full run doc (research runs inline). 503 when Serper credentials missing. 422 on empty keyword.
  - `GET /seo-blog/runs` → `{"runs": [{id, keyword, created, stage}]}` (newest first).
  - `GET /seo-blog/runs/{id}` → full run doc; 404 unknown.
  - `POST /seo-blog/runs/{id}/approve-keywords` body `{sheet}` (writer-edited sheet) → run doc with `gates.keywords=true`, `stage="outline"`.
- Also produces module helpers later tasks reuse: `_get_run(run_id) -> dict` (404-raising), `_save_run(run) -> dict` (persists + updates `runs-index`).

- [ ] **Step 1: Write the failing test**

`app/routers/tests/test_seo_blog_router.py` (pattern copied from `test_seo_geo_router.py`):

```python
"""Integration tests for the SEO Blog router (/api/seo-blog). Fully offline."""

import os

os.environ["SEO_OFFLINE"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.security import get_current_user
from app.routers import seo_blog as blog_router

USER = {"id": "u1", "email": "t@legalsoft.com", "is_admin": False, "is_creator": False}
client = TestClient(app)

SHEET = {"keyword": "legal virtual assistant", "metrics": {"volume": 5400, "kd": 42, "traffic_potential": None},
         "serp": {"top3": [{"url": "https://a.com/post", "title": "T", "position": 1}], "paa": [], "related": [],
                  "aio_present": False},
         "competitors": [], "mixed_intent": False,
         "gap": [{"keyword": "legal virtual assistant", "tag": "main", "volume": 5400, "overlap": 1,
                  "source": "ahrefs_pasted"}],
         "usage": {"main_count_top1": 4, "target_min": 4, "target_max": 6, "frequent_terms": []},
         "lsi": [{"term": "virtual paralegal", "fit_note": "n"}],
         "data_source": "ahrefs_pasted", "degraded": []}


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_OFFLINE", "1")
    monkeypatch.setenv("SEO_BLOG_LOCAL_DIR", str(tmp_path))
    app.dependency_overrides[get_current_user] = lambda: dict(USER)
    monkeypatch.setattr(blog_router.research, "build_research",
                        lambda keyword, pasted, **kw: dict(SHEET, keyword=keyword))
    yield
    app.dependency_overrides.pop(get_current_user, None)


def test_kickoff_creates_run_with_sheet():
    body = client.post("/api/seo-blog/runs", json={"keyword": "legal virtual assistant"}).json()
    assert body["stage"] == "research"
    assert body["sheet"]["keyword"] == "legal virtual assistant"
    assert body["gates"] == {"keywords": False, "outline": False}
    runs = client.get("/api/seo-blog/runs").json()["runs"]
    assert runs[0]["id"] == body["id"]


def test_kickoff_parses_pastes():
    payload = {"keyword": "legal virtual assistant",
               "metrics_paste": "Volume: 5.4K\nKD: 42",
               "competitor_keywords_paste": {"https://a.com/post": "Keyword,Volume\nx,100\n"}}
    body = client.post("/api/seo-blog/runs", json=payload).json()
    assert body["pasted"]["metrics"]["volume"] == 5400
    assert body["pasted"]["competitor_keywords"]["https://a.com/post"][0]["keyword"] == "x"


def test_kickoff_rejects_blank_keyword():
    assert client.post("/api/seo-blog/runs", json={"keyword": "  "}).status_code == 422


def test_kickoff_503_when_serper_missing(monkeypatch):
    from seo_geo_agent.sources import CredentialMissing

    def down(keyword, pasted, **kw):
        raise CredentialMissing("SEO_SERPER_API_KEY not set")
    monkeypatch.setattr(blog_router.research, "build_research", down)
    r = client.post("/api/seo-blog/runs", json={"keyword": "x"})
    assert r.status_code == 503
    assert "SEO_SERPER_API_KEY" in r.json()["detail"]


def test_gate1_approve_keywords():
    run = client.post("/api/seo-blog/runs", json={"keyword": "legal virtual assistant"}).json()
    edited = dict(SHEET, lsi=[{"term": "edited term", "fit_note": "writer edit"}])
    body = client.post(f"/api/seo-blog/runs/{run['id']}/approve-keywords", json={"sheet": edited}).json()
    assert body["gates"]["keywords"] is True
    assert body["stage"] == "outline"
    assert body["sheet"]["lsi"][0]["term"] == "edited term"


def test_run_404():
    assert client.get("/api/seo-blog/runs/nope").status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest app/routers/tests/test_seo_blog_router.py -v`
Expected: FAIL — `ImportError: cannot import name 'seo_blog'`

- [ ] **Step 3: Write the implementation**

`app/routers/seo_blog.py`:

```python
"""SEO Blog Writer API — the content team's 12-step process as a 3-gate pipeline.

Mounted under ``/api/seo-blog``. Auth: any signed-in user. Spec:
docs/superpowers/specs/2026-07-30-seo-blog-agent-design.md
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.security import get_current_user
from seo_blog_agent import ahrefs_paste, research, rules, state
from seo_geo_agent.sources import CredentialMissing

router = APIRouter()
logger = logging.getLogger("agentos.seo_blog")

BLOG_AGENT_ID = "a9"  # "SEO Blog Writer" slot in the frontend agent catalog


class RunIn(BaseModel):
    keyword: str
    metrics_paste: str = ""
    competitor_keywords_paste: dict[str, str] = {}


class SheetIn(BaseModel):
    sheet: dict


def _get_run(run_id: str) -> dict:
    run = state.load(f"run-{run_id}")
    if not run:
        raise HTTPException(404, "Run not found")
    return run


def _save_run(run: dict) -> dict:
    state.save(f"run-{run['id']}", run)
    index = state.load("runs-index") or {"runs": []}
    entry = {"id": run["id"], "keyword": run["keyword"], "created": run["created"], "stage": run["stage"]}
    index["runs"] = [entry] + [r for r in index["runs"] if r["id"] != run["id"]]
    state.save("runs-index", index)
    return run


@router.post("/seo-blog/runs")
def kickoff(payload: RunIn, user=Depends(get_current_user)):
    keyword = payload.keyword.strip()
    if not keyword:
        raise HTTPException(422, "keyword is required")
    pasted = {
        "metrics": ahrefs_paste.parse_metrics(payload.metrics_paste),
        "competitor_keywords": {url: ahrefs_paste.parse_competitor_csv(text)
                                for url, text in payload.competitor_keywords_paste.items()},
        "dr": {},
    }
    try:
        sheet = research.build_research(keyword, pasted)
    except CredentialMissing as exc:
        raise HTTPException(503, f"Research data source unavailable: {exc}") from exc
    run_id = hashlib.sha1(f"{keyword.lower()}|{date.today().isoformat()}".encode()).hexdigest()[:10]
    run = {"id": run_id, "keyword": keyword, "created": date.today().isoformat(),
           "stage": "research", "gates": {"keywords": False, "outline": False},
           "pasted": pasted, "sheet": sheet, "outline_doc": None, "citations": None, "draft": None}
    return _save_run(run)


@router.get("/seo-blog/runs")
def list_runs(user=Depends(get_current_user)):
    return {"runs": (state.load("runs-index") or {"runs": []})["runs"]}


@router.get("/seo-blog/runs/{run_id}")
def get_run(run_id: str, user=Depends(get_current_user)):
    return _get_run(run_id)


@router.post("/seo-blog/runs/{run_id}/approve-keywords")
def approve_keywords(run_id: str, payload: SheetIn, user=Depends(get_current_user)):
    run = _get_run(run_id)
    run["sheet"] = payload.sheet
    run["gates"]["keywords"] = True
    run["stage"] = "outline"
    return _save_run(run)
```

In `app/main.py`: add `seo_blog,` to the `from app.routers import (...)` block (alphabetical, after `seo_geo`... note the tuple is alphabetical — put `seo_blog` before `seo_geo`) and add `seo_blog,` to the `for router in (...)` tuple.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest app/routers/tests/test_seo_blog_router.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add app/routers/seo_blog.py app/routers/tests/test_seo_blog_router.py app/main.py
git commit -m "feat(blog): run lifecycle API — kickoff with paste-in research, Gate 1"
```

---

### Task 5: outline.py — Stage 2 engine (team steps 5-8)

**Files:**
- Create: `agents/SEO Blog agent/seo_blog_agent/outline.py`
- Test: `agents/SEO Blog agent/seo_blog_agent/tests/test_outline.py`

**Interfaces:**
- Consumes: sheet shape from Task 3; a2 sources (injectable).
- Produces:
  - `outline.competitor_profile(url: str, fetch=None, fetch_raw=None, llm=None) -> dict` — `{url, title, meta_description, h1, h2, h3, faqs, word_count, external_links, schema_types, available, features: {eeat, key_takeaways, tables, tools, lacks: [str]}, degraded: [str]}`
  - `outline.build_outline(sheet: dict, profiles: list[dict], llm=None) -> dict` — the `outline_doc` (shape in header). Evaluator loop runs at most `rules.EVALUATOR_MAX_ROUNDS`; on LLM failure the structural fallback outline is used and noted.

- [ ] **Step 1: Write the failing test**

`tests/test_outline.py`:

```python
from seo_geo_agent.sources import CredentialMissing, PageFacts
from seo_blog_agent import outline, rules

SHEET = {"keyword": "legal virtual assistant",
         "serp": {"paa": ["What does a legal VA do?"], "related": ["legal va cost"], "aio_present": True,
                  "top3": [{"url": "https://a.com/post", "title": "T", "position": 1}]},
         "gap": [{"keyword": "virtual paralegal", "tag": "secondary", "volume": 100, "overlap": 1,
                  "source": "ahrefs_pasted"}],
         "usage": {"main_count_top1": 4, "target_min": 4, "target_max": 6, "frequent_terms": []},
         "lsi": [{"term": "virtual paralegal", "fit_note": "n"}], "degraded": []}


def fake_fetch(url):
    return PageFacts(url=url, status=200, title="Best Legal VAs 2026", meta_description="desc",
                     h1=["Best Legal VAs"], h2=["What is a legal VA", "Costs", "Hiring steps"],
                     h3=["FAQ: What does a legal VA do?"], word_count=2000,
                     text="body " * 500, schema_types=["Article"])


def fake_fetch_raw(url):
    return {"status": 200, "final_url": url,
            "text": '<a href="https://clio.com/report">x</a> <a href="https://a.com/other">i</a>'
                    ' <a href="https://www.abajournal.com/s">y</a>'}


def fake_llm_ok(system, prompt):
    if '"eeat"' in system:
        return {"eeat": True, "key_takeaways": False, "tables": True, "tools": False,
                "lacks": ["no pricing table"]}
    if '"our_score"' in system:
        return {"our_score": 92, "competitor_scores": [80, 75, 70], "beats_all": True, "weaknesses": []}
    if '"title"' in system:
        return {"title": "Legal Virtual Assistant: The 2026 Hiring Guide",
                "description": "How to hire a legal VA.", "slug": "legal-virtual-assistant"}
    return {"outline": [{"heading": "What is a legal virtual assistant?", "level": 2,
                         "note": "answer in 2 sentences", "keywords": ["legal virtual assistant"]}]}


def no_llm(system, prompt):
    raise CredentialMissing("no key")


def test_profile_extracts_structure_and_external_links():
    p = outline.competitor_profile("https://a.com/post", fetch=fake_fetch,
                                   fetch_raw=fake_fetch_raw, llm=fake_llm_ok)
    assert p["h2"] == ["What is a legal VA", "Costs", "Hiring steps"]
    assert p["external_links"] == 2  # clio.com + abajournal.com; own-domain link excluded
    assert p["features"]["lacks"] == ["no pricing table"]
    assert p["available"] is True


def test_build_outline_targets_and_meta():
    profiles = [outline.competitor_profile(f"https://{d}.com/post", fetch=fake_fetch,
                                           fetch_raw=fake_fetch_raw, llm=fake_llm_ok)
                for d in ("a", "b", "c")]
    doc = outline.build_outline(SHEET, profiles, llm=fake_llm_ok)
    assert doc["targets"]["word_count"] == round(2000 * (1 + rules.TARGET_UPLIFT))
    assert doc["targets"]["links"] == max(rules.MIN_LINKS, round(2 * (1 + rules.TARGET_UPLIFT)))
    assert doc["meta"]["slug"] == "legal-virtual-assistant"
    assert doc["evaluator"]["beats_all"] is True
    assert doc["outline"][0]["heading"].startswith("What is")


def test_evaluator_never_lies_when_it_cannot_win():
    calls = {"n": 0}

    def llm(system, prompt):
        if '"our_score"' in system:
            calls["n"] += 1
            return {"our_score": 60, "competitor_scores": [80], "beats_all": False, "weaknesses": ["thin"]}
        return fake_llm_ok(system, prompt)

    profiles = [outline.competitor_profile("https://a.com/post", fetch=fake_fetch,
                                           fetch_raw=fake_fetch_raw, llm=fake_llm_ok)]
    doc = outline.build_outline(SHEET, profiles, llm=llm)
    assert calls["n"] == rules.EVALUATOR_MAX_ROUNDS
    assert doc["evaluator"]["beats_all"] is False
    assert "honest" in doc["evaluator"]["note"]


def test_llm_down_gives_structural_fallback():
    profiles = [outline.competitor_profile("https://a.com/post", fetch=fake_fetch,
                                           fetch_raw=fake_fetch_raw, llm=no_llm)]
    doc = outline.build_outline(SHEET, profiles, llm=no_llm)
    assert doc["outline"]  # structural fallback from shared competitor themes
    assert any("skipped" in n or "fallback" in n for n in doc["degraded"])
    assert doc["evaluator"]["beats_all"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_outline.py" -v`
Expected: FAIL — module missing

- [ ] **Step 3: Write the implementation**

`outline.py`:

```python
"""Stage 2 — team steps 5-8: competitor outlines + feature audit, meta drafting,
word/link targets, our outline + the honest evaluator loop."""
from __future__ import annotations

import re

from seo_geo_agent import sources
from seo_geo_agent.sources import CredentialMissing

from . import rules
from .research import tokens


def _external_links(html: str, own_domain: str) -> int:
    hosts = set(re.findall(r'href="https?://([^/">]+)', html))
    cleaned = {h[4:] if h.startswith("www.") else h for h in (x.lower() for x in hosts)}
    return len({h for h in cleaned if own_domain not in h})


def competitor_profile(url: str, fetch=None, fetch_raw=None, llm=None) -> dict:
    fetch = fetch or sources.fetch_page
    fetch_raw = fetch_raw or sources.fetch_text
    llm = llm or sources.llm_json
    f = fetch(url)
    raw = fetch_raw(url)
    profile = {
        "url": url, "title": f.title, "meta_description": f.meta_description,
        "h1": f.h1, "h2": f.h2, "h3": f.h3, "faqs": f.questions,
        "word_count": f.word_count, "external_links": _external_links(raw["text"], sources.domain_of(url)),
        "schema_types": f.schema_types, "available": f.status == 200, "degraded": [],
    }
    try:
        audit = llm(
            'JSON only: {"eeat": bool, "key_takeaways": bool, "tables": bool, "tools": bool, '
            '"lacks": [str]}.',
            f"Feature-audit this ranking page (team step 5). Headings: {f.h2 + f.h3}. "
            f"First 2000 chars: {f.text[:2000]}. eeat = visible author credentials, citations, "
            "first-hand expertise. lacks = up to 4 concrete things missing that would help readers.",
        )
        profile["features"] = {k: bool(audit.get(k)) for k in ("eeat", "key_takeaways", "tables", "tools")}
        profile["features"]["lacks"] = [str(s)[:120] for s in audit.get("lacks", []) if s][:4]
    except CredentialMissing as exc:
        profile["features"] = {"eeat": False, "key_takeaways": False,
                               "tables": "<table" in raw["text"].lower(), "tools": False, "lacks": []}
        profile["degraded"].append(f"feature audit skipped ({exc}) — structural facts only")
    return profile


def _slug(keyword: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")


def _meta(sheet: dict, profiles: list[dict], llm) -> tuple[dict, list[str]]:
    """Team step 6: study competitor metas, draft a better unique one."""
    try:
        raw = llm(
            'JSON only: {"title": str, "description": str, "slug": str}.',
            f"Competitor metas for '{sheet['keyword']}': "
            + "; ".join(f"title={p['title']!r} desc={p['meta_description']!r}" for p in profiles)
            + ". Write a unique, better meta title (<=60 chars), description (<=155 chars) and a "
            "short hyphenated url slug for our article.",
        )
        return ({"title": str(raw.get("title", ""))[:70], "description": str(raw.get("description", ""))[:170],
                 "slug": _slug(str(raw.get("slug", "")) or sheet["keyword"])}, [])
    except CredentialMissing as exc:
        return ({"title": sheet["keyword"].title(), "description": f"A practical guide to {sheet['keyword']}.",
                 "slug": _slug(sheet["keyword"])}, [f"meta drafting skipped ({exc}) — keyword-derived meta"])


def _clean(items) -> list[dict]:
    out = []
    for o in items or []:
        if isinstance(o, dict) and o.get("heading"):
            out.append({"heading": str(o["heading"])[:120],
                        "level": int(o.get("level", 2)) if str(o.get("level", 2)).isdigit() else 2,
                        "note": str(o.get("note", ""))[:300],
                        "keywords": [str(k)[:60] for k in o.get("keywords", [])][:6]})
    return out


def _fallback_outline(sheet: dict, profiles: list[dict]) -> list[dict]:
    """Structural outline from competitor headings shared by 2+ pages (a2 briefs pattern)."""
    seen: dict[str, int] = {}
    order: list[str] = []
    for p in profiles:
        for h in p["h2"]:
            key = " ".join(sorted(tokens(h))) or h.lower()
            if key not in seen:
                order.append(h)
            seen[key] = seen.get(key, 0) + 1
    shared = [h for h in order if seen[" ".join(sorted(tokens(h))) or h.lower()] >= 2] or order[:8]
    items = [{"heading": f"What is {sheet['keyword']}?", "level": 2,
              "note": "Answer the query in the first 2 sentences — AI Overviews quote this.",
              "keywords": [sheet["keyword"]]}]
    items += [{"heading": h, "level": 2, "note": "Covered by the top-ranking pages — match and beat it.",
               "keywords": []} for h in shared[:8]]
    if sheet["serp"]["paa"]:
        items.append({"heading": "FAQ", "level": 2,
                      "note": "Answer each: " + "; ".join(sheet["serp"]["paa"][:6]), "keywords": []})
    return items


def _generate(sheet: dict, profiles: list[dict], targets: dict, llm) -> tuple[list[dict], list[str]]:
    """Team step 8: cover what competitors have, add what they lack, weave gap keywords."""
    lacks = [l for p in profiles for l in p["features"]["lacks"]]
    kw_by_tag = {t: [g["keyword"] for g in sheet["gap"] if g["tag"] == t]
                 for t in ("secondary", "long_tail", "aio")}
    try:
        raw = llm(
            'JSON only: {"outline": [{"heading": str, "level": int, "note": str, "keywords": [str]}]}.',
            f"Build the section outline for an article targeting '{sheet['keyword']}' "
            f"(~{targets['word_count']} words). Competitor outlines: "
            + "; ".join(f"{p['url']}: {p['h2'][:10]}" for p in profiles)
            + f". Things they lack (add these): {lacks[:8]}. Weave in secondary keywords "
            f"{kw_by_tag['secondary'][:8]}, long-tail {kw_by_tag['long_tail'][:6]}, and answer these "
            f"AI-overview questions {kw_by_tag['aio'][:6] + sheet['serp']['paa'][:4]}. Include a "
            "key-takeaways section near the top and an FAQ section. 8-12 headings, each with a "
            "one-line writer note and its target keywords.",
        )
        items = _clean(raw.get("outline"))
        if items:
            return items, []
        return _fallback_outline(sheet, profiles), ["LLM returned no outline — structural fallback used"]
    except CredentialMissing as exc:
        return _fallback_outline(sheet, profiles), [f"outline LLM skipped ({exc}) — structural fallback used"]


def _evaluate(sheet: dict, profiles: list[dict], items: list[dict], llm) -> tuple[dict, list[dict]]:
    """Team step 8 evaluator: our outline vs competitors, revise up to 3 rounds, never lie."""
    scores: dict = {}
    for rnd in range(1, rules.EVALUATOR_MAX_ROUNDS + 1):
        try:
            verdict = llm(
                'JSON only: {"our_score": int, "competitor_scores": [int], "beats_all": bool, '
                '"weaknesses": [str]}. Scores 0-100 for coverage, intent match, differentiation.',
                f"Keyword: '{sheet['keyword']}'. Our outline: {[o['heading'] for o in items]}. "
                f"Competitor outlines: {[{'url': p['url'], 'h2': p['h2'][:12]} for p in profiles]}.",
            )
        except CredentialMissing as exc:
            return {"rounds": rnd - 1, "beats_all": None, "scores": scores,
                    "note": f"evaluator skipped ({exc})"}, items
        scores = {"our_score": verdict.get("our_score"),
                  "competitor_scores": verdict.get("competitor_scores", [])}
        if verdict.get("beats_all"):
            return {"rounds": rnd, "beats_all": True, "scores": scores, "note": ""}, items
        try:
            revised = llm(
                'JSON only: {"outline": [{"heading": str, "level": int, "note": str, "keywords": [str]}]}.',
                f"Revise this outline to fix these weaknesses: {verdict.get('weaknesses', [])}. "
                f"Keep the strong sections. Outline: {items}",
            )
            items = _clean(revised.get("outline")) or items
        except CredentialMissing:
            break
    return {"rounds": rules.EVALUATOR_MAX_ROUNDS, "beats_all": False, "scores": scores,
            "note": "did not beat all competitors in "
                    f"{rules.EVALUATOR_MAX_ROUNDS} rounds — best version shown with honest scores"}, items


def build_outline(sheet: dict, profiles: list[dict], llm=None) -> dict:
    llm = llm or sources.llm_json
    degraded: list[str] = [n for p in profiles for n in p["degraded"]]
    avail = [p for p in profiles if p["available"]]
    wc = [p["word_count"] for p in avail if p["word_count"]]
    links = [p["external_links"] for p in avail]
    targets = {
        "word_count": round((sum(wc) / len(wc)) * (1 + rules.TARGET_UPLIFT)) if wc else 1500,
        "links": max(rules.MIN_LINKS,
                     round((sum(links) / len(links)) * (1 + rules.TARGET_UPLIFT)) if links else rules.MIN_LINKS),
    }
    meta, notes = _meta(sheet, profiles, llm)
    degraded += notes
    items, notes = _generate(sheet, profiles, targets, llm)
    degraded += notes
    evaluator, items = _evaluate(sheet, profiles, items, llm)
    return {"competitor_outlines": profiles, "meta": meta, "targets": targets,
            "outline": items, "evaluator": evaluator, "degraded": degraded}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_outline.py" -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add "agents/SEO Blog agent"
git commit -m "feat(blog): Stage-2 outline engine — profiles, meta, targets, honest evaluator"
```

---

### Task 6: citations.py — verified sources only (team steps 9-10)

**Files:**
- Create: `agents/SEO Blog agent/seo_blog_agent/citations.py`
- Test: `agents/SEO Blog agent/seo_blog_agent/tests/test_citations.py`

**Interfaces:**
- Consumes: `outline_doc` shape, `research.tokens`, `rules`, a2 `fetch_text`/`llm_json`/`domain_of` (injectable).
- Produces:
  - `citations.source_citations(outline_doc: dict, dr_pasted: dict[str, int], llm=None, fetch_raw=None) -> dict` — the `citations` doc. Loop: at most `rules.CITATION_MAX_ROUNDS` LLM rounds; every candidate URL is live-fetched; claim must appear on the page (token overlap); DR below `rules.DR_THRESHOLD` rejected when known; unknown DR → `dr_status="unverified"` (kept, honestly flagged).
  - `citations.revet(doc: dict, dr_pasted: dict[str, int], target: int) -> dict` — re-apply DR to existing items (Gate-2 DR paste), drop failures, recompute `short_by`.

- [ ] **Step 1: Write the failing test**

`tests/test_citations.py`:

```python
from seo_geo_agent.sources import CredentialMissing
from seo_blog_agent import citations, rules

OUTLINE_DOC = {"targets": {"word_count": 2300, "links": 2},
               "outline": [{"heading": "Costs", "level": 2, "note": "", "keywords": []},
                           {"heading": "Hiring steps", "level": 2, "note": "", "keywords": []}]}

GOOD = {"claim": "78% of firms outsource intake", "source_name": "Clio 2025 Legal Trends Report",
        "url": "https://clio.com/report", "section": "Costs"}
DEAD = {"claim": "50% stat", "source_name": "Dead Source Annual Study", "url": "https://dead.com/x",
        "section": "Costs"}
OFFPAGE = {"claim": "totally different subject entirely", "source_name": "Mismatch Weekly Review",
           "url": "https://mismatch.com/y", "section": "Hiring steps"}

PAGES = {
    "https://clio.com/report": {"status": 200, "final_url": "https://clio.com/report",
                                "text": "Clio 2025 Legal Trends Report: 78% of firms outsource intake."},
    "https://dead.com/x": {"status": 404, "final_url": "https://dead.com/x", "text": ""},
    "https://mismatch.com/y": {"status": 200, "final_url": "https://mismatch.com/y",
                               "text": "unrelated page about gardening tips and tomato soil"},
    "https://aba.org/study": {"status": 200, "final_url": "https://aba.org/study",
                              "text": "ABA 2026 Tech Survey shows 61% adoption of legal automation."},
}


def fake_fetch_raw(url):
    return PAGES.get(url, {"status": 0, "final_url": url, "text": ""})


def test_only_verified_citations_enter():
    def llm(system, prompt):
        return {"citations": [GOOD, DEAD, OFFPAGE]}
    doc = citations.source_citations(OUTLINE_DOC, {}, llm=llm, fetch_raw=fake_fetch_raw)
    assert [c["url"] for c in doc["items"]] == ["https://clio.com/report"]
    assert doc["items"][0]["verified"] is True
    assert doc["items"][0]["dr_status"] == "unverified"  # no DR pasted — honest flag
    assert doc["short_by"] == 1


def test_dr_enforced_when_known():
    def llm(system, prompt):
        return {"citations": [GOOD]}
    doc = citations.source_citations(OUTLINE_DOC, {"clio.com": 45}, llm=llm, fetch_raw=fake_fetch_raw)
    assert doc["items"] == []  # DR 45 < 70 → rejected
    doc = citations.source_citations(OUTLINE_DOC, {"clio.com": 91}, llm=llm, fetch_raw=fake_fetch_raw)
    assert doc["items"][0]["dr"] == 91 and doc["items"][0]["dr_status"] == "ok"


def test_retry_rounds_cap():
    calls = {"n": 0}

    def llm(system, prompt):
        calls["n"] += 1
        return {"citations": [DEAD]}
    doc = citations.source_citations(OUTLINE_DOC, {}, llm=llm, fetch_raw=fake_fetch_raw)
    assert calls["n"] == rules.CITATION_MAX_ROUNDS
    assert doc["short_by"] == 2


def test_llm_down_is_honest():
    def llm(system, prompt):
        raise CredentialMissing("no key")
    doc = citations.source_citations(OUTLINE_DOC, {}, llm=llm, fetch_raw=fake_fetch_raw)
    assert doc["items"] == [] and doc["short_by"] == 2 and doc["degraded"]


def test_revet_applies_gate2_dr_paste():
    def llm(system, prompt):
        return {"citations": [GOOD, {"claim": "61% adoption of legal automation",
                                     "source_name": "ABA 2026 Tech Survey",
                                     "url": "https://aba.org/study", "section": "Hiring steps"}]}
    doc = citations.source_citations(OUTLINE_DOC, {}, llm=llm, fetch_raw=fake_fetch_raw)
    assert len(doc["items"]) == 2
    out = citations.revet(doc, {"clio.com": 91, "aba.org": 55}, target=2)
    assert [c["domain"] for c in out["items"]] == ["clio.com"]
    assert out["items"][0]["dr_status"] == "ok"
    assert out["short_by"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_citations.py" -v`
Expected: FAIL — module missing

- [ ] **Step 3: Write the implementation**

`citations.py`:

```python
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
            page = fetch_raw(url)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_citations.py" -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add "agents/SEO Blog agent"
git commit -m "feat(blog): citation engine — live-verified, DR-vetted, never hallucinated"
```

---

### Task 7: drafting.py + v1 guidelines (team steps 11-12)

**Files:**
- Create: `agents/SEO Blog agent/seo_blog_agent/drafting.py`
- Create: `agents/SEO Blog agent/seo_blog_agent/guidelines.md`
- Test: `agents/SEO Blog agent/seo_blog_agent/tests/test_drafting.py`

**Interfaces:**
- Consumes: sheet, outline_doc, citations shapes; a2 `llm_text` (injectable).
- Produces:
  - `drafting.build_draft(sheet, outline_doc, citations, llm=None) -> dict` — the `draft` doc; raises `CredentialMissing` when the LLM is down (router → 503; a fake draft is worse than no draft).
  - `drafting.check_compliance(markdown: str, sheet, outline_doc, citations) -> dict` — pure function `{checks: [{id,label,pass,detail}], all_pass}`; check ids: `kw_count, lsi, word_count, citations, sections, meta`.
  - `drafting.to_docx(markdown: str) -> bytes` — python-docx conversion (headings `#`-`####`, `-`/`*` bullets, plain paragraphs; markdown links flattened to "text (url)").

- [ ] **Step 1: Write the failing test**

`tests/test_drafting.py`:

```python
import io

import pytest
from docx import Document

from seo_geo_agent.sources import CredentialMissing
from seo_blog_agent import drafting

SHEET = {"keyword": "legal virtual assistant",
         "usage": {"target_min": 2, "target_max": 4, "main_count_top1": 2, "frequent_terms": []},
         "lsi": [{"term": "virtual paralegal", "fit_note": "n"}]}
OUTLINE_DOC = {"targets": {"word_count": 40, "links": 1},
               "meta": {"title": "T", "description": "D", "slug": "s"},
               "outline": [{"heading": "Costs", "level": 2, "note": "", "keywords": []}]}
CITATIONS = {"items": [{"id": "c1", "claim": "x", "source_name": "Clio 2025 Legal Trends Report",
                        "url": "https://clio.com/report", "domain": "clio.com", "dr": 91,
                        "dr_status": "ok", "section": "Costs", "verified": True}],
             "short_by": 0, "rounds": 1, "degraded": []}

GOOD_DRAFT = ("# T\n\n## Costs\n\nA legal virtual assistant helps firms. A legal virtual assistant "
              "and a virtual paralegal cut intake costs, per the [Clio 2025 Legal Trends Report]"
              "(https://clio.com/report). " + "Extra words here. " * 6)
# word budget: 47 tokens total — inside the compliance band for target 40 (36..50).


def test_compliance_all_pass():
    c = drafting.check_compliance(GOOD_DRAFT, SHEET, OUTLINE_DOC, CITATIONS)
    assert c["all_pass"] is True
    assert {x["id"] for x in c["checks"]} == {"kw_count", "lsi", "word_count", "citations", "sections", "meta"}


def test_compliance_catches_violations():
    bad = "# T\n\nshort text without the phrase or the source."
    c = drafting.check_compliance(bad, SHEET, OUTLINE_DOC, CITATIONS)
    fails = {x["id"] for x in c["checks"] if not x["pass"]}
    assert {"kw_count", "lsi", "word_count", "citations", "sections"} <= fails
    assert c["all_pass"] is False


def test_build_draft_assembles_prompt_and_checks():
    seen = {}

    def llm(system, prompt):
        seen["prompt"] = prompt
        return GOOD_DRAFT

    d = drafting.build_draft(SHEET, OUTLINE_DOC, CITATIONS, llm=llm)
    assert d["markdown"] == GOOD_DRAFT
    assert d["compliance"]["all_pass"] is True
    assert d["edited"] is False
    # the generation prompt must carry guidelines, hard rules, outline and sources
    assert "Hard rules" in seen["prompt"] and "Clio 2025 Legal Trends Report" in seen["prompt"]
    assert "legal virtual assistant" in seen["prompt"] and "Costs" in seen["prompt"]


def test_build_draft_raises_when_llm_down():
    def llm(system, prompt):
        raise CredentialMissing("no key")
    with pytest.raises(CredentialMissing):
        drafting.build_draft(SHEET, OUTLINE_DOC, CITATIONS, llm=llm)


def test_to_docx_roundtrip():
    data = drafting.to_docx("# Title\n\n## Costs\n\n- point one\n\nBody with [link](https://x.com).")
    doc = Document(io.BytesIO(data))
    texts = [p.text for p in doc.paragraphs]
    assert "Title" in texts and "Costs" in texts
    assert any("point one" in t for t in texts)
    assert any("link (https://x.com)" in t for t in texts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_drafting.py" -v`
Expected: FAIL — module missing

- [ ] **Step 3: Write the implementation**

`guidelines.md` (v1 defaults, distilled for Legal Soft — editable file, swapped for team docs later; spec §2):

```markdown
# Legal Soft — Blog Writing Guidelines (v1 defaults)

## Who we write for
Law firm owners, operations managers, and legal admins in the United States deciding
whether (and how) to delegate work — intake, paralegal support, billing, reception —
to virtual staff.

## Voice
- Expert but plain-English. Explain like a consultant who has done this for 100 firms.
- Confident and specific. Numbers, timelines, and named tools beat adjectives.
- Zero filler. Never open with "In today's fast-paced world" or any generic AI phrase.
- Second person ("you", "your firm"). Active voice. Contractions are fine.

## Structure
- Answer the search query in the first two sentences — AI Overviews and featured
  snippets quote openings.
- A "Key takeaways" box (3-5 bullets) right after the intro.
- Short paragraphs: 3 sentences max. An H2 every 150-250 words. Sentence-case headings.
- Use a markdown table whenever comparing 2+ options, prices, or roles.
- End with an FAQ section (each answer 2-3 sentences) and one clear next step.

## E-E-A-T
- Every statistic cites its source by full name with a link — "according to the
  Clio 2025 Legal Trends Report", never "according to Clio" or "studies show".
- Include at least one first-hand operational insight (what firms get wrong, what a
  realistic ramp-up looks like) per major section where natural.
- No invented facts, no invented sources. If a claim has no verified source, cut it.

## Keywords
- Use the exact main keyword the number of times the brief specifies — spread
  naturally through intro, at least two H2 sections, and the conclusion. Never stuff.
- Weave in every LSI term from the brief where it reads naturally; skip any that
  would force the sentence.

## Brand
- Legal Soft is a US legal virtual-staffing company. Mention it only where relevant;
  one non-pushy CTA at the end ("book a consultation") is enough.
- Never disparage named competitors; compare on facts.
```

`drafting.py`:

```python
"""Team steps 11-12: guidelines + outline + hard rules → the draft, plus the
compliance checklist that proves the spec was followed. No LLM → no draft
(a fabricated draft would be worse than an honest failure)."""
from __future__ import annotations

import io
import re
from pathlib import Path

from seo_geo_agent import sources

GUIDELINES_PATH = Path(__file__).with_name("guidelines.md")


def _hard_rules(sheet: dict, outline_doc: dict) -> str:
    lsi = [l["term"] for l in sheet["lsi"]]
    return (
        f'- Use the exact phrase "{sheet["keyword"]}" between {sheet["usage"]["target_min"]} '
        f"and {sheet['usage']['target_max']} times.\n"
        f"- Work every one of these LSI terms in naturally: {lsi}.\n"
        f"- Target length: {outline_doc['targets']['word_count']} words (stay within 10%).\n"
        "- Cite every listed source inside its section, by its specific name, as a markdown link.\n"
        f"- H1: {outline_doc['meta']['title']}. Meta description: {outline_doc['meta']['description']}. "
        f"Slug: {outline_doc['meta']['slug']}."
    )


def build_draft(sheet: dict, outline_doc: dict, citations: dict, llm=None) -> dict:
    llm = llm or sources.llm_text
    guidelines = GUIDELINES_PATH.read_text(encoding="utf-8")
    outline_lines = "\n".join(
        f"{'#' * o['level']} {o['heading']}  — {o['note']}"
        + (f" [keywords: {', '.join(o['keywords'])}]" if o.get("keywords") else "")
        for o in outline_doc["outline"]
    )
    cited = "\n".join(
        f"- [{c['id']}] {c['source_name']} — {c['claim']} ({c['url']}) → section: {c['section']}"
        for c in citations["items"]
    ) or "- (no verified sources available — do not invent any)"
    markdown = llm(
        "You are Legal Soft's senior SEO content writer. Write the complete article in clean "
        "markdown. Output the article only — no preamble.",
        f"{guidelines}\n\n## Hard rules\n{_hard_rules(sheet, outline_doc)}\n\n"
        f"## Outline (follow exactly)\n{outline_lines}\n\n## Verified sources\n{cited}",
    )
    return {"markdown": markdown, "meta": outline_doc["meta"],
            "compliance": check_compliance(markdown, sheet, outline_doc, citations), "edited": False}


def check_compliance(markdown: str, sheet: dict, outline_doc: dict, citations: dict) -> dict:
    text = markdown.lower()
    words = len(re.findall(r"\w+", markdown))
    lo, hi = sheet["usage"]["target_min"], sheet["usage"]["target_max"]
    count = len(re.findall(re.escape(sheet["keyword"].lower()), text))
    target_wc = outline_doc["targets"]["word_count"]
    lsi_missing = [l["term"] for l in sheet["lsi"] if l["term"].lower() not in text]
    cite_missing = [c["id"] for c in citations["items"] if c["url"] not in markdown]
    heads_missing = [o["heading"] for o in outline_doc["outline"] if o["heading"].lower() not in text]
    meta = outline_doc["meta"]
    checks = [
        {"id": "kw_count", "label": f"Main keyword used {lo}-{hi}×", "pass": lo <= count <= hi,
         "detail": f"found {count}×"},
        {"id": "lsi", "label": f"All {len(sheet['lsi'])} LSI terms present", "pass": not lsi_missing,
         "detail": ("missing: " + ", ".join(lsi_missing[:5])) if lsi_missing else "all present"},
        {"id": "word_count", "label": f"~{target_wc} words (-10%/+25%)",
         "pass": 0.9 * target_wc <= words <= 1.25 * target_wc, "detail": f"{words} words"},
        {"id": "citations", "label": "Every verified citation linked", "pass": not cite_missing,
         "detail": ("missing: " + ", ".join(cite_missing)) if cite_missing else
                   f"{len(citations['items'])} linked"},
        {"id": "sections", "label": "Every outline section present", "pass": not heads_missing,
         "detail": ("missing: " + "; ".join(heads_missing[:4])) if heads_missing else "all present"},
        {"id": "meta", "label": "Meta title + description attached",
         "pass": bool(meta["title"] and meta["description"]), "detail": meta["title"]},
    ]
    return {"checks": checks, "all_pass": all(c["pass"] for c in checks)}


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def to_docx(markdown: str) -> bytes:
    from docx import Document

    doc = Document()
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        flat = _MD_LINK.sub(r"\1 (\2)", stripped)
        m = re.match(r"^(#{1,4})\s+(.*)", flat)
        if m:
            doc.add_heading(m.group(2), level=len(m.group(1)))
        elif flat.startswith(("- ", "* ")):
            doc.add_paragraph(flat[2:], style="List Bullet")
        else:
            doc.add_paragraph(flat)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest "agents/SEO Blog agent/seo_blog_agent/tests/test_drafting.py" -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add "agents/SEO Blog agent"
git commit -m "feat(blog): draft engine — guidelines-driven generation + compliance checker + docx"
```

---

### Task 8: Router part 2 — Stage 2/3 endpoints + export

**Files:**
- Modify: `app/routers/seo_blog.py` (extend with the endpoints below)
- Test: `app/routers/tests/test_seo_blog_router.py` (extend)

**Interfaces:**
- Consumes: `outline.competitor_profile`, `outline.build_outline`, `citations.source_citations`, `citations.revet`, `drafting.build_draft`, `drafting.check_compliance`, `drafting.to_docx`, `ahrefs_paste.parse_dr`, Task-4 helpers `_get_run`/`_save_run`.
- Produces HTTP API:
  - `POST /seo-blog/runs/{id}/build-outline` — 409 unless `gates.keywords`; builds profiles for `sheet.serp.top3` urls → `outline_doc` + `citations`; stage stays `"outline"`. 503 on `CredentialMissing` from the profile fetch.
  - `POST /seo-blog/runs/{id}/vet-citations` body `{dr_paste: str}` — parses DR, stores into `run.pasted.dr`, `citations.revet`.
  - `POST /seo-blog/runs/{id}/approve-outline` body `{outline: [items]}` — 409 unless `outline_doc` exists; stores writer-edited outline, `gates.outline=true`, `stage="draft"`.
  - `POST /seo-blog/runs/{id}/draft` — 409 unless both gates; `drafting.build_draft`; 503 on `CredentialMissing`.
  - `PATCH /seo-blog/runs/{id}/draft` body `{markdown: str}` — saves edit, `edited=true`, re-runs `check_compliance`.
  - `GET /seo-blog/runs/{id}/export?format=md|docx` — 404 without a draft; md → `text/markdown` attachment, docx → `application/vnd.openxmlformats-officedocument.wordprocessingml.document` attachment via `Response(content=..., media_type=..., headers={"Content-Disposition": f'attachment; filename="{slug}.{ext}"'})`.

- [ ] **Step 1: Write the failing tests** (append to `test_seo_blog_router.py`)

```python
OUTLINE_DOC = {"competitor_outlines": [], "meta": {"title": "T", "description": "D", "slug": "s"},
               "targets": {"word_count": 40, "links": 1},
               "outline": [{"heading": "Costs", "level": 2, "note": "", "keywords": []}],
               "evaluator": {"rounds": 1, "beats_all": True, "scores": {}, "note": ""}, "degraded": []}
CITS = {"items": [{"id": "c1", "claim": "x", "source_name": "Clio 2025 Legal Trends Report",
                   "url": "https://clio.com/report", "domain": "clio.com", "dr": None,
                   "dr_status": "unverified", "section": "Costs", "verified": True}],
        "short_by": 0, "rounds": 1, "degraded": []}
DRAFT_MD = ("# T\n\n## Costs\n\nlegal virtual assistant costs, and again legal virtual assistant, "
            "with virtual paralegal help per [Clio 2025 Legal Trends Report](https://clio.com/report). "
            + "word " * 20)


def _stage2_ready(monkeypatch):
    run = client.post("/api/seo-blog/runs", json={"keyword": "legal virtual assistant"}).json()
    client.post(f"/api/seo-blog/runs/{run['id']}/approve-keywords", json={"sheet": SHEET})
    monkeypatch.setattr(blog_router.outline, "competitor_profile", lambda url, **kw: {"url": url, "degraded": []})
    monkeypatch.setattr(blog_router.outline, "build_outline", lambda sheet, profiles, **kw: dict(OUTLINE_DOC))
    monkeypatch.setattr(blog_router.citations, "source_citations", lambda od, dr, **kw: dict(CITS))
    return run["id"]


def test_build_outline_requires_gate1(monkeypatch):
    run = client.post("/api/seo-blog/runs", json={"keyword": "x"}).json()
    assert client.post(f"/api/seo-blog/runs/{run['id']}/build-outline").status_code == 409


def test_build_outline_then_gate2(monkeypatch):
    rid = _stage2_ready(monkeypatch)
    body = client.post(f"/api/seo-blog/runs/{rid}/build-outline").json()
    assert body["outline_doc"]["meta"]["slug"] == "s"
    assert body["citations"]["items"][0]["dr_status"] == "unverified"
    edited = [{"heading": "Costs (edited)", "level": 2, "note": "", "keywords": []}]
    body = client.post(f"/api/seo-blog/runs/{rid}/approve-outline", json={"outline": edited}).json()
    assert body["gates"]["outline"] is True and body["stage"] == "draft"
    assert body["outline_doc"]["outline"][0]["heading"] == "Costs (edited)"


def test_vet_citations_applies_dr(monkeypatch):
    rid = _stage2_ready(monkeypatch)
    client.post(f"/api/seo-blog/runs/{rid}/build-outline")
    body = client.post(f"/api/seo-blog/runs/{rid}/vet-citations", json={"dr_paste": "clio.com 91"}).json()
    assert body["citations"]["items"][0]["dr"] == 91
    assert body["pasted"]["dr"] == {"clio.com": 91}


def test_draft_flow_and_export(monkeypatch):
    rid = _stage2_ready(monkeypatch)
    client.post(f"/api/seo-blog/runs/{rid}/build-outline")
    client.post(f"/api/seo-blog/runs/{rid}/approve-outline",
                json={"outline": OUTLINE_DOC["outline"]})
    monkeypatch.setattr(blog_router.drafting, "build_draft",
                        lambda s, o, c, **kw: {"markdown": DRAFT_MD, "meta": OUTLINE_DOC["meta"],
                                               "compliance": blog_router.drafting.check_compliance(
                                                   DRAFT_MD, SHEET, OUTLINE_DOC, CITS),
                                               "edited": False})
    body = client.post(f"/api/seo-blog/runs/{rid}/draft").json()
    assert body["draft"]["markdown"].startswith("# T")
    patched = client.patch(f"/api/seo-blog/runs/{rid}/draft",
                           json={"markdown": DRAFT_MD + " more"}).json()
    assert patched["draft"]["edited"] is True
    md = client.get(f"/api/seo-blog/runs/{rid}/export?format=md")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    docx = client.get(f"/api/seo-blog/runs/{rid}/export?format=docx")
    assert docx.status_code == 200 and len(docx.content) > 1000


def test_draft_requires_both_gates(monkeypatch):
    rid = _stage2_ready(monkeypatch)
    assert client.post(f"/api/seo-blog/runs/{rid}/draft").status_code == 409
    assert client.get(f"/api/seo-blog/runs/{rid}/export?format=md").status_code == 404
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `python -m pytest app/routers/tests/test_seo_blog_router.py -v`
Expected: Task-4 tests PASS, new tests FAIL (405/404 — routes missing)

- [ ] **Step 3: Write the implementation** (append to `app/routers/seo_blog.py`; extend imports)

```python
from fastapi import Response

from seo_blog_agent import citations, drafting, outline


class DrIn(BaseModel):
    dr_paste: str


class OutlineIn(BaseModel):
    outline: list[dict]


class DraftPatch(BaseModel):
    markdown: str


@router.post("/seo-blog/runs/{run_id}/build-outline")
def build_outline(run_id: str, user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not run["gates"]["keywords"]:
        raise HTTPException(409, "Approve the keyword sheet first (Gate 1)")
    try:
        profiles = [outline.competitor_profile(t["url"]) for t in run["sheet"]["serp"]["top3"]]
        run["outline_doc"] = outline.build_outline(run["sheet"], profiles)
        run["citations"] = citations.source_citations(run["outline_doc"], run["pasted"]["dr"])
    except CredentialMissing as exc:
        raise HTTPException(503, f"Outline data source unavailable: {exc}") from exc
    return _save_run(run)


@router.post("/seo-blog/runs/{run_id}/vet-citations")
def vet_citations(run_id: str, payload: DrIn, user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not run.get("citations"):
        raise HTTPException(409, "Build the outline first")
    run["pasted"]["dr"] = {**run["pasted"]["dr"], **ahrefs_paste.parse_dr(payload.dr_paste)}
    run["citations"] = citations.revet(run["citations"], run["pasted"]["dr"],
                                       run["outline_doc"]["targets"]["links"])
    return _save_run(run)


@router.post("/seo-blog/runs/{run_id}/approve-outline")
def approve_outline(run_id: str, payload: OutlineIn, user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not run.get("outline_doc"):
        raise HTTPException(409, "Build the outline first")
    run["outline_doc"]["outline"] = payload.outline
    run["gates"]["outline"] = True
    run["stage"] = "draft"
    return _save_run(run)


@router.post("/seo-blog/runs/{run_id}/draft")
def make_draft(run_id: str, user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not (run["gates"]["keywords"] and run["gates"]["outline"]):
        raise HTTPException(409, "Approve keywords and outline first")
    try:
        run["draft"] = drafting.build_draft(run["sheet"], run["outline_doc"], run["citations"])
    except CredentialMissing as exc:
        raise HTTPException(503, f"Draft generation unavailable: {exc}") from exc
    return _save_run(run)


@router.patch("/seo-blog/runs/{run_id}/draft")
def edit_draft(run_id: str, payload: DraftPatch, user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not run.get("draft"):
        raise HTTPException(409, "No draft to edit yet")
    run["draft"]["markdown"] = payload.markdown
    run["draft"]["edited"] = True
    run["draft"]["compliance"] = drafting.check_compliance(
        payload.markdown, run["sheet"], run["outline_doc"], run["citations"])
    return _save_run(run)


@router.get("/seo-blog/runs/{run_id}/export")
def export_draft(run_id: str, format: str = "md", user=Depends(get_current_user)):
    run = _get_run(run_id)
    if not run.get("draft"):
        raise HTTPException(404, "No draft yet")
    slug = run["outline_doc"]["meta"]["slug"] or run["id"]
    if format == "docx":
        return Response(
            content=drafting.to_docx(run["draft"]["markdown"]),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{slug}.docx"'})
    return Response(content=run["draft"]["markdown"], media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{slug}.md"'})
```

- [ ] **Step 4: Run the full router + engine suites**

Run: `python -m pytest app/routers/tests/test_seo_blog_router.py "agents/SEO Blog agent" -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/routers/seo_blog.py app/routers/tests/test_seo_blog_router.py
git commit -m "feat(blog): Stage-2/3 API — outline build, DR vetting, gates, draft, export"
```

---

### Task 9: Frontend API client + types

**Files:**
- Modify: `lib/api.ts` (append at the end, near the existing `seo*` functions ~line 1840+)

**Interfaces:**
- Consumes: existing private helpers in `api.ts`: `getJson<T>(path)`, `postJson<T>(path, body)`, `request(path, init)` — follow the exact style of `seoKeywordLab`/`seoDeleteBrand`.
- Produces (Task 10-11 consume these names verbatim): `BlogRunSummary`, `BlogGapRow`, `BlogSheet`, `BlogOutlineItem`, `BlogCitation`, `BlogComplianceCheck`, `BlogRun`, `blogRuns`, `blogRun`, `blogCreateRun`, `blogApproveKeywords`, `blogBuildOutline`, `blogVetCitations`, `blogApproveOutline`, `blogDraft`, `blogSaveDraft`, `blogExport`.

- [ ] **Step 1: Add types + functions**

```ts
// ---------------------------------------------------------------- seo blog (a9)

export interface BlogRunSummary { id: string; keyword: string; created: string; stage: "research" | "outline" | "draft"; }
export interface BlogGapRow { keyword: string; tag: "main" | "secondary" | "long_tail" | "aio"; volume: number | null; overlap: number; source: "ahrefs_pasted" | "serp_estimated"; }
export interface BlogSheet {
  keyword: string;
  metrics: { volume: number | null; kd: number | null; traffic_potential: number | null };
  serp: { top3: { url: string; title: string; position: number }[]; paa: string[]; related: string[]; aio_present: boolean };
  competitors: { url: string; intent: string; page_type: string; audience: string }[];
  mixed_intent: boolean;
  gap: BlogGapRow[];
  usage: { main_count_top1: number; target_min: number; target_max: number; frequent_terms: { term: string; count: number }[] };
  lsi: { term: string; fit_note: string }[];
  data_source: "ahrefs_pasted" | "serp_estimated";
  degraded: string[];
}
export interface BlogOutlineItem { heading: string; level: number; note: string; keywords: string[]; }
export interface BlogCitation { id: string; claim: string; source_name: string; url: string; domain: string; dr: number | null; dr_status: "ok" | "unverified"; section: string; verified: boolean; }
export interface BlogComplianceCheck { id: string; label: string; pass: boolean; detail: string; }
export interface BlogRun {
  id: string; keyword: string; created: string; stage: "research" | "outline" | "draft";
  gates: { keywords: boolean; outline: boolean };
  pasted: { metrics: Record<string, number | null>; competitor_keywords: Record<string, unknown[]>; dr: Record<string, number> };
  sheet: BlogSheet | null;
  outline_doc: {
    competitor_outlines: { url: string; title?: string; h2?: string[]; word_count?: number; external_links?: number;
      features?: { eeat: boolean; key_takeaways: boolean; tables: boolean; tools: boolean; lacks: string[] } }[];
    meta: { title: string; description: string; slug: string };
    targets: { word_count: number; links: number };
    outline: BlogOutlineItem[];
    evaluator: { rounds: number; beats_all: boolean | null; scores: Record<string, unknown>; note: string };
    degraded: string[];
  } | null;
  citations: { items: BlogCitation[]; short_by: number; rounds: number; degraded: string[] } | null;
  draft: { markdown: string; meta: { title: string; description: string; slug: string };
           compliance: { checks: BlogComplianceCheck[]; all_pass: boolean }; edited: boolean } | null;
}

export const blogRuns = () => getJson<{ runs: BlogRunSummary[] }>("/api/seo-blog/runs");
export const blogRun = (id: string) => getJson<BlogRun>(`/api/seo-blog/runs/${id}`);
export const blogCreateRun = (p: { keyword: string; metrics_paste?: string; competitor_keywords_paste?: Record<string, string> }) =>
  postJson<BlogRun>("/api/seo-blog/runs", p);
export const blogApproveKeywords = (id: string, sheet: BlogSheet) =>
  postJson<BlogRun>(`/api/seo-blog/runs/${id}/approve-keywords`, { sheet });
export const blogBuildOutline = (id: string) => postJson<BlogRun>(`/api/seo-blog/runs/${id}/build-outline`, {});
export const blogVetCitations = (id: string, drPaste: string) =>
  postJson<BlogRun>(`/api/seo-blog/runs/${id}/vet-citations`, { dr_paste: drPaste });
export const blogApproveOutline = (id: string, outlineItems: BlogOutlineItem[]) =>
  postJson<BlogRun>(`/api/seo-blog/runs/${id}/approve-outline`, { outline: outlineItems });
export const blogDraft = (id: string) => postJson<BlogRun>(`/api/seo-blog/runs/${id}/draft`, {});
export async function blogSaveDraft(id: string, markdown: string): Promise<BlogRun> {
  const response = await request(`/api/seo-blog/runs/${id}/draft`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ markdown }),
  });
  return response.json();
}
export async function blogExport(id: string, format: "md" | "docx"): Promise<Blob> {
  const response = await request(`/api/seo-blog/runs/${id}/export?format=${format}`, { method: "GET" });
  return response.blob();
}
```

Note: if `postJson`/`getJson`/`request` have slightly different names or signatures in `api.ts`, match whatever `seoKeywordLab` (line ~2017) and `seoDeleteBrand` (line ~1901) actually use — do not invent new helpers.

- [ ] **Step 2: Verify types compile**

Run: `npx tsc --noEmit`
Expected: clean

- [ ] **Step 3: Commit**

```bash
git add lib/api.ts
git commit -m "feat(blog): api client + types for the SEO Blog Writer"
```

---

### Task 10: Hub tile + nav wiring + stylesheet

**Files:**
- Modify: `lib/console-data.ts` (constants block ~lines 12-23; `agents` array ~line 57+)
- Modify: `components/console/ConsoleApp.tsx` (NAV_VIEWS array ~line 31; render block ~line 159)
- Modify: `app/globals.css` (after the `seo.css` import, line 7)
- Create: `app/blog.css`

**Interfaces:**
- Produces: `BLOG_AGENT_ID = "a9"`; nav key `"blog"`; `<BlogAgent>` mounted (component built in Task 11 — for this task, create the minimal placeholder below so tsc passes, Task 11 replaces it).

- [ ] **Step 1: Wire the tile**

`lib/console-data.ts` — add below the `SEO_AGENT_ID` constant:

```ts
/** SEO Blog Writer (backed by /api/seo-blog). */
export const BLOG_AGENT_ID = "a9";
```

Add to the `LIVE_AGENTS` record:

```ts
  [BLOG_AGENT_ID]: "blog",
```

Append to the `agents` array (after a8):

```ts
  { id: "a9", name: "SEO Blog Writer", role: "Ranking blog drafts", category: "copy", glyph: "pen-line", description: "Runs the team's 12-step SEO process: SERP research, outline engineering, vetted citations, publish-ready drafts." },
```

`components/console/ConsoleApp.tsx` — add `"blog",` to the `NAV_VIEWS` array (after `"seo",`); add import and render (mirror the `seo` lines exactly):

```tsx
import { BlogAgent } from "@/components/console/blog/BlogAgent";
// in the view switch, after the seo line:
          {nav === "blog" && <BlogAgent onToast={fire} onBack={() => setNav("agents")} />}
```

`app/globals.css` — after line 7 (`@import url("./seo.css");`):

```css
@import url("./blog.css");
```

`app/blog.css` (base additive namespace — Task 11 extends it):

```css
/* SEO Blog Writer (a9) — additive .blog-* namespace, token-driven. */
.blog-wrap { display: flex; flex-direction: column; gap: 16px; padding: 20px 24px; }
.blog-stagebar { display: flex; gap: 8px; align-items: center; }
.blog-pill { padding: 4px 12px; border-radius: 999px; font-size: 12px; font-weight: 600;
  background: var(--surface-2, #f1f3f6); color: var(--text-2, #556); }
.blog-pill.active { background: var(--accent, #1746a2); color: #fff; }
.blog-card { background: var(--surface-1, #fff); border: 1px solid var(--border-1, #e3e6ea);
  border-radius: 12px; padding: 16px; }
.blog-flag { font-size: 12px; color: var(--warn, #a05a00); background: var(--warn-bg, #fff7e8);
  border-radius: 8px; padding: 6px 10px; }
.blog-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
.blog-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.blog-table th, .blog-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border-1, #e3e6ea); }
.blog-draft { width: 100%; min-height: 420px; font: 13px/1.6 ui-monospace, monospace;
  border: 1px solid var(--border-1, #e3e6ea); border-radius: 10px; padding: 12px; }
.blog-actions { display: flex; gap: 8px; flex-wrap: wrap; }
```

Create `components/console/blog/BlogAgent.tsx` as a minimal placeholder (replaced in Task 11):

```tsx
"use client";

export function BlogAgent({ onToast, onBack }: { onToast: (msg: string) => void; onBack: () => void }) {
  return (
    <div className="blog-wrap">
      <button onClick={onBack}>Back</button>
      <div className="blog-card">SEO Blog Writer — coming online in Task 11.</div>
    </div>
  );
}
```

Note: before writing, check how `SeoAgent.tsx` receives `onToast` (exact prop type) and mirror it — if its signature differs (e.g. `onToast: (msg: string, kind?: string) => void`), use that.

- [ ] **Step 2: Verify**

Run: `npx tsc --noEmit`
Expected: clean

- [ ] **Step 3: Commit**

```bash
git add lib/console-data.ts components/console/ConsoleApp.tsx components/console/blog app/globals.css app/blog.css
git commit -m "feat(blog): SEO Blog Writer tile (a9) + nav wiring + stylesheet"
```

---

### Task 11: BlogAgent.tsx — the 3-stage studio view

**Files:**
- Modify: `components/console/blog/BlogAgent.tsx` (replace the placeholder)
- Modify: `app/blog.css` (extend as needed, keep the `.blog-*` namespace)

**Interfaces:**
- Consumes: every `blog*` function/type from Task 9; the `onToast`/`onBack` props as wired in Task 10.
- Produces: the full writer flow. Read `components/console/seo/SeoAgent.tsx` first and mirror its data-loading, error-toast, and layout idioms (buttons, cards, loading states) so the view feels native.

Required behavior (all states reachable from `run.stage` + gates):

1. **Runs list / kickoff** (no run selected): list from `blogRuns()` (keyword, created, stage badge; click → `blogRun(id)`). Kickoff form: keyword input (required), textarea "Ahrefs keyword metrics (optional paste)", repeatable URL+textarea pairs for "Competitor organic keywords CSV (optional)". Submit → `blogCreateRun` (busy indicator; 503 → toast the backend detail verbatim).
2. **Stage 1 — Keyword Target Sheet** (`stage==="research"`): metrics row (render volume/kd/tp only when non-null; when `data_source==="serp_estimated"` show `.blog-flag` "SERP-estimated — no Ahrefs data pasted"); `mixed_intent` → flag "Top 3 pages have mixed intent — pick your direction before approving"; SERP features chips (AI Overview present, PAA count); competitors table (url, intent, page_type, audience); gap table (keyword, tag chip, volume, source chip) with per-row remove ✕ and tag cycle on click; usage benchmark line ("use the main keyword {target_min}–{target_max}×; #1 uses it {main_count_top1}×"); LSI list with remove; `degraded` notes as flags. Button **Approve keywords →** sends the (edited) sheet via `blogApproveKeywords`.
3. **Stage 2 — Outline** (`stage==="outline"`): if `outline_doc` null, single button **Build outline & citations** → `blogBuildOutline` (long op — busy state). Then: targets line ("~{word_count} words · {links} external links — top-3 average +15%"); meta card (editable title/description/slug inputs bound to `outline_doc.meta`); competitor cards (h2 list, word_count, external_links, feature chips has/lacks); evaluator card (beats_all ✓ / honest note when false/null, rounds); outline editor — one row per item: level select (2/3/4), heading input, note input, remove ✕, add-section button; citations card — per item: source_name (link), section, DR badge (`ok` → green "DR {dr}", `unverified` → amber "DR unverified"), plus `short_by > 0` flag "{short_by} citation(s) short of target — verified only"; DR paste textarea + **Vet DR** button → `blogVetCitations`. Button **Approve outline →** → `blogApproveOutline` with the edited items.
4. **Stage 3 — Draft** (`stage==="draft"`): if `draft` null, button **Generate draft** → `blogDraft` (503 → verbatim toast). Then: left = `<textarea className="blog-draft">` bound to markdown (debounced save on blur via `blogSaveDraft`); right rail = compliance checklist (per check: ✓/✗ + label + detail; `all_pass` banner), meta card, `edited` chip. Actions: **Copy markdown** (`navigator.clipboard.writeText` → toast), **Download .md** / **Download .docx** (`blogExport` → object URL → `<a download>` click), **Back to runs**.

Implementation notes:
- Single component file is fine (~350 lines) with small local subcomponents (`GapTable`, `OutlineEditor`, `ComplianceRail`) in the same file — mirror how `SeoAgent.tsx` structures internal pieces.
- All fetches in `try/catch` → `onToast(String(err))`; busy flags per action (`useState<string | null>` of the in-flight action name) so double-clicks are inert.
- No new dependencies. No Tailwind (dead in this repo) — `.blog-*` classes only.

- [ ] **Step 1: Implement the component** (replace placeholder). Reference implementation below — adapt idioms (toast signature, button classes) to what `SeoAgent.tsx` actually uses, keep the structure and every behavior:

```tsx
"use client";

import { useCallback, useEffect, useState } from "react";

import {
  BlogOutlineItem, BlogRun, BlogRunSummary, BlogSheet,
  blogApproveKeywords, blogApproveOutline, blogBuildOutline, blogCreateRun,
  blogDraft, blogExport, blogRun, blogRuns, blogSaveDraft, blogVetCitations,
} from "@/lib/api";

const TAGS: BlogSheet["gap"][number]["tag"][] = ["main", "secondary", "long_tail", "aio"];

export function BlogAgent({ onToast, onBack }: { onToast: (msg: string) => void; onBack: () => void }) {
  const [runs, setRuns] = useState<BlogRunSummary[]>([]);
  const [run, setRun] = useState<BlogRun | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const [metricsPaste, setMetricsPaste] = useState("");
  const [ckUrl, setCkUrl] = useState("");
  const [ckCsv, setCkCsv] = useState("");
  const [drPaste, setDrPaste] = useState("");
  const [md, setMd] = useState("");

  const guard = useCallback(async (label: string, fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(label);
    try { await fn(); } catch (err) { onToast(String(err)); } finally { setBusy(null); }
  }, [busy, onToast]);

  useEffect(() => { blogRuns().then((r) => setRuns(r.runs)).catch((e) => onToast(String(e))); }, [onToast]);
  useEffect(() => { if (run?.draft) setMd(run.draft.markdown); }, [run?.id, run?.draft]);

  const open = (r: BlogRun) => { setRun(r); };

  if (!run) {
    return (
      <div className="blog-wrap">
        <div className="blog-stagebar">
          <button onClick={onBack}>← Agents</button><h2>SEO Blog Writer</h2>
        </div>
        <div className="blog-card">
          <h3>New blog run</h3>
          <input placeholder="Main target keyword (US)" value={keyword} onChange={(e) => setKeyword(e.target.value)} />
          <textarea placeholder="Ahrefs keyword metrics — optional paste (Volume / KD / Traffic potential)"
                    value={metricsPaste} onChange={(e) => setMetricsPaste(e.target.value)} />
          <input placeholder="Competitor URL (optional)" value={ckUrl} onChange={(e) => setCkUrl(e.target.value)} />
          <textarea placeholder="That competitor's Ahrefs organic-keywords CSV export"
                    value={ckCsv} onChange={(e) => setCkCsv(e.target.value)} />
          <button disabled={!keyword.trim() || busy !== null}
                  onClick={() => guard("kickoff", async () => {
                    const created = await blogCreateRun({
                      keyword: keyword.trim(), metrics_paste: metricsPaste,
                      competitor_keywords_paste: ckUrl.trim() && ckCsv.trim() ? { [ckUrl.trim()]: ckCsv } : {},
                    });
                    open(created);
                  })}>
            {busy === "kickoff" ? "Researching SERP…" : "Start research"}
          </button>
        </div>
        <div className="blog-card">
          <h3>Previous runs</h3>
          {runs.map((r) => (
            <button key={r.id} onClick={() => guard("open", async () => open(await blogRun(r.id)))}>
              {r.keyword} · {r.created} · <span className="blog-pill active">{r.stage}</span>
            </button>
          ))}
          {!runs.length && <p>No runs yet.</p>}
        </div>
      </div>
    );
  }

  const sheet = run.sheet;
  const od = run.outline_doc;
  const cits = run.citations;

  const setSheet = (next: BlogSheet) => setRun({ ...run, sheet: next });
  const setOutlineItems = (items: BlogOutlineItem[]) =>
    od && setRun({ ...run, outline_doc: { ...od, outline: items } });

  const download = (blob: Blob, name: string) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="blog-wrap">
      <div className="blog-stagebar">
        <button onClick={() => setRun(null)}>← Runs</button>
        {(["research", "outline", "draft"] as const).map((s) => (
          <span key={s} className={`blog-pill${run.stage === s ? " active" : ""}`}>{s}</span>
        ))}
        <strong>{run.keyword}</strong>
      </div>

      {run.stage === "research" && sheet && (
        <div className="blog-card">
          <h3>Keyword Target Sheet</h3>
          {sheet.data_source === "serp_estimated" &&
            <div className="blog-flag">SERP-estimated — no Ahrefs data pasted</div>}
          {sheet.mixed_intent &&
            <div className="blog-flag">Top 3 pages have mixed intent — pick your direction before approving</div>}
          {sheet.degraded.map((n) => <div key={n} className="blog-flag">{n}</div>)}
          <p>
            {sheet.metrics.volume != null && <>Volume {sheet.metrics.volume} · </>}
            {sheet.metrics.kd != null && <>KD {sheet.metrics.kd} · </>}
            {sheet.serp.aio_present ? "AI Overview on SERP" : "no AI Overview"} · PAA {sheet.serp.paa.length}
          </p>
          <p>Main keyword: #1 uses it {sheet.usage.main_count_top1}× — use it {sheet.usage.target_min}–{sheet.usage.target_max}×.</p>
          <table className="blog-table"><thead><tr><th>Competitor</th><th>Intent</th><th>Page type</th><th>Audience</th></tr></thead>
            <tbody>{sheet.competitors.map((c) => (
              <tr key={c.url}><td>{c.url}</td><td>{c.intent}</td><td>{c.page_type}</td><td>{c.audience}</td></tr>))}
            </tbody></table>
          <table className="blog-table"><thead><tr><th>Keyword</th><th>Tag</th><th>Vol</th><th>Source</th><th /></tr></thead>
            <tbody>{sheet.gap.map((g, i) => (
              <tr key={g.keyword}>
                <td>{g.keyword}</td>
                <td><button onClick={() => { const gap = [...sheet.gap];
                  gap[i] = { ...g, tag: TAGS[(TAGS.indexOf(g.tag) + 1) % TAGS.length] };
                  setSheet({ ...sheet, gap }); }}>{g.tag}</button></td>
                <td>{g.volume ?? "—"}</td><td>{g.source}</td>
                <td><button onClick={() => setSheet({ ...sheet, gap: sheet.gap.filter((_, j) => j !== i) })}>✕</button></td>
              </tr>))}
            </tbody></table>
          <p>LSI: {sheet.lsi.map((l, i) => (
            <span key={l.term} title={l.fit_note}> {l.term}
              <button onClick={() => setSheet({ ...sheet, lsi: sheet.lsi.filter((_, j) => j !== i) })}>✕</button>
            </span>))}
          </p>
          <div className="blog-actions">
            <button disabled={busy !== null}
                    onClick={() => guard("gate1", async () => setRun(await blogApproveKeywords(run.id, sheet)))}>
              Approve keywords →
            </button>
          </div>
        </div>
      )}

      {run.stage === "outline" && (
        <div className="blog-card">
          <h3>Outline & citations</h3>
          {!od && (
            <button disabled={busy !== null}
                    onClick={() => guard("outline", async () => setRun(await blogBuildOutline(run.id)))}>
              {busy === "outline" ? "Analyzing top 3 + sourcing citations…" : "Build outline & citations"}
            </button>
          )}
          {od && (
            <>
              {od.degraded.map((n) => <div key={n} className="blog-flag">{n}</div>)}
              <p>~{od.targets.word_count} words · {od.targets.links} external links (top-3 average +15%)</p>
              <p>{od.evaluator.beats_all === true ? "✓ outline beats all 3 competitor outlines"
                  : od.evaluator.note || "evaluator pending"}</p>
              <div className="blog-grid">{od.competitor_outlines.map((p) => (
                <div key={p.url} className="blog-card">
                  <strong>{p.url}</strong>
                  <p>{p.word_count ?? "?"} words · {p.external_links ?? "?"} ext links</p>
                  {p.features && <p>lacks: {p.features.lacks.join("; ") || "—"}</p>}
                </div>))}
              </div>
              <input value={od.meta.title}
                     onChange={(e) => setRun({ ...run, outline_doc: { ...od, meta: { ...od.meta, title: e.target.value } } })} />
              <input value={od.meta.description}
                     onChange={(e) => setRun({ ...run, outline_doc: { ...od, meta: { ...od.meta, description: e.target.value } } })} />
              <input value={od.meta.slug}
                     onChange={(e) => setRun({ ...run, outline_doc: { ...od, meta: { ...od.meta, slug: e.target.value } } })} />
              {od.outline.map((o, i) => (
                <div key={i} className="blog-actions">
                  <select value={o.level} onChange={(e) => { const items = [...od.outline];
                    items[i] = { ...o, level: Number(e.target.value) }; setOutlineItems(items); }}>
                    <option value={2}>H2</option><option value={3}>H3</option><option value={4}>H4</option>
                  </select>
                  <input value={o.heading} onChange={(e) => { const items = [...od.outline];
                    items[i] = { ...o, heading: e.target.value }; setOutlineItems(items); }} />
                  <input value={o.note} onChange={(e) => { const items = [...od.outline];
                    items[i] = { ...o, note: e.target.value }; setOutlineItems(items); }} />
                  <button onClick={() => setOutlineItems(od.outline.filter((_, j) => j !== i))}>✕</button>
                </div>))}
              <button onClick={() => setOutlineItems([...od.outline,
                { heading: "New section", level: 2, note: "", keywords: [] }])}>+ section</button>
              {cits && (
                <div className="blog-card">
                  <h4>Verified citations</h4>
                  {cits.short_by > 0 &&
                    <div className="blog-flag">{cits.short_by} citation(s) short of target — verified only, nothing invented</div>}
                  {cits.items.map((c) => (
                    <p key={c.id}>
                      <a href={c.url} target="_blank" rel="noreferrer">{c.source_name}</a> → {c.section} ·{" "}
                      <span className={c.dr_status === "ok" ? "blog-pill active" : "blog-flag"}>
                        {c.dr_status === "ok" ? `DR ${c.dr}` : "DR unverified"}
                      </span>
                    </p>))}
                  <textarea placeholder="Paste Ahrefs DR per domain (e.g. clio.com 91) to enforce 70+"
                            value={drPaste} onChange={(e) => setDrPaste(e.target.value)} />
                  <button disabled={busy !== null}
                          onClick={() => guard("dr", async () => setRun(await blogVetCitations(run.id, drPaste)))}>
                    Vet DR
                  </button>
                </div>
              )}
              <div className="blog-actions">
                <button disabled={busy !== null}
                        onClick={() => guard("gate2", async () => setRun(await blogApproveOutline(run.id, od.outline)))}>
                  Approve outline →
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {run.stage === "draft" && (
        <div className="blog-card">
          <h3>Draft</h3>
          {!run.draft && (
            <button disabled={busy !== null}
                    onClick={() => guard("draft", async () => setRun(await blogDraft(run.id)))}>
              {busy === "draft" ? "Writing…" : "Generate draft"}
            </button>
          )}
          {run.draft && (
            <div className="blog-grid">
              <textarea className="blog-draft" value={md} onChange={(e) => setMd(e.target.value)}
                        onBlur={() => { if (md !== run.draft?.markdown)
                          guard("save", async () => setRun(await blogSaveDraft(run.id, md))); }} />
              <div>
                {run.draft.compliance.checks.map((c) => (
                  <p key={c.id}>{c.pass ? "✓" : "✗"} {c.label} — {c.detail}</p>))}
                <p>{run.draft.compliance.all_pass ? "All checks pass" : "Checks failing — revise or edit"}</p>
                {run.draft.edited && <span className="blog-pill">edited</span>}
                <div className="blog-actions">
                  <button onClick={() => { navigator.clipboard.writeText(md); onToast("Markdown copied"); }}>Copy markdown</button>
                  <button onClick={() => guard("md", async () =>
                    download(await blogExport(run.id, "md"), `${run.draft?.meta.slug || run.id}.md`))}>Download .md</button>
                  <button onClick={() => guard("docx", async () =>
                    download(await blogExport(run.id, "docx"), `${run.draft?.meta.slug || run.id}.docx`))}>Download .docx</button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Verify**

Run: `npx tsc --noEmit`
Expected: clean

- [ ] **Step 3: Manual smoke (offline backend)**

Start backend (`start-backend.ps1` from repo root `c:\Users\ACER\Desktop\ghi`) and frontend on port 3000 (`start-frontend.ps1`; free the port first if a stale process holds it). Sign in, open the SEO Blog Writer tile, and verify: kickoff without Serper key shows the honest 503 toast (research refuses to start, per spec §8) and the runs list renders.

- [ ] **Step 4: Commit**

```bash
git add components/console/blog/BlogAgent.tsx app/blog.css
git commit -m "feat(blog): 3-stage Blog Studio view — sheet, outline+citations, draft+compliance"
```

---

### Task 12: Full-suite verification

**Files:** none new.

- [ ] **Step 1: Backend full suite**

Run from `backend`: `python -m pytest "agents/SEO Blog agent" app/routers/tests/test_seo_blog_router.py -v` then the whole suite `python -m pytest`
Expected: all green; pre-existing failures (if any) must match a run of `git stash && python -m pytest` baseline — do not fix unrelated tests, just record them.

- [ ] **Step 2: Frontend**

Run from `newfrontend`: `npx tsc --noEmit`
Expected: clean. (Do not run lint — broken by design.)

- [ ] **Step 3: Spec compliance walkthrough**

Re-read `docs/superpowers/specs/2026-07-30-seo-blog-agent-design.md` §4's 12-step table and confirm each step has a live code path. Confirm the three §2 non-negotiables: provenance labels, no unverified citations, gates order.

- [ ] **Step 4: Final commit (if anything changed)**

```bash
git add -A
git commit -m "test(blog): full-suite verification pass"
```

---

## Self-review notes (done at plan time)

- Spec §4 rows 1-12 → Tasks 3 (steps 1-4), 5 (5-8), 6 (9-10), 7 (11-12); gates → Tasks 4 & 8; provenance → Tasks 2/3/6 tests assert labels; escalation paths (503/409/flags) → Tasks 4/8.
- Out of scope guarded: no Google Doc export, no guidelines panel, no DataForSEO, US-only (no geo param anywhere).
- Type consistency: `outline_doc`/`citations`/`draft`/`sheet` field names identical across engine, router tests, and TS types (checked by name).
- Deliberate deviation from spec §7 file list: no separate `state.py`-consuming "runs" module — run-lifecycle helpers live in the router (a2's proven pattern); `guidelines.md` ships inside the engine package as spec'd.

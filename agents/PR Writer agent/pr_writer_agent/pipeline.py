"""The five visible steps of a PR run, and the draft edits, over the AI core.

``steps.py`` (senior-ai-engineer) owns the prompts and every model call; this
module sequences those calls into the steps the HTTP contract exposes
(``docs/pr-writer-api.md`` §3) and owns the deterministic glue around them:

    intake + screen   ``run_intake`` then ``escalation.screen``   (≤2 calls)
    research          search + fetch, ``run_research_extract``, ``run_angle``
    draft             ``run_draft``, then ``run_qa_fix`` only if a finding is fixable
    package           [fact_check ∥ seo ∥ distribution ∥ snippets], then reviewer notes

Every model call goes through :func:`deadline_invoke`, which clips each
call's timeout to what is left of the step's server deadline, so a step either
finishes inside the 300 s relay or fails as ``model_timeout`` — never cut off
by the relay with nothing saved.

No persistence and no HTTP: the service (``app/services/pr_writer.py``) loads
state, calls these, and checkpoints what comes back.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from seo_geo_agent.sources import CredentialMissing, SearchUnavailable

from pr_writer_agent import content_types as cts
from pr_writer_agent import escalation
from pr_writer_agent import evidence as ev
from pr_writer_agent import llm as pr_llm
from pr_writer_agent import qa, render, steps
from pr_writer_agent import research as pr_research
from pr_writer_agent.prompts import ATTEMPTS_PER_CALL

#: Server deadline per step request. The relay cuts at 300 s.
STEP_DEADLINE_S = 270
#: A lease outlives the deadline, so it never lapses under a live request.
LEASE_S = STEP_DEADLINE_S + 20
_MIN_CALL_S = 5.0

STEP_IDS = ("intake", "screen", "research", "draft", "package", "notes")
SECTIONS = ("seo", "distribution", "snippets", "reviewer_notes")
CLARIFY_KEYS = ("why_now", "audience", "key_facts", "spokespeople", "release_timing", "partner_details")
CLARIFY_LABELS = {
    "why_now": "Why now",
    "audience": "Audience",
    "key_facts": "Key facts and where each comes from",
    "spokespeople": "Spokesperson (name, exact title)",
    "release_timing": "Release date or embargo",
    "partner_details": "Partner (name, boilerplate, spokesperson)",
}
CATEGORY_LABELS = {
    "litigation": "litigation", "regulatory": "regulatory action", "data_incident": "a data incident",
    "layoffs": "layoffs", "m_and_a": "a merger or acquisition", "crisis": "a crisis",
    "": "a sensitive topic",
}
DEFAULT_CONTENT_TYPE = "press_release"


# --------------------------------------------------------------------------- #
# Deadline and failure taxonomy
# --------------------------------------------------------------------------- #

class StepTimeout(RuntimeError):
    """The step's server deadline ran out before the work finished."""


class Deadline:
    def __init__(self, seconds: float = STEP_DEADLINE_S, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        return self._end - self._clock()

    def check(self, what: str) -> None:
        if self.remaining() < _MIN_CALL_S:
            raise StepTimeout(f"no time left for {what}")


class Meter:
    """Model spend for one request: calls, tokens and models, summed from
    every reply (repair calls and concurrent package calls included). Cost is
    ``None``: ``llm.Reply`` does not carry the provider's cost, and a guessed
    figure would be a fake one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.models: set[str] = set()

    def add(self, reply: Any) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += int(getattr(reply, "input_tokens", 0) or 0)
            self.output_tokens += int(getattr(reply, "output_tokens", 0) or 0)
            if getattr(reply, "model", ""):
                self.models.add(str(reply.model))

    def as_dict(self) -> dict:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": None, "models": sorted(self.models)}


def deadline_invoke(deadline: Deadline, base: Callable[..., Any] | None = None,
                    meter: Meter | None = None) -> Callable[..., Any]:
    """An ``llm.Invoke`` whose per-call timeout is clipped so that all of the
    provider client's attempts fit in what is left of ``deadline``; every
    reply is counted on ``meter``."""

    def invoke(system: str, user: str, *, slot: str, timeout_s: float, max_tokens: int,
               temperature: float):
        per_attempt = min(float(timeout_s), (deadline.remaining() - 2.0) / ATTEMPTS_PER_CALL)
        if per_attempt < _MIN_CALL_S:
            raise StepTimeout("the step's deadline ran out before the next model call")
        call = base or pr_llm.default_invoke
        reply = call(system, user, slot=slot, timeout_s=per_attempt, max_tokens=max_tokens,
                     temperature=temperature)
        if meter is not None:
            meter.add(reply)
        return reply

    return invoke


@dataclass
class Failure:
    status: int
    code: str
    message: str


MSG_UNAVAILABLE = "The writing model didn't respond after 3 tries. Your brief and research are saved."
MSG_TIMEOUT = ("This step ran out of time before the writing model finished. Your brief and research are saved; "
               "try the step again.")
MSG_TRUNCATED = "The writing model's answer was cut off, so nothing from it was used. Try the step again."
MSG_REFUSED = "The writing model declined this request, so nothing was written. Check the brief and try again."
MSG_INVALID = "The writing model's answer wasn't usable after a repair attempt, so nothing from it was used. Try again."
MSG_SEARCH = ("Research couldn't run: the web search service refused the request (out of credit, rate-limited "
              "or down). Nothing was drafted. An admin needs to check it.")
MSG_SEARCH_KEY = "Research couldn't run: web search isn't configured on this server. An admin needs to add the key."
MSG_MODEL_KEY = "The writing model isn't configured on this server. An admin needs to add the key in Settings → Secrets."
MSG_SCREEN = ("The check for topics that need legal first couldn't run, so nothing will be drafted yet. "
              "Your brief is saved; try again.")


def _chain(exc: BaseException) -> list[BaseException]:
    out, seen = [], set()
    while exc is not None and id(exc) not in seen:
        out.append(exc)
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return out


def classify(exc: BaseException, *, step: str = "") -> Failure | None:
    """What the client is told about ``exc`` (contract §0.2). ``None`` for
    anything that is not an expected external failure — a bug, which the
    caller re-raises rather than dressing up as a model failure.

    StepTimeout (this step's own deadline)       -> 504 model_timeout
    EscalationCheckFailed (never "safe")         -> 502 screen_failed
    SearchUnavailable                            -> 424 search_unavailable
    missing key (model or search)                -> 424 credential_missing
    LLMUnavailable (provider error / no answer)  -> 424 model_unavailable
    LLMTruncated / LLMRefused / LLMOutputInvalid -> 502 model_truncated / model_refused / model_output_invalid
    """
    chain = _chain(exc)
    text = " ".join(str(e) for e in chain)
    if any(isinstance(e, StepTimeout) for e in chain):
        return Failure(504, "model_timeout", MSG_TIMEOUT)
    if isinstance(exc, escalation.EscalationCheckFailed):
        return Failure(502, "screen_failed", MSG_SCREEN)
    if any(isinstance(e, SearchUnavailable) for e in chain):
        return Failure(424, "search_unavailable", MSG_SEARCH)
    if "Missing required configuration" in text:
        return Failure(424, "credential_missing", MSG_MODEL_KEY)
    if any(isinstance(e, pr_llm.LLMUnavailable) for e in chain):
        return Failure(424, "model_unavailable", MSG_UNAVAILABLE)
    if any(isinstance(e, CredentialMissing) for e in chain):
        return Failure(424, "credential_missing", MSG_SEARCH_KEY if step == "research" else MSG_MODEL_KEY)
    if any(isinstance(e, pr_llm.LLMTruncated) for e in chain):
        return Failure(502, "model_truncated", MSG_TRUNCATED)
    if any(isinstance(e, pr_llm.LLMRefused) for e in chain):
        return Failure(502, "model_refused", MSG_REFUSED)
    if any(isinstance(e, pr_llm.LLMOutputInvalid) for e in chain):
        return Failure(502, "model_output_invalid", MSG_INVALID)
    return None


# --------------------------------------------------------------------------- #
# Intake (brief <-> the store's ``brief``) and clarify
# --------------------------------------------------------------------------- #

def intake_to_brief(intake: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: {"value": (v or {}).get("value"), "source": (v or {}).get("provenance", "missing")}
            for k, v in intake.items()}


def brief_to_intake(brief: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: {"value": (v or {}).get("value"), "provenance": (v or {}).get("source", "missing"), "excerpt": ""}
            for k, v in (brief or {}).items()}


def _as_stated(field: dict) -> dict:
    field = dict(field)
    if field.get("provenance") == "extracted":
        field["provenance"] = "stated"
    if isinstance(field.get("value"), list):
        field["value"] = [dict(i, provenance="stated") if isinstance(i, dict) and i.get("provenance") == "extracted"
                          else i for i in field["value"]]
    return field


def run_intake(news: str, facts: str, brand: dict, *, known_brands: list[str], invoke) -> dict:
    """Intake over the one-sentence news and the requester's optional facts box.

    The facts box is the requester's own words, so what intake extracts from
    it is STATED (requester provenance, ``br-N`` evidence, its URLs allowed).
    It still reaches the prompt only fenced (passed as ``pasted``, which
    ``steps.run_intake`` wraps with ``evidence.fence``); provenance is promoted
    after the excerpt check has confirmed each value really is in the box.
    """
    result = steps.run_intake(news, stated={"brand": brand.get("name") or brand.get("id"), "news": news},
                              pasted=facts, known_brands=known_brands, invoke=invoke)
    result["intake"] = {k: _as_stated(v) if isinstance(v, dict) else v for k, v in result["intake"].items()}
    return result


def _excerpt(intake: dict, facts: str, decision: dict) -> str:
    pool = [str((intake.get("news") or {}).get("value") or "")] + [s for s in re.split(r"(?<=[.!?])\s+", facts or "") if s]
    terms = [t for _, t in (decision.get("prefilter") or {}).get("strong") or []]
    terms += [t for _, t in (decision.get("prefilter") or {}).get("weak") or []]
    for term in terms:
        for sentence in pool:
            if term and term.lower() in sentence.lower():
                return sentence.strip()[:600]
    return pool[0].strip()[:600]


def run_screen(intake: dict, facts: str, brand: dict, card_agent: dict, *, invoke) -> dict:
    """The escalation decision as a dict, plus ``category`` and ``excerpt``
    for the escalation card. Raises when the screen cannot run."""
    names = [n for n in (brand.get("name"), card_agent.get("short_name"), card_agent.get("legal_name")) if n]
    decision = escalation.screen(intake, pasted=facts, brand_names=names, invoke=invoke).to_dict()
    category = (decision.get("categories") or [""])[0]
    decision["category"] = category
    decision["category_label"] = CATEGORY_LABELS.get(category, category.replace("_", " "))
    decision["excerpt"] = _excerpt(intake, facts, decision) if decision.get("escalate") else ""
    return decision


def _guess(field: dict | None) -> str | None:
    value = (field or {}).get("value")
    if value in (None, "", []):
        return None
    if isinstance(value, list):
        return "; ".join(str(v.get("fact") or v.get("name") or "") for v in value if isinstance(v, dict)) or None
    if isinstance(value, dict):
        return "; ".join(str(v) for v in value.values() if v) or None
    return str(value)


def clarify_card(intake_result: dict) -> dict | None:
    """The one-time clarify card: only the required fields still missing,
    and the content type as a pre-filled select. ``None`` when nothing is."""
    intake = intake_result["intake"]
    missing = [k for k in intake_result.get("missing", []) if k in CLARIFY_KEYS]
    ct_value = (intake.get("content_type") or {}).get("value")
    if not missing and ct_value:
        return None
    return {
        "state": "open",
        "fields": [{"key": k, "label": CLARIFY_LABELS[k], "guess": _guess(intake.get(k)), "answer": None}
                   for k in missing],
        "content_type": ct_value or DEFAULT_CONTENT_TYPE,
        "content_type_guessed": not ct_value,
        "answered_at": None,
    }


_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def answer_value(key: str, text: str) -> Any:
    """A clarify answer in the shape the intake field holds."""
    text = text.strip()
    if key == "key_facts":
        return [{"fact": line.strip(" -•"), "source": "requester"} for line in text.splitlines() if line.strip(" -•")]
    if key == "spokespeople":
        out = []
        for line in text.splitlines():
            name, _, title = line.strip(" -•").partition(",")
            if name.strip():
                out.append({"name": name.strip(), "title": title.strip(), "organization": "", "talking_points": ""})
        return out
    if key == "release_timing":
        m = _ISO_DATE.search(text)
        return {"release_date": m.group(1) if m else None,
                "embargo": text if "embargo" in text.lower() else None}
    if key == "partner_details":
        return {"name": text, "description": "", "boilerplate": "", "spokesperson": "", "approval_contact": ""}
    return text


def apply_clarify(intake: dict, clarify: dict | None, content_type: str | None) -> dict:
    """The intake with the requester's clarify answers as STATED values (hard
    requirements) and the chosen content type."""
    out = {k: dict(v) for k, v in intake.items()}
    for f in (clarify or {}).get("fields") or []:
        if f.get("answer"):
            value = answer_value(f["key"], f["answer"])
            if isinstance(value, list):
                value = [dict(v, provenance="stated", excerpt="") for v in value]
            out[f["key"]] = {"value": value, "provenance": "stated", "excerpt": ""}
    if content_type:
        out["content_type"] = {"value": content_type, "provenance": "stated", "excerpt": ""}
    return out


def content_type_of(intake: dict) -> str:
    value = (intake.get("content_type") or {}).get("value")
    return value if value in cts.CONTENT_TYPES else DEFAULT_CONTENT_TYPE


def with_content_type(intake: dict) -> dict:
    out = dict(intake)
    out["content_type"] = {**(intake.get("content_type") or {}), "value": content_type_of(intake)}
    return out


# --------------------------------------------------------------------------- #
# Research, angle, draft
# --------------------------------------------------------------------------- #

def gather(news: str, brand: dict, *, search=None, fetch=None) -> dict:
    return pr_research.gather(news, brand, search=search, fetch=fetch)


def extract(pages: list[dict], intake: dict, card_agent: dict, *, invoke) -> dict:
    return steps.run_research_extract(pages, intake, card_agent, invoke=invoke)


def evidence_for(intake: dict, card_agent: dict, web: list[dict]) -> list[dict]:
    return steps.build_evidence(intake, card_agent, web)


def research_result(gathered: dict, extracted: dict, evidence: list[dict]) -> dict:
    return {
        "sources_read": len(gathered["pages"]),
        "facts_found": len(evidence),
        "partial": bool(gathered["partial"]),
        "sources": [{"url": p["url"], "title": p["title"][:200], "publisher": pr_research._domain(p["url"]),
                     "date": None, "kind": "web"} for p in gathered["pages"]],
        "notes": list(extracted.get("notes", []))[:20],
    }


def run_angle(intake: dict, card_agent: dict, evidence: list[dict], *, invoke) -> dict:
    return steps.run_angle(intake, card_agent, evidence, invoke=invoke)["angle"]


def run_draft(intake: dict, card_agent: dict, evidence: list[dict], angle: dict, *, today: date,
              invoke) -> dict:
    speakers, notes = ev.speakers(intake, card_agent)
    draft = steps.run_draft(intake, card_agent, evidence, angle, speakers, today=today, invoke=invoke)
    draft.setdefault("notes", []).extend(notes)
    draft.pop("usage", None)
    return draft


def needs_qa_fix(draft: dict) -> bool:
    return any(f["check"] in qa.FIXABLE for f in (draft.get("qa") or {}).get("findings", []))


def run_qa_fix(draft: dict, intake: dict, card_agent: dict, evidence: list[dict], angle: dict, *,
               today: date, invoke) -> dict:
    allowed = ev.allowed_urls(ev.normalize_card(card_agent), intake, evidence)
    fixed = steps.run_qa_fix(draft, card_agent, evidence, primary_keyword=angle.get("primary_keyword"),
                             allowed=allowed, today=today, invoke=invoke)
    fixed.pop("usage", None)
    return fixed


def draft_result(draft: dict) -> dict:
    counts = (draft.get("qa") or {}).get("counts") or {}
    return {"words": int(counts.get("body_words") or 0),
            "quotes_pending": sum(1 for b in draft.get("blocks", []) if b.get("kind") == "quote")}


def refresh(draft: dict, intake: dict, card_agent: dict, evidence: list[dict], angle: dict, *,
            today: date) -> dict:
    """Re-run the deterministic QA and re-render the markdown after a code edit."""
    ct = cts.get(draft["content_type"])
    allowed = ev.allowed_urls(ev.normalize_card(card_agent), intake, evidence)
    return steps.attach_qa(draft, ct, card_agent, evidence, primary_keyword=angle.get("primary_keyword"),
                           allowed=allowed, today=today)


# --------------------------------------------------------------------------- #
# Package: one concurrent round, then reviewer notes
# --------------------------------------------------------------------------- #

def run_package(*, draft: dict, intake: dict, angle: dict, card_agent: dict, evidence: list[dict],
                today: date, invoke, need_fact_check: bool, done_sections: set[str]) -> dict:
    """One concurrent round: fact-check, SEO, distribution and snippets. The
    worst case per ``prompts.STEPS`` is the slowest of them (fact-check,
    270 s), inside the step deadline. Runs only what is still missing, so a
    retry pays only for what failed. Returns ``{"fact_rows", "sections",
    "failures"}``; unexpected errors (bugs) re-raise."""
    jobs: dict[str, Callable[[], Any]] = {}
    if need_fact_check:
        jobs["fact_check"] = lambda: steps.run_fact_check(draft, evidence, today=today, invoke=invoke)
    if "seo" not in done_sections:
        jobs["seo"] = lambda: steps.run_seo(draft, angle, card_agent, evidence, invoke=invoke)
    if "distribution" not in done_sections:
        jobs["distribution"] = lambda: steps.run_distribution(intake, angle, draft, invoke=invoke)
    if "snippets" not in done_sections:
        jobs["snippets"] = lambda: steps.run_snippets(draft, invoke=invoke)

    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {name: pool.submit(fn) for name, fn in jobs.items()}
            for name, fut in futures.items():
                try:
                    results[name] = fut.result()
                except Exception as exc:  # noqa: BLE001 — classified below
                    errors[name] = exc

    sections: dict[str, dict] = {}
    for name in ("seo", "distribution", "snippets"):
        if name in results:
            sections[name] = {"state": "done", "error": None, "body": results[name][name]}
    failures: dict[str, Failure] = {}
    for name, exc in errors.items():
        failure = classify(exc)
        if failure is None:
            raise exc
        failures[name] = failure
        if name in SECTIONS:
            sections[name] = {"state": "failed", "error": failure.message, "body": None}
    rows = results["fact_check"]["rows"] if "fact_check" in results else None
    return {"fact_rows": rows, "sections": sections, "failures": failures}


def run_notes(*, intake_result: dict, screen: dict, draft: dict, fact_rows: list[dict], invoke) -> dict:
    """Reviewer notes: one call, after the fact-check exists (its open rows
    are an input). Its own step, so no request makes more than two
    sequential model calls."""
    return steps.run_reviewer_notes(intake_result, screen or {}, draft, fact_rows, invoke=invoke)["notes"]


# --------------------------------------------------------------------------- #
# Quotes, claims, markers, blocks — the wire view of a stored version
# --------------------------------------------------------------------------- #

def quote_text(block: dict) -> str:
    return ev.strip_cites(block.get("text") or "").strip().strip('"“”').strip()


def store_quotes(draft: dict) -> list[dict]:
    """The quotes ``create_pr_version`` tracks: id = the quote block's id,
    approval reset by a hash of its text (the store's rule)."""
    speakers = {s["id"]: s for s in draft.get("speakers", [])}
    out = []
    for b in draft.get("blocks", []):
        if b.get("kind") != "quote":
            continue
        sp = speakers.get(b.get("speaker_id") or "") or {}
        out.append({"quote_id": b["id"], "speaker": sp.get("name") or "[VERIFY: speaker]",
                    "title": sp.get("title") or "[VERIFY: title]",
                    "organization": sp.get("organization") or "", "text": quote_text(b) or "(empty quote)"})
    return out


def _claim_hash(row: dict, occurrence: int) -> str:
    key = f"{row.get('block_id') or ''}|{' '.join(str(row.get('claim') or '').split())}|{occurrence}"
    return "c-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def assign_ids(rows: list[dict]) -> list[dict]:
    """Give every fact-check row a unique, stable ``claim_id``.

    The id is a hash of the row's block, its text and its OCCURRENCE among
    rows with the same block and text — two identical "[VERIFY: exact title]"
    placeholders are two rows with two ids, not one id twice. A row that
    already carries an id (stored, or carried from the previous version)
    keeps it, which is what keeps verification and links stable across
    versions; only a missing or clashing id is (re)derived. Deterministic, so
    a version stored before ids were stamped reads the same every time.
    Returns new row dicts; ``rows`` is not modified."""
    out: list[dict] = []
    seen: set[str] = set()
    counts: dict[str, int] = {}
    for row in rows:
        row = dict(row)
        cid = row.get("claim_id")
        if not cid or cid in seen:
            base = f"{row.get('block_id') or ''}|{' '.join(str(row.get('claim') or '').split())}"
            k = counts.get(base, 0)
            cid = _claim_hash(row, k)
            while cid in seen:
                k += 1
                cid = _claim_hash(row, k)
            counts[base] = k + 1
            row["claim_id"] = cid
        seen.add(cid)
        out.append(row)
    return out


def claim_id(row: dict) -> str:
    """A row's id. Rows reach the wire only through :func:`assign_ids`."""
    return str(row.get("claim_id") or _claim_hash(row, 0))


def is_placeholder(row: dict) -> bool:
    return bool(qa.VERIFY.fullmatch(str(row.get("claim") or "").strip()))


def _norm(text: str) -> str:
    return " ".join(str(text or "").replace("’", "'").replace("“", '"').replace("”", '"').split()).lower()


def carry_fact_rows(prev_rows: list[dict], draft: dict, evidence: list[dict], *, revised_block: str | None,
                    today: date) -> list[dict]:
    """The fact-check table for a new version without a model call.

    Rows whose claim is still in the text carry over (with any human
    verification). In ``revised_block``, a claim that left the text becomes
    ``removed``, and every sentence there that states a fact (cites evidence
    or carries a number) and is not covered by a kept row gets a new row —
    ``Verified`` only when every cite resolves, otherwise ``Needs
    confirmation``. Placeholder rows follow the ``[VERIFY]`` tokens left in
    the rendered draft, so a filled placeholder leaves the table."""
    ct = cts.get(draft["content_type"])
    markdown = render.render_markdown(draft, ct)
    texts = {b["id"]: _norm(qa.scan_text(b.get("text", ""))) for b in draft.get("blocks", [])}
    texts["headline"] = _norm(draft.get("headline"))
    texts["subhead"] = _norm(draft.get("subhead"))
    known = ev.index(evidence)
    rows: list[dict] = []
    covered: list[str] = []
    for row in prev_rows:
        if is_placeholder(row):
            continue
        where = row.get("block_id") or ""
        present = _norm(row.get("claim")) in texts.get(where, "") or (
            not where and any(_norm(row.get("claim")) in t for t in texts.values()))
        if present or row.get("status") == "removed":
            rows.append(dict(row))
            if present:
                covered.append(_norm(row.get("claim")))
        else:
            rows.append(dict(row, status="removed", status_reason="no longer in the draft"))
    if revised_block:
        block = next((b for b in draft.get("blocks", []) if b["id"] == revised_block), None)
        for sentence in qa.sentences(block.get("text", "") if block else ""):
            plain = ev.strip_cites(sentence).strip()
            cites = ev.cites_in(sentence)
            if not plain or qa.VERIFY.search(plain) or (not cites and not re.search(r"\d", plain)):
                continue
            if any(c and c in _norm(plain) for c in covered):
                continue
            items = [known[c] for c in cites if c in known]
            ok = bool(items) and len(items) == len(cites)
            rows.append({
                "claim": plain, "block_id": revised_block, "category": "other", "evidence_ids": cites,
                "source": "; ".join(f"{i['source_name']}{' — ' + i['url'] if i['url'] else ''}" for i in items),
                "source_date": "; ".join(i["date"] for i in items if i["date"]),
                "date_checked": today.isoformat(),
                "status": "Verified" if ok else "Needs confirmation",
                "status_reason": "" if ok else "revised text; no source cited",
                "owner": "; ".join(dict.fromkeys(i["owner"] for i in items if i["owner"])) or "comms lead",
            })
    prior_placeholders: dict[str, list[dict]] = {}
    for r in prev_rows:
        if is_placeholder(r):
            prior_placeholders.setdefault(r.get("claim"), []).append(r)
    for m in qa.VERIFY.finditer(markdown):
        token = m.group(0)
        # The n-th occurrence of a token inherits the n-th prior row for it,
        # so identical placeholders keep their own ids across versions.
        queue = prior_placeholders.get(token) or []
        prior = queue.pop(0) if queue else None
        rows.append(dict(prior) if prior else {
            "claim": token, "block_id": "", "category": "other", "evidence_ids": [], "source": "",
            "source_date": "", "date_checked": today.isoformat(), "status": "Needs confirmation",
            "status_reason": "placeholder in the draft", "owner": "comms lead"})
    return [dict(row, n=i + 1) for i, row in enumerate(assign_ids(rows))]


def _replace_everywhere(draft: dict, token: str, value: str) -> int:
    n = 0
    for b in draft.get("blocks", []):
        if token in b.get("text", ""):
            n += b["text"].count(token)
            b.setdefault("history", []).append(b["text"])
            b["text"] = b["text"].replace(token, value)
    for key in ("headline", "subhead", "release_line", "dateline", "media_contact"):
        if token in str(draft.get(key) or ""):
            n += 1
            draft[key] = draft[key].replace(token, value)
    for bp in draft.get("boilerplates", []):
        if token in bp.get("text", ""):
            n += 1
            bp["text"] = bp["text"].replace(token, value)
    for sp in draft.get("speakers", []):
        for key in ("name", "title"):
            if token in str(sp.get(key) or ""):
                n += 1
                sp[key] = sp[key].replace(token, value)
    return n


def apply_verification(draft: dict, rows: list[dict], cid: str, *, note: str, value: str | None,
                       actor_id: str, actor_name: str, now: str) -> tuple[list[dict], str]:
    """A person confirms one fact-check row. A placeholder needs the
    confirmed ``value``, which replaces its token everywhere it is printed.
    Returns ``(rows, what_changed)``; raises ``ValueError`` on a placeholder
    without a value and ``KeyError`` for an unknown or removed row."""
    rows = assign_ids(rows)
    row = next((r for r in rows if claim_id(r) == cid and r.get("status") != "removed"), None)
    if row is None:
        raise KeyError(cid)
    stamp = {"status": "Verified", "status_reason": "confirmed by a person",
             "verified_by": actor_id, "verified_by_name": actor_name, "verified_note": note,
             "verified_at": now}
    if is_placeholder(row):
        if not value:
            raise ValueError("value")
        if _replace_everywhere(draft, row["claim"], value) == 0:
            raise KeyError(cid)
        new_row = dict(row, **stamp, claim=value, source=f"Confirmed by {actor_name}: {note}")
        rows = [new_row if r is row else r for r in rows]
        return rows, f"Filled {row['claim']} with “{value[:80]}” ({note[:80]})"
    rows = [dict(r, **stamp, source=(r.get("source") or "") + ("; " if r.get("source") else "")
                 + f"confirmed by {actor_name}: {note}") if r is row else r for r in rows]
    return rows, f"Verified “{str(row['claim'])[:80]}” ({note[:80]})"


def run_revise(draft: dict, block_id: str, comment: str, *, intake: dict, card_agent: dict,
               evidence: list[dict], angle: dict, today: date, invoke, commenter: str) -> tuple[dict, str]:
    """One model call. Mutates ``draft`` and ``evidence`` (a fact really
    written in the comment becomes an ``rv-N`` item attributed to
    ``commenter``, the signed-in person). Returns ``(draft, change summary)``;
    ``KeyError`` for a block that is not a body block."""
    allowed = ev.allowed_urls(ev.normalize_card(card_agent), intake, evidence)
    result = steps.run_revise_block(draft, block_id, comment, card_agent, evidence,
                                    primary_keyword=angle.get("primary_keyword"), allowed=allowed,
                                    today=today, invoke=invoke, commenter=commenter)
    return result["draft"], str(result.get("change_summary") or comment)[:300]


def remove_comment(claim: str) -> str:
    return ("Remove this claim from the block and keep everything else as it is; add nothing new: "
            f"“{claim}”")


def next_minor(label: str) -> str:
    major, _, minor = (label or "0.0").partition(".")
    return f"{int(major or 0)}.{int(minor or 0) + 1}"


def next_major(label: str) -> str:
    major, _, _ = (label or "0.0").partition(".")
    return f"{int(major or 0) + 1}.0"


def wire_blocks(draft: dict, quote_state: dict[str, str]) -> list[dict]:
    """The draft as the reader sees it, block by block, cites stripped and
    every marker token left in place. A pending quote carries its
    ``[QUOTE – PENDING APPROVAL: …]`` line; an approved one does not."""
    ct = cts.get(draft["content_type"])
    speakers = {s["id"]: s for s in draft.get("speakers", [])}
    brand = draft.get("brand_short_name", "")
    out: list[dict] = []

    def add(bid, kind, text, *, revisable=False, quote_id=None, cites=(), last_comment=None):
        out.append({"id": bid, "kind": kind, "text": text, "revisable": revisable, "quote_id": quote_id,
                    "claim_ids": [], "cites": list(cites), "last_comment": last_comment})

    if ct.release_line and draft.get("release_line"):
        add("release_line", "release_line", draft["release_line"])
    add("headline", "headline", draft.get("headline", ""))
    if ct.subhead_chars and draft.get("subhead"):
        add("subheadline", "subheadline", draft["subhead"])
    if ct.dateline and draft.get("dateline"):
        add("dateline", "dateline", draft["dateline"])
    for b in draft.get("blocks", []):
        text = ev.strip_cites(b.get("text", "")).strip()
        if b.get("kind") == "quote":
            sp = speakers.get(b.get("speaker_id") or "") or {
                "name": "[VERIFY: speaker]", "title": "[VERIFY: title]", "organization": ""}
            text = render._quote_paragraph(text, sp)
            if quote_state.get(b["id"], "pending") != "approved":
                text += "\n" + render.quote_marker(sp, brand)
            add(b["id"], "quote", text, revisable=True, quote_id=b["id"], cites=ev.cites_in(b.get("text", "")),
                last_comment=b.get("last_comment"))
        else:
            add(b["id"], b.get("kind", "body"), text, revisable=True, cites=ev.cites_in(b.get("text", "")),
                last_comment=b.get("last_comment"))
    for i, bp in enumerate(draft.get("boilerplates", []), start=1):
        add(f"boilerplate-{i}", "boilerplate", f"About {bp['brand']}\n{bp['text']}")
    if ct.media_contact:
        add("media_contact", "media_contact", f"Media Contact\n{draft.get('media_contact', '')}")
    if ct.release_line:
        add("end", "end", "###")
    return out


def wire_markers(blocks: list[dict], claims: list[dict]) -> list[dict]:
    placeholder_ids: dict[str, list[str]] = {}
    for c in claims:
        if c.get("placeholder") and c["status"] != "removed":
            placeholder_ids.setdefault(c["text"], []).append(c["id"])
    out = []
    for b in blocks:
        for m in qa.VERIFY.finditer(b["text"]):
            token = m.group(0)
            label = token[1:-1].partition(":")[2].strip() or "fact"
            queue = placeholder_ids.get(token) or []
            out.append({"id": f"m-{len(out) + 1}", "kind": "verify", "block_id": b["id"], "token": token,
                        "label": label, "claim_id": queue.pop(0) if queue else None, "quote_id": None,
                        "resolved": False})
        for m in re.finditer(r"\[MISSING:\s*([^\]]+)\]", b["text"]):
            out.append({"id": f"m-{len(out) + 1}", "kind": "missing", "block_id": b["id"], "token": m.group(0),
                        "label": m.group(1).strip(), "claim_id": None, "quote_id": None, "resolved": False})
        for m in qa.QUOTE_PENDING.finditer(b["text"]):
            out.append({"id": f"m-{len(out) + 1}", "kind": "quote_pending", "block_id": b["id"],
                        "token": m.group(0), "label": m.group(1).strip(), "claim_id": None,
                        "quote_id": b.get("quote_id"), "resolved": False})
    return out


def count_missing(blocks: list[dict]) -> int:
    return sum(len(re.findall(r"\[MISSING:[^\]]*\]", b["text"])) for b in blocks)


# --------------------------------------------------------------------------- #
# A version as stored: printed content in ``package``, everything else beside it
# --------------------------------------------------------------------------- #
# The store counts open ``[VERIFY`` markers by scanning the whole ``package``
# (``firestore_repo.count_open_verify``). QA findings, drafting notes and a
# block's edit history quote old text — "[VERIFY]" included — so they would
# keep an approval blocked after every placeholder in the draft was filled.
# ``package`` therefore holds only what is printed; the rest goes in the
# version's ``qa`` field, which the count does not read.

_CONTENT_KEYS = ("content_type", "brand_short_name", "release_line", "headline", "headline_options", "subhead",
                 "subhead_options", "dateline", "speakers", "boilerplates", "media_contact", "markdown")
_BLOCK_KEYS = ("id", "kind", "text", "speaker_id", "last_comment")


def for_store(draft: dict, evidence: list[dict]) -> tuple[dict, dict]:
    """``(package draft, qa extras)`` for ``create_pr_version``."""
    content = {k: draft[k] for k in _CONTENT_KEYS if k in draft}
    content["blocks"] = [{k: b.get(k) for k in _BLOCK_KEYS} for b in draft.get("blocks", [])]
    extras = {
        "report": draft.get("qa") or {},
        "notes": list(draft.get("notes") or [])[-50:],
        "injection_notes": list(draft.get("injection_notes") or [])[-50:],
        "quote_status": dict(draft.get("quote_status") or {}),
        "history": {b["id"]: list(b.get("history") or [])[-3:] for b in draft.get("blocks", [])},
        "evidence": evidence,
    }
    return content, extras


def from_store(content: dict, extras: dict | None, fallback_evidence: list[dict]) -> tuple[dict, list[dict]]:
    """The working draft and its evidence back from a stored version."""
    extras = extras or {}
    draft = dict(content or {})
    history = extras.get("history") or {}
    draft["blocks"] = [dict(b, history=list(history.get(b["id"]) or [])) for b in (content or {}).get("blocks", [])]
    draft["qa"] = extras.get("report") or {}
    draft["notes"] = list(extras.get("notes") or [])
    draft["injection_notes"] = list(extras.get("injection_notes") or [])
    draft["quote_status"] = dict(extras.get("quote_status") or {})
    evidence = extras.get("evidence")
    return draft, list(evidence if evidence is not None else fallback_evidence)

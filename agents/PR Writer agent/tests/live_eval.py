"""PR Writer live eval — runs the real prompts against the real model slots
and scores every output with the deterministic checks. OPT-IN AND BILLED.

    cd backend
    PR_LIVE_EVAL=1 .venv/Scripts/python "agents/PR Writer agent/tests/live_eval.py"
    #   --only b03-ai-intake-launch      one brief
    #   --main-model M --fast-model F    pin model ids (else env/.env defaults)
    #   --out DIR                        results folder (default: system temp)

Not a pytest module (pytest never collects it): the suite's guards blank the
OpenRouter key on purpose, and this script exists to spend it. It refuses to
run without ``PR_LIVE_EVAL=1``. Firestore is blocked before anything loads,
so model ids come from the environment, not production config, and nothing
is written to the database. Calls bill the shared OpenRouter account; one
full pass is roughly 20 briefs x up to 7 calls.

What a brief passes on (from its ``expect`` block):
- escalation verdict matches (and ``decided_by`` when given); an escalated
  brief stops there, as the blueprint pauses it;
- every expected missing field is reported missing;
- no trap phrase from the pasted text reaches the draft or snippets;
- every ``must_keep`` stated input survives into the draft;
- after one QA-fix round, no QA *errors* remain (a missing card boilerplate
  excepted; briefs tagged "missing" draft with placeholders and are scored
  on intake only).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[2]
# Errors that mean "the brief lacked a fact", which the blueprint answers with
# a placeholder, not a failure of the writer.
PLACEHOLDER_ERRORS = {"boilerplate"}


def _setup(main_model: str | None, fast_model: str | None):
    sys.path.insert(0, str(BACKEND))
    import app  # noqa: F401 — registers agent roots
    sys.path.insert(0, str(BACKEND / "agents" / "PR Writer agent"))
    from app.services import firestore_repo

    def _blocked():
        raise RuntimeError("Firestore is blocked in the PR Writer live eval")

    firestore_repo._db = _blocked

    from pr_writer_agent import llm as pr_llm

    if not (main_model or fast_model):
        return pr_llm.default_invoke

    def invoke(system, user, *, slot, timeout_s, max_tokens, temperature):
        from app.services.openrouter import get_llm
        model = main_model if slot == "main" else fast_model
        try:
            llm = get_llm(temperature, fast=(slot == "fast"), model=model, agent_id=pr_llm.AGENT_ID,
                          timeout=timeout_s, max_tokens=max_tokens)
            msg = llm.invoke([("system", system), ("user", user)])
        except Exception as exc:  # noqa: BLE001
            raise pr_llm.LLMUnavailable(f"model call failed ({slot}): {type(exc).__name__}: {exc}") from exc
        meta, usage = msg.response_metadata or {}, msg.usage_metadata or {}
        return pr_llm.Reply(str(msg.content), meta.get("finish_reason"), str(meta.get("model_name") or model),
                            int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0))

    return invoke


def _add_usage(total: dict, usage: dict, step: str, seconds: float, timings: dict) -> None:
    total["calls"] += usage.get("calls", 0)
    total["input_tokens"] += usage.get("input_tokens", 0)
    total["output_tokens"] += usage.get("output_tokens", 0)
    timings[step] = round(seconds, 1)


def run_brief(brief: dict, card: dict, invoke) -> dict:
    from pr_writer_agent import escalation, steps
    from pr_writer_agent import evidence as ev
    from pr_writer_agent import qa

    exp = brief["expect"]
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    timings: dict = {}
    out = {"id": brief["id"], "failures": [], "usage": usage, "timings": timings}

    t = time.monotonic()
    intake = steps.run_intake(brief["sentence"], stated=brief["stated"], pasted=brief.get("pasted", ""),
                              known_brands=[card["short_name"]], invoke=invoke)
    _add_usage(usage, intake["usage"], "intake", time.monotonic() - t, timings)
    out["missing"] = intake["missing"]
    lost = sorted(set(exp["missing"]) - set(intake["missing"]))
    if lost:
        out["failures"].append(f"missing fields not reported: {lost}")

    t = time.monotonic()
    try:
        decision = escalation.screen(intake["intake"], pasted=brief.get("pasted", ""),
                                     brand_names=[card["short_name"]], invoke=invoke).to_dict()
    except escalation.EscalationCheckFailed as exc:
        out["failures"].append(f"screen failed: {exc}")
        return out
    _add_usage(usage, decision.get("usage") or {}, "screen", time.monotonic() - t, timings)
    out["escalation"] = {k: decision[k] for k in ("escalate", "categories", "reason", "decided_by")}
    if decision["escalate"] != exp["escalate"]:
        out["failures"].append(f"escalate={decision['escalate']}, expected {exp['escalate']}")
    if exp.get("decided_by") and decision["decided_by"] != exp["decided_by"]:
        out["failures"].append(f"decided_by={decision['decided_by']}, expected {exp['decided_by']}")
    if decision["escalate"]:
        missed = set(exp.get("categories", [])) - set(decision["categories"])
        if missed:
            out["failures"].append(f"escalation categories missed: {sorted(missed)}")
        return out  # the blueprint pauses here for the comms lead and legal

    today = date.today()
    evidence = steps.build_evidence(intake["intake"], card)
    speakers, _ = ev.speakers(intake["intake"], card)
    allowed = ev.allowed_urls(card, intake["intake"], evidence)

    t = time.monotonic()
    angle = steps.run_angle(intake["intake"], card, evidence, invoke=invoke)
    _add_usage(usage, angle["usage"], "angle", time.monotonic() - t, timings)
    kw = angle["angle"]["primary_keyword"]

    t = time.monotonic()
    draft = steps.run_draft(intake["intake"], card, evidence, angle["angle"], speakers, today=today, invoke=invoke)
    _add_usage(usage, draft["usage"], "draft", time.monotonic() - t, timings)
    out["qa_first_draft"] = draft["qa"]["counts"]

    t = time.monotonic()
    draft = steps.run_qa_fix(draft, card, evidence, primary_keyword=kw, allowed=allowed, today=today, invoke=invoke)
    _add_usage(usage, draft["usage"], "qa_fix", time.monotonic() - t, timings)

    t = time.monotonic()
    facts = steps.run_fact_check(draft, evidence, today=today, invoke=invoke)
    _add_usage(usage, facts["usage"], "fact_check", time.monotonic() - t, timings)

    t = time.monotonic()
    snippets = steps.run_snippets(draft, invoke=invoke)
    _add_usage(usage, snippets["usage"], "snippets", time.monotonic() - t, timings)

    md = draft["markdown"]
    # Placeholders and pending markers are stripped first: a trap quoted inside
    # "[VERIFY: is it clinically proven?]" is the writer refusing it, not leaking it.
    blob = qa.scan_text(md + "\n" + "\n".join(str(v) for v in snippets["snippets"].values())).lower()
    for trap in exp["traps"]:
        if trap.lower() in blob:
            out["failures"].append(f"trap phrase reached the output: {trap!r}")
    for keep in exp["must_keep"]:
        if keep.lower() not in md.lower():
            out["failures"].append(f"stated input lost: {keep!r}")
    errors = [f for f in draft["qa"]["findings"] if f["severity"] == "error" and f["check"] not in PLACEHOLDER_ERRORS]
    if errors and "missing" not in brief["tags"]:  # a brief missing its facts drafts with placeholders
        out["failures"].append("QA errors after fix: " + "; ".join(f"{f['check']}@{f['where']}" for f in errors))
    out.update({
        "qa_after_fix": draft["qa"]["counts"],
        "fact_rows": len(facts["rows"]),
        "fact_rows_open": sum(1 for r in facts["rows"] if r["status"] != "Verified"),
        "snippet_findings": snippets["qa"],
        "markdown": md,
        "notes": draft.get("notes", []),
    })
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--main-model")
    parser.add_argument("--fast-model")
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "pr_writer_eval"))
    args = parser.parse_args(argv)

    if os.environ.get("PR_LIVE_EVAL") != "1":
        print("PR Writer live eval is opt-in and bills the shared OpenRouter account. "
              "Set PR_LIVE_EVAL=1 to run it.", file=sys.stderr)
        return 2

    invoke = _setup(args.main_model, args.fast_model)
    card = json.loads((HERE / "fixtures" / "brand_card_legalsoft.json").read_text(encoding="utf-8"))
    briefs = json.loads((HERE / "fixtures" / "briefs.json").read_text(encoding="utf-8"))["briefs"]
    if args.only:
        briefs = [b for b in briefs if b["id"] in args.only]

    results = []
    for brief in briefs:
        t = time.monotonic()
        try:
            r = run_brief(brief, card, invoke)
        except Exception as exc:  # noqa: BLE001 — record the honest failure, keep going
            r = {"id": brief["id"], "failures": [f"{type(exc).__name__}: {exc}"], "usage": {}}
        r["seconds"] = round(time.monotonic() - t, 1)
        results.append(r)
        status = "PASS" if not r["failures"] else "FAIL"
        print(f"{status}  {brief['id']:<36} {r['seconds']:>6}s  {r.get('usage', {})}")
        for f in r["failures"]:
            print(f"      - {f}")

    passed = sum(1 for r in results if not r["failures"])
    tok_in = sum(r.get("usage", {}).get("input_tokens", 0) for r in results)
    tok_out = sum(r.get("usage", {}).get("output_tokens", 0) for r in results)
    print(f"\n{passed}/{len(results)} briefs passed; tokens in {tok_in:,} / out {tok_out:,}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"pr_writer_eval_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({"passed": passed, "total": len(results), "results": results}, indent=1),
                    encoding="utf-8")
    print(f"results: {path}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

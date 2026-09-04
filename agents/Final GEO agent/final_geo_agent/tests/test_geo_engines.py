"""GEO engine adapters — response-shape parsing against recorded fixtures.

httpx is stubbed at the module seam; no network, no keys."""
import base64
import json
import pathlib

import httpx
import pytest

from final_geo_agent import geo_engines, geo_window

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

_real_dataforseo_creds = geo_engines.dataforseo_creds


@pytest.fixture(autouse=True)
def _no_dataforseo(monkeypatch):
    # a developer's real DATAFORSEO_* pair (local .env, exported env) must never
    # turn an offline test into a billed live SERP call; tests of the DataForSEO
    # path re-patch this seam themselves
    monkeypatch.setattr(geo_engines, "dataforseo_creds", lambda: ("", ""))


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or "err"

    def json(self):
        return self._payload


def patch_post(monkeypatch, resp: FakeResp):
    monkeypatch.setattr(geo_engines.httpx, "post", lambda *a, **k: resp)


def capture_post(monkeypatch, resp: FakeResp) -> dict:
    """Stub ``httpx.post`` and hand back what the adapter sent."""
    sent: dict = {}

    def post(url, headers=None, json=None, **kw):
        sent.update(url=url, headers=headers or {}, body=json)
        return resp

    monkeypatch.setattr(geo_engines.httpx, "post", post)
    return sent


PERPLEXITY_OK = {
    "choices": [{"message": {"content": "Legal Soft is a top provider."}}],
    "search_results": [
        {"url": "https://www.g2.com/products/x", "title": "G2 reviews"},
        {"url": "https://reddit.com/r/law/1", "title": "Reddit thread"},
    ],
}

GEMINI_OK = {
    "candidates": [{
        "content": {"parts": [{"text": "Here are the best options."}, {"text": "More detail."}]},
        "groundingMetadata": {
            "groundingChunks": [
                {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
                         "title": "clio.com"}},
                {"web": {"uri": "https://example.org/page", "title": "An article title"}},
            ]
        },
    }]
}

OPENAI_OK = {
    "output": [
        {"content": [{"text": "ChatGPT-style answer.",
                      "annotations": [{"type": "url_citation",
                                       "url": "https://capterra.com/p/1", "title": "Capterra"}]}]},
    ]
}


def test_perplexity_parses_text_and_search_results(monkeypatch):
    patch_post(monkeypatch, FakeResp(200, PERPLEXITY_OK))
    ans = geo_engines.poll_perplexity("best legal va", "pplx-test")
    assert ans.error is None
    assert "Legal Soft" in ans.text
    assert [c["domain"] for c in ans.citations] == ["g2.com", "reddit.com"]


def test_perplexity_falls_back_to_bare_citations(monkeypatch):
    payload = {"choices": [{"message": {"content": "hi"}}],
               "citations": ["https://www.example.com/a"]}
    patch_post(monkeypatch, FakeResp(200, payload))
    ans = geo_engines.poll_perplexity("q", "pplx-test")
    assert ans.citations == [{"url": "https://www.example.com/a",
                              "domain": "example.com", "title": ""}]


def test_gemini_joins_parts_and_trusts_domain_titles(monkeypatch):
    patch_post(monkeypatch, FakeResp(200, GEMINI_OK))
    ans = geo_engines.poll_gemini("q", "AIza-test")
    assert ans.text == "Here are the best options.\nMore detail."
    assert ans.citations[0]["domain"] == "clio.com"          # title looked like a domain
    assert ans.citations[1]["domain"] == "example.org"       # fell back to the uri


def test_openai_collects_text_and_url_citations(monkeypatch):
    patch_post(monkeypatch, FakeResp(200, OPENAI_OK))
    ans = geo_engines.poll_openai("q", "sk-test")
    assert ans.text == "ChatGPT-style answer."
    assert ans.citations == [{"url": "https://capterra.com/p/1",
                              "domain": "capterra.com", "title": "Capterra"}]


def test_http_error_is_captured_not_raised(monkeypatch):
    patch_post(monkeypatch, FakeResp(401, text="unauthorized"))
    ans = geo_engines.poll_perplexity("q", "pplx-bad")
    assert ans.error and "HTTP 401" in ans.error
    assert ans.text == ""


def test_transport_exception_is_captured(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectTimeout("timeout")
    monkeypatch.setattr(geo_engines.httpx, "post", boom)
    ans = geo_engines.poll_gemini("q", "k")
    assert ans.error and "ConnectTimeout" in ans.error


def test_poll_engine_without_any_key_reports_unavailable(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "")
    ans = geo_engines.poll_engine("perplexity", "q")
    assert ans.error == "no API key configured"


OPENROUTER_OK = {
    "choices": [{"message": {
        "content": "Legal Soft leads this space.",
        "annotations": [
            {"type": "url_citation",
             "url_citation": {"url": "https://clutch.co/profile/x", "title": "Clutch"}},
        ],
    }}],
}


def test_openrouter_fallback_when_no_native_key(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "or-key")
    patch_post(monkeypatch, FakeResp(200, OPENROUTER_OK))
    ans = geo_engines.poll_engine("chatgpt", "best legal va")
    assert ans.error is None and ans.via == "openrouter"
    assert ans.model == geo_engines.OPENROUTER_ENGINE_MODELS["chatgpt"]
    assert [c["domain"] for c in ans.citations] == ["clutch.co"]


def test_native_quota_error_falls_back_to_openrouter(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "native-key")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "or-key")
    calls = []

    def post(url, **kw):
        calls.append(url)
        if "openrouter" in url:
            return FakeResp(200, OPENROUTER_OK)
        return FakeResp(429, text="quota exceeded")

    monkeypatch.setattr(geo_engines.httpx, "post", post)
    ans = geo_engines.poll_engine("gemini", "q")
    assert ans.via == "openrouter" and ans.error is None
    assert len(calls) == 2                       # native tried first, then fallback


def test_available_engines_lights_up_with_openrouter_only(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "or-key")
    engines = geo_engines.available_engines()
    assert engines["perplexity"] and engines["gemini"] and engines["chatgpt"]
    # SERP engines need DataForSEO — a chat fallback can't fake a SERP
    assert engines["aio"] is False
    assert engines["ai_mode"] is False


def test_poll_engine_unknown_engine():
    ans = geo_engines.poll_engine("bing", "q")
    assert ans.error == "unknown engine: bing"


def test_answer_text_capped():
    ans = geo_engines.EngineAnswer(engine="perplexity", text="x" * 10000)
    assert len(ans.to_dict()["text"]) == geo_engines.ANSWER_TEXT_CAP


def test_to_dict_caps_citations_and_lengths():
    ans = geo_engines.EngineAnswer(
        engine="gemini", text="x" * 9000,
        citations=[{"url": "https://vertexaisearch.example/" + "r" * 900,
                    "domain": f"site{i}.com", "title": "t" * 500} for i in range(30)],
    )
    d = ans.to_dict()
    assert len(d["text"]) == geo_engines.ANSWER_TEXT_CAP
    assert len(d["citations"]) == geo_engines.CITATIONS_CAP        # 1MB day-doc guard
    assert len(d["citations"][0]["url"]) == geo_engines.CITATION_URL_CAP
    assert len(d["citations"][0]["title"]) == geo_engines.CITATION_TITLE_CAP


# --------------------------------------------- Google SERP via DataForSEO ----
# The only search provider. Both endpoints answer in one envelope and put the
# AI answer in one item of ``result[0].items``; the parser accepts every
# spelling of that item's ``type`` the vendor has used, because guessing wrong
# does not raise — it files "Google showed no AI answer" forever.


def test_dataforseo_aio_parses_markdown_and_references(monkeypatch):
    sent = capture_post(monkeypatch, FakeResp(200, fixture("dataforseo_aio_ok")))

    ans = geo_engines.poll_dataforseo("aio", "best legal intake service", "login", "pw")

    assert ans.error is None and ans.no_aio is False
    assert ans.via == "dataforseo" and ans.model == "google-ai-overview"
    assert "Legal Soft" in ans.text and "Smith.ai" in ans.text
    assert ans.credits == 1
    # the vendor's domain wins over one parsed from the url, www stripped
    assert [c["domain"] for c in ans.citations] == ["lawyerist.com", "clio.com", "legalsoft.com"]
    assert ans.citations[0]["url"] == "https://lawyerist.com/reviews/legal-intake/"     # url
    assert ans.citations[1]["url"] == "https://www.clio.com/blog/legal-intake-services/"  # link
    assert ans.citations[2]["url"] == "https://legalsoft.com"                            # domain only
    assert ans.citations[0]["title"] == "Best Legal Intake Services"
    assert sent["url"].endswith("/serp/google/organic/live/advanced")
    assert sent["body"] == [{
        "keyword": "best legal intake service", "location_code": 2840,
        "language_code": "en", "device": "desktop",
    }]


def test_dataforseo_sends_basic_auth_built_from_login_and_password(monkeypatch):
    sent = capture_post(monkeypatch, FakeResp(200, fixture("dataforseo_aio_ok")))
    geo_engines.poll_dataforseo("aio", "q", "me@example.com", "s3cret")
    expected = "Basic " + base64.b64encode(b"me@example.com:s3cret").decode()
    assert sent["headers"]["Authorization"] == expected


def test_dataforseo_deferred_overview_is_no_aio_not_an_empty_answer(monkeypatch):
    """THE gotcha: Google defers the overview — the item is there with
    ``asynchronous_ai_overview: true`` and nothing in it. That is "no overview
    was shown when we looked", never an answer with 0 citations."""
    patch_post(monkeypatch, FakeResp(200, fixture("dataforseo_aio_async_stub")))

    ans = geo_engines.poll_dataforseo("aio", "q", "l", "p")

    assert ans.no_aio is True and ans.error is None
    assert ans.text == "" and ans.citations == []
    assert ans.to_dict()["no_aio"] is True


def test_a_deferred_overview_and_a_genuine_absence_store_identically(monkeypatch):
    """RECORDED, NOT FIXED — deliberately.

    Google defers an overview (the item is present, ``asynchronous_ai_overview:
    true``, empty) and Google publishing none at all are DIFFERENT events, and
    the stored record cannot tell them apart: same ``no_aio``, same empty text,
    same empty citations, byte-identical. Resolving a deferred overview means a
    second, separately billed DataForSEO fetch, which is a spend decision and
    not one this layer makes.

    What that costs, precisely, so the choice is made with the number in view:
    ``no_aio`` rows sit OUTSIDE every rate denominator (``geo_metrics.
    measurable``), so a deferred overview does not drag the mention rate down —
    it is `n_no_aio` that over-counts, and the panel labels that "Google
    published no AI Overview", which for this subset is not what happened.

    If the follow-up fetch is ever bought, this test is the list of what has to
    change with it.
    """
    deferred = fixture("dataforseo_aio_async_stub")
    absent = fixture("dataforseo_aio_ok")
    result = absent["tasks"][0]["result"][0]
    result["items"] = [i for i in result["items"] if i["type"] != "ai_overview"]

    patch_post(monkeypatch, FakeResp(200, deferred))
    one = geo_engines.poll_dataforseo("aio", "q", "l", "p").to_dict()
    patch_post(monkeypatch, FakeResp(200, absent))
    other = geo_engines.poll_dataforseo("aio", "q", "l", "p").to_dict()

    one.pop("latency_ms"), other.pop("latency_ms")   # timing, not measurement
    assert one == other
    assert one["no_aio"] is True and one["error"] is None


def test_dataforseo_serp_without_an_overview_item_is_no_aio(monkeypatch):
    payload = fixture("dataforseo_aio_ok")
    result = payload["tasks"][0]["result"][0]
    result["items"] = [i for i in result["items"] if i["type"] != "ai_overview"]
    patch_post(monkeypatch, FakeResp(200, payload))

    ans = geo_engines.poll_dataforseo("aio", "q", "l", "p")

    assert ans.no_aio is True and ans.error is None


def test_dataforseo_ai_mode_parses_and_hits_its_own_endpoint(monkeypatch):
    sent = capture_post(monkeypatch, FakeResp(200, fixture("dataforseo_ai_mode_ok")))

    ans = geo_engines.poll_dataforseo("ai_mode", "best legal intake service", "l", "p")

    assert ans.engine == "ai_mode" and ans.model == "google-ai-mode" and ans.via == "dataforseo"
    assert ans.error is None and ans.no_aio is False
    assert "Legal Soft" in ans.text and "MyCase" in ans.text
    assert [c["domain"] for c in ans.citations] == [
        "mycase.com", "getperspective.ai", "smith.ai", "legalsoft.com",
    ]
    assert "/serp/google/ai_mode/live/advanced" in sent["url"]
    assert "device" not in sent["body"][0]


def test_dataforseo_ai_mode_with_nothing_at_all_is_no_aio(monkeypatch):
    """Empty item, empty everything = Google put no AI answer on the surface.
    A completed observation, not an error and not a blank answer."""
    payload = fixture("dataforseo_ai_mode_ok")
    block = payload["tasks"][0]["result"][0]["items"][0]
    block["markdown"], block["items"], block["references"] = "", [], []
    patch_post(monkeypatch, FakeResp(200, payload))

    ans = geo_engines.poll_dataforseo("ai_mode", "q", "l", "p")

    assert ans.no_aio is True and ans.error is None
    assert ans.text == "" and ans.citations == []


def test_dataforseo_ai_mode_reads_nested_elements_when_markdown_is_missing(monkeypatch):
    """Shape drift, not silence: the top-level ``markdown`` is gone but the
    nested elements still carry the answer. Reporting ``no_aio`` here would
    file a real Google answer as "Google said nothing" — the exact lie this
    engine is being rebuilt to stop telling."""
    payload = fixture("dataforseo_ai_mode_ok")
    block = payload["tasks"][0]["result"][0]["items"][0]
    del block["markdown"]
    patch_post(monkeypatch, FakeResp(200, payload))

    ans = geo_engines.poll_dataforseo("ai_mode", "q", "l", "p")

    assert ans.no_aio is False and ans.error is None
    assert "Legal Soft" in ans.text and "Smith.ai" in ans.text
    assert [c["domain"] for c in ans.citations][:1] == ["mycase.com"]


@pytest.mark.parametrize("item_type", ["ai_overview", "ai_mode", "ai_mode_message"])
def test_dataforseo_ai_mode_accepts_every_spelling_of_the_answer_item(
    monkeypatch, item_type
):
    """DataForSEO documents ``ai_overview`` on the AI Mode endpoint and its own
    writing also names ``ai_mode_message``. An unrecognised type does not raise
    — it reads as "no AI answer" forever — so every spelling is accepted."""
    payload = fixture("dataforseo_ai_mode_ok")
    payload["tasks"][0]["result"][0]["items"][0]["type"] = item_type
    patch_post(monkeypatch, FakeResp(200, payload))

    ans = geo_engines.poll_dataforseo("ai_mode", "q", "l", "p")

    assert ans.no_aio is False and ans.error is None and "Legal Soft" in ans.text


def test_dataforseo_ai_mode_unknown_item_type_is_no_aio_not_a_crash(monkeypatch):
    """The floor under the guess above: a type nobody has seen degrades to the
    honest observation, never an exception out of an adapter."""
    payload = fixture("dataforseo_ai_mode_ok")
    payload["tasks"][0]["result"][0]["items"][0]["type"] = "ai_something_new"
    patch_post(monkeypatch, FakeResp(200, payload))

    ans = geo_engines.poll_dataforseo("ai_mode", "q", "l", "p")

    assert ans.no_aio is True and ans.error is None and ans.text == ""


def test_dataforseo_ai_mode_collects_per_section_references_deduped(monkeypatch):
    """AI Mode attributes sources per section as well as at the top. Every
    source Google credited counts once — a citation list that misses the
    nested ones under-reports the brands the answer actually linked."""
    payload = fixture("dataforseo_ai_mode_ok")
    block = payload["tasks"][0]["result"][0]["items"][0]
    block["items"][0]["references"] = [
        {"type": "ai_overview_reference", "domain": "www.legalsoft.com",
         "url": "https://www.legalsoft.com/intake", "title": "dupe of a top-level ref"},
        {"type": "ai_overview_reference", "domain": "lawyerist.com",
         "url": "https://lawyerist.com/reviews/x", "title": "Lawyerist"},
    ]
    patch_post(monkeypatch, FakeResp(200, payload))

    ans = geo_engines.poll_dataforseo("ai_mode", "q", "l", "p")

    domains = [c["domain"] for c in ans.citations]
    assert domains == ["mycase.com", "getperspective.ai", "smith.ai",
                       "legalsoft.com", "lawyerist.com"]
    assert domains.count("legalsoft.com") == 1


def test_dataforseo_success_with_no_result_payload_is_an_error(monkeypatch):
    """20000 with nothing in it measured NOTHING. Banking that as ``no_aio``
    would write a permanent zero into the brand's score from a failed call."""
    payload = fixture("dataforseo_ai_mode_ok")
    payload["tasks"][0]["result"] = None
    patch_post(monkeypatch, FakeResp(200, payload))

    ans = geo_engines.poll_dataforseo("ai_mode", "q", "l", "p")

    assert ans.error == "DataForSEO returned no result for the task"
    assert ans.no_aio is False and ans.text == ""


def test_dataforseo_task_level_error_is_an_error_with_code_and_message(monkeypatch):
    patch_post(monkeypatch, FakeResp(200, fixture("dataforseo_task_error")))

    ans = geo_engines.poll_dataforseo("aio", "q", "l", "p")

    assert ans.error == "DataForSEO 40501: Invalid Field: 'location_code'."
    assert ans.no_aio is False and ans.text == ""


def test_dataforseo_top_level_error_is_an_error(monkeypatch):
    patch_post(monkeypatch, FakeResp(200, {
        "status_code": 40101, "status_message": "Auth error. Invalid login/password.",
        "tasks": [],
    }))

    ans = geo_engines.poll_dataforseo("ai_mode", "q", "l", "p")

    assert ans.error == "DataForSEO 40101: Auth error. Invalid login/password."


def test_dataforseo_http_error_is_captured_not_raised(monkeypatch):
    patch_post(monkeypatch, FakeResp(502, text="bad gateway"))
    ans = geo_engines.poll_dataforseo("aio", "q", "l", "p")
    assert ans.error and "HTTP 502" in ans.error


def test_dataforseo_transport_exception_is_captured(monkeypatch):
    def boom(*a, **k):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(geo_engines.httpx, "post", boom)
    ans = geo_engines.poll_dataforseo("ai_mode", "q", "l", "p")
    assert ans.error and "ReadTimeout" in ans.error


def test_dataforseo_creds_need_both_halves(monkeypatch):
    """Half a Basic-auth credential is no credential: the engines stay off
    rather than sending an auth header that cannot work."""
    values = {"dataforseo_login": "me@example.com", "dataforseo_password": ""}
    monkeypatch.setattr(geo_engines.runtime_config, "get",
                        lambda field: values.get(field, ""))
    assert _real_dataforseo_creds() == ("", "")
    values["dataforseo_password"] = "pw"
    assert _real_dataforseo_creds() == ("me@example.com", "pw")


def test_dataforseo_creds_are_real_settings_fields_with_an_admin_override():
    """The resolution the docstring advertises has to exist, or ``get`` returns
    "" forever and the admin layer is unreachable dead code (the
    ``gd_polish_image_model`` defect, one agent over)."""
    from app.config import settings
    from app.services import runtime_config

    for field in ("dataforseo_login", "dataforseo_password"):
        assert isinstance(getattr(settings, field, None), str)
        assert field in runtime_config.OVERRIDE_FIELDS


# ------------------------------------------------------- the vendor seam ----
# The engine id is what the product measures; the vendor is how it was fetched.
# There is one search vendor now: DataForSEO serves both SERP engines, and no
# credential means no measurement — never a quiet zero.


def test_both_serp_engines_are_served_by_dataforseo(monkeypatch):
    monkeypatch.setattr(geo_engines, "dataforseo_creds", lambda: ("l", "p"))
    patch_post(monkeypatch, FakeResp(200, fixture("dataforseo_aio_ok")))
    assert geo_engines.poll_engine("aio", "q").via == "dataforseo"

    patch_post(monkeypatch, FakeResp(200, fixture("dataforseo_ai_mode_ok")))
    assert geo_engines.poll_engine("ai_mode", "q").via == "dataforseo"

    assert geo_engines.serp_vendor("aio") == "dataforseo"
    assert geo_engines.serp_vendor("ai_mode") == "dataforseo"


@pytest.mark.parametrize("engine", ["aio", "ai_mode"])
def test_serp_engine_without_credentials_fails_loudly(monkeypatch, engine):
    """No credential is an ERROR, and the message names the env vars to set.
    It is emphatically not ``no_aio``: that would average a missing key into
    the brand's visibility score as if Google had stayed silent."""
    def never(*a, **k):
        raise AssertionError("a SERP call was attempted with no credentials")
    monkeypatch.setattr(geo_engines.httpx, "post", never)

    ans = geo_engines.poll_engine(engine, "q")

    assert ans.no_aio is False and ans.text == ""
    assert ans.error == (
        "DataForSEO credentials are not configured — set "
        "DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD"
    )
    assert geo_engines.serp_vendor(engine) == "off"


def test_no_provider_is_selected_behind_a_flag(monkeypatch):
    """One provider, no fallback selection: nothing in the module still knows
    how to reach a second search vendor."""
    monkeypatch.setattr(geo_engines, "dataforseo_creds", lambda: ("l", "p"))
    assert set(geo_engines.MODE_MEANING) == {"native", "proxy", "dataforseo", "off"}
    assert not hasattr(geo_engines, "serpapi_key")
    assert not hasattr(geo_engines, "poll_aio")
    assert {geo_engines.serp_vendor(e) for e in geo_engines.SERP_ENGINES} == {"dataforseo"}


# ------------------------------------------------------------- the specs ----


def test_engine_specs_name_every_engine_once_in_panel_order():
    assert geo_engines.ALL_ENGINES == ("perplexity", "gemini", "chatgpt", "aio", "ai_mode")
    assert geo_engines.SERP_ENGINES == ("aio", "ai_mode")
    assert geo_engines.AIO_ENGINE in geo_engines.ALL_ENGINES
    assert geo_engines.AI_MODE_ENGINE in geo_engines.ALL_ENGINES
    # the reader's constant derives from the specs, so no reader can miss one
    assert geo_window.ENGINES == geo_engines.ALL_ENGINES


def test_every_spec_is_well_formed():
    for engine, spec in geo_engines.ENGINE_SPECS.items():
        assert spec.id == engine
        assert spec.kind in ("chat", "serp")
        assert spec.runs_per_prompt >= 1
        assert spec.label
        assert callable(spec.poll) and callable(spec.status)
        assert set(spec.status()) == {"connected", "mode", "model", "means"}


def test_serp_specs_run_once_over_discovery_prompts_and_chat_specs_sample_everything():
    for engine in geo_engines.SERP_ENGINES:
        spec = geo_engines.ENGINE_SPECS[engine]
        assert spec.kind == "serp"
        assert spec.runs_per_prompt == 1
        assert spec.intents == ("category", "problem")
    for engine in geo_engines.ENGINE_KEY_FIELDS:
        spec = geo_engines.ENGINE_SPECS[engine]
        assert spec.kind == "chat"
        assert spec.runs_per_prompt == geo_engines.CHAT_RUNS_PER_PROMPT == 3
        assert spec.intents is None


def test_human_labels():
    assert geo_engines.ENGINE_LABELS["aio"] == "Google AI Overview"
    assert geo_engines.ENGINE_LABELS["ai_mode"] == "Google AI Mode"
    assert geo_engines.ENGINE_SPECS["chatgpt"].label == "ChatGPT"


# ---------------------------------------------------------------- provenance
# A green chip that says "Perplexity" while an OpenRouter stand-in answered is
# the single fastest way to lose a user's trust in every number on the panel.
# available_engines() collapses that distinction; engine_status() must keep it.


def test_engine_status_marks_openrouter_fallback_as_proxy(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "or-key")

    status = geo_engines.engine_status()

    assert status["perplexity"]["mode"] == "proxy"
    assert status["perplexity"]["model"] == "perplexity/sonar"
    assert "similar model" in status["perplexity"]["means"]
    # still usable — proxy is connected, it just isn't the real product
    assert status["perplexity"]["connected"] is True
    # and the old boolean would have said nothing but "true"
    assert geo_engines.available_engines()["perplexity"] is True


def test_engine_status_marks_native_key_as_native(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "native-key")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "or-key")

    status = geo_engines.engine_status()

    assert status["gemini"]["mode"] == "native"
    assert status["gemini"]["model"] == geo_engines.GEMINI_MODEL


def test_engine_status_reports_off_when_no_key_at_all(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "")

    status = geo_engines.engine_status()

    assert status["chatgpt"] == {
        "connected": False, "mode": "off", "model": "",
        "means": geo_engines.MODE_MEANING["off"],
    }
    # SERP engines have no chat fallback — no vendor means genuinely nothing measured
    assert status[geo_engines.AIO_ENGINE]["mode"] == "off"
    assert status[geo_engines.AI_MODE_ENGINE]["mode"] == "off"
    assert status[geo_engines.AI_MODE_ENGINE]["model"] == ""
    assert set(status) == set(geo_engines.ALL_ENGINES)


def test_engine_status_reports_dataforseo_for_both_serp_engines(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "")
    monkeypatch.setattr(geo_engines, "dataforseo_creds", lambda: ("l", "p"))

    status = geo_engines.engine_status()

    assert status["aio"] == {
        "connected": True, "mode": "dataforseo", "model": "google-ai-overview",
        "means": geo_engines.MODE_MEANING["dataforseo"],
    }
    assert status["ai_mode"] == {
        "connected": True, "mode": "dataforseo", "model": "google-ai-mode",
        "means": geo_engines.MODE_MEANING["dataforseo"],
    }
    assert "DataForSEO" in status["ai_mode"]["means"]
    assert "real consumer surface" in status["ai_mode"]["means"]
    engines = geo_engines.available_engines()
    assert engines["aio"] is True and engines["ai_mode"] is True
    assert engines["perplexity"] is False


# ------------------------------------------- how each engine is ASKED, published
# The console mirrored `SERP_ENGINES = ["aio", "ai_mode"]` and
# `CHAT_RUNS_PER_PROMPT = 3` as frontend constants so it could explain why the
# engines return different numbers of answers. `engine_shapes` publishes the
# fact instead, so there is one definition rather than a copy that starts lying
# the day the real one changes.


def test_engine_shapes_is_a_read_of_the_specs_not_a_second_copy():
    shapes = geo_engines.engine_shapes()

    assert set(shapes) == set(geo_engines.ENGINE_SPECS)
    for engine, spec in geo_engines.ENGINE_SPECS.items():
        assert shapes[engine]["label"] == spec.label
        assert shapes[engine]["kind"] == spec.kind
        assert shapes[engine]["runs_per_prompt"] == spec.runs_per_prompt


def test_engine_shapes_carries_both_halves_of_the_count_difference():
    """A chat engine is sampled three times over every question; a billed SERP
    engine is fetched once and only on the discovery questions. Publishing the
    sample size without the intents explains half the gap and mis-explains the
    rest."""
    shapes = geo_engines.engine_shapes()

    assert shapes["chatgpt"]["runs_per_prompt"] == geo_engines.CHAT_RUNS_PER_PROMPT
    assert shapes["chatgpt"]["intents"] is None           # every question
    assert shapes["aio"]["runs_per_prompt"] == geo_engines.SERP_RUNS_PER_PROMPT
    assert shapes["aio"]["intents"] == list(geo_engines.SERP_INTENTS)
    # `intents: None` must survive JSON as null, not as an empty list that would
    # read as "this engine is asked no questions at all"
    assert json.loads(json.dumps(shapes))["chatgpt"]["intents"] is None


def test_the_serp_engine_set_is_derivable_from_what_is_published():
    """The exact constant the frontend was mirroring."""
    shapes = geo_engines.engine_shapes()
    assert [e for e, s in shapes.items() if s["kind"] == "serp"] == list(
        geo_engines.SERP_ENGINES
    )

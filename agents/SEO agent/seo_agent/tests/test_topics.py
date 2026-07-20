from seo_agent.schemas import TermTarget
from seo_agent.topics import group_topics

TARGETS = [
    TermTarget(term="intake", weight=1.0, min_count=2, max_count=5),
    TermTarget(term="salary", weight=0.8, min_count=1, max_count=3),
    TermTarget(term="qualification", weight=0.6, min_count=1, max_count=2),
]
PAA = ["What does a legal intake specialist do?"]


class FakeLLM:
    def __init__(self, content):
        self._content = content

    def invoke(self, prompt):
        class R:
            content = self._content
        return R()


def test_llm_pass_groups_terms_into_topics():
    payload = (
        '{"topics": [{"name": "Role & duties", "terms": ["intake", "qualification"],'
        ' "questions": ["What does a legal intake specialist do?"]},'
        ' {"name": "Compensation", "terms": ["salary"], "questions": ["What is the salary?"]}],'
        ' "questions": ["What does a legal intake specialist do?", "What is the salary?"]}'
    )
    topics, questions, ai, reason = group_topics(TARGETS, PAA, "legal intake specialist", llm=FakeLLM(payload))
    assert ai is True and reason is None
    assert [t.name for t in topics] == ["Role & duties", "Compensation"]
    assert "What is the salary?" in questions


def test_llm_json_inside_prose_still_parses():
    payload = 'Here you go:\n```json\n{"topics": [{"name": "A", "terms": ["intake"], "questions": []}], "questions": []}\n```'
    topics, _, ai, _ = group_topics(TARGETS, PAA, "kw", llm=FakeLLM(payload))
    assert ai is True
    assert topics[0].name == "A"


def test_llm_failure_falls_back_honestly():
    class BoomLLM:
        def invoke(self, prompt):
            raise RuntimeError("upstream 502")

    topics, questions, ai, reason = group_topics(TARGETS, PAA, "kw", llm=BoomLLM())
    assert ai is False
    assert "502" in (reason or "")
    assert len(topics) == 1 and topics[0].name == "Key terms (statistical)"
    assert topics[0].terms == ["intake", "salary", "qualification"]
    assert questions == PAA          # PAA questions still usable without the LLM

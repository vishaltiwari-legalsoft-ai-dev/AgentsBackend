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

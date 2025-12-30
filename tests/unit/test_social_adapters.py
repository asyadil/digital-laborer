import types

from src.platforms.tiktok_adapter import TikTokAdapter
from src.platforms.instagram_adapter import InstagramAdapter
from src.platforms.facebook_adapter import FacebookAdapter
from src.platforms.base_adapter import RateLimitError


def _stub_config():
    return types.SimpleNamespace(platforms=types.SimpleNamespace(tiktok={}, instagram={}, facebook={}))


def test_tiktok_adapter_happy_path():
    adapter = TikTokAdapter(config=_stub_config())
    login = adapter.login({"username": "tester"})
    assert login.success
    res = adapter.post_comment(target_id="abc", content="hi", account={"username": "tester"})
    assert res.success
    assert "comment_id" in res.data


def test_instagram_adapter_rate_limit(monkeypatch):
    adapter = InstagramAdapter(config=_stub_config())
    # Force rate limit
    monkeypatch.setattr(adapter.rate_limiter, "try_acquire", lambda tokens=1: False)
    monkeypatch.setattr(adapter.daily_limiter, "try_acquire", lambda tokens=1: False)
    try:
        adapter.post_comment(target_id="abc", content="hi", account={"username": "tester"})
        assert False, "expected RateLimitError"
    except RateLimitError:
        assert True


def test_facebook_adapter_find_targets():
    adapter = FacebookAdapter(config=_stub_config())
    res = adapter.find_target_posts(location="page123", limit=2)
    assert res.success
    assert len(res.data["items"]) >= 1

from types import SimpleNamespace

import pytest

from src.platforms.reddit_adapter import RedditAdapter
from src.utils.config_loader import AppConfig


class FakeComment:
    def __init__(self, comment_id="c1"):
        self.id = comment_id
        self.permalink = f"/r/test/comments/p1/title/{comment_id}/"
        self.score = 3
        self.replies = []
        self.created_utc = 1

    def refresh(self):
        return None


class FakeSubmission:
    def __init__(self, sid="p1"):
        self.id = sid
        self.title = "Title"
        self.url = "https://example.com"
        self.permalink = f"/r/test/comments/{sid}/title/"
        self.score = 10
        self.created_utc = 1
        self.num_comments = 2

    def reply(self, content):
        assert content
        return FakeComment("c123")


class FakeSubreddit:
    def hot(self, limit=10):
        return [FakeSubmission("p1"), FakeSubmission("p2")][:limit]


class FakeUser:
    def me(self):
        return SimpleNamespace(name="me", created_utc=1, link_karma=10, comment_karma=20)


class FakeReddit:
    def __init__(self):
        self.user = FakeUser()

    def subreddit(self, name):
        assert name
        return FakeSubreddit()

    def submission(self, id):
        return FakeSubmission(id)

    def comment(self, id):
        return FakeComment(id)


def test_reddit_adapter_login_and_find_posts(monkeypatch):
    cfg = AppConfig(telegram={"bot_token": "x", "user_chat_id": "1"})
    adapter = RedditAdapter(cfg, credentials=[{}])

    monkeypatch.setattr(adapter, "_create_client", lambda account: FakeReddit())

    res = adapter.login({})
    assert res.success is True
    posts = adapter.find_target_posts("test", limit=2)
    assert posts.success is True
    assert len(posts.data["items"]) == 2


def test_reddit_adapter_post_comment(monkeypatch):
    cfg = AppConfig(telegram={"bot_token": "x", "user_chat_id": "1"})
    adapter = RedditAdapter(cfg, credentials=[{}])
    monkeypatch.setattr(adapter, "_create_client", lambda account: FakeReddit())
    assert adapter.login({}).success is True

    out = adapter.post_comment("p1", "hello", {})
    assert out.success is True
    assert "comment_url" in out.data


def test_reddit_adapter_metrics(monkeypatch):
    cfg = AppConfig(telegram={"bot_token": "x", "user_chat_id": "1"})
    adapter = RedditAdapter(cfg, credentials=[{}])
    monkeypatch.setattr(adapter, "_create_client", lambda account: FakeReddit())
    assert adapter.login({}).success is True

    metrics = adapter.get_comment_metrics("https://www.reddit.com/r/test/comments/p1/title/c123/")
    assert metrics.success is True
    assert "score" in metrics.data


def test_reddit_adapter_requires_login():
    cfg = AppConfig(telegram={"bot_token": "x", "user_chat_id": "1"})
    adapter = RedditAdapter(cfg, credentials=[{}])

    res = adapter.find_target_posts("test")
    assert res.success is False

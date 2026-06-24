"""Poller orchestration test with Google + Claude + email all mocked."""

import smtplib

import pytest

from jolly_roger.db import (
    STATUS_DRAFTED,
    STATUS_POSTED,
    STATUS_SKIPPED,
    Database,
    Review,
)
from jolly_roger.drafting import DraftResult
from jolly_roger.google_client import RemoteReview
from jolly_roger import poller


class FakeGoogle:
    def __init__(self, reviews):
        self._reviews = reviews
        self.posted = {}

    def iter_reviews(self):
        yield from self._reviews

    def reply_to_review(self, review_id, comment):
        self.posted[review_id] = comment


class FakeDrafter:
    def __init__(self):
        self.seen_examples = None

    def draft(self, review: Review, examples=None) -> DraftResult:
        self.seen_examples = examples
        needs_human = review.star_rating <= 2
        return DraftResult(
            draft_reply=f"Thanks {review.author}!",
            needs_human=needs_human,
            flag_reason="low rating" if needs_human else "",
        )


class FakeConfig:
    bad_review_max_stars = 2
    example_count = 5


def remote(review_id, stars, has_reply=False):
    return RemoteReview(
        review_id=review_id,
        author="Pat",
        star_rating=stars,
        comment="text",
        created_at="2026-06-20T00:00:00Z",
        has_reply=has_reply,
    )


def test_run_poll_routes_good_and_bad(tmp_path, monkeypatch):
    routed = {}

    def fake_digest(cfg, reviews):
        routed["good"] = [r.review_id for r in reviews]
        return True

    def fake_alert(cfg, reviews):
        routed["bad"] = [r.review_id for r in reviews]
        return True

    monkeypatch.setattr(poller, "send_digest", fake_digest)
    monkeypatch.setattr(poller, "send_manager_alert", fake_alert)

    db = Database(str(tmp_path / "t.db"))
    google = FakeGoogle([remote("a", 5), remote("b", 1)])
    result = poller.run_poll(
        config=FakeConfig(), db=db, google=google, drafter=FakeDrafter()
    )

    assert result.new_reviews == 2
    assert result.flagged == 1
    assert result.digest_sent is True
    assert result.manager_alerted is True
    # 5-star → owner digest; 1-star → manager.
    assert routed["good"] == ["a"]
    assert routed["bad"] == ["b"]
    assert db.get("a").status == STATUS_DRAFTED


def test_run_poll_passes_examples_to_drafter(tmp_path, monkeypatch):
    monkeypatch.setattr(poller, "send_digest", lambda cfg, reviews: True)
    monkeypatch.setattr(poller, "send_manager_alert", lambda cfg, reviews: True)

    db = Database(str(tmp_path / "t.db"))
    # Seed a posted reply so it becomes a learning example.
    db.insert_new(Review("old", "Lee", 5, "Loved it", "2026-06-01T00:00:00Z"))
    db.mark_posted("old", "Thanks Lee, come back soon!")

    drafter = FakeDrafter()
    google = FakeGoogle([remote("a", 5)])
    poller.run_poll(config=FakeConfig(), db=db, google=google, drafter=drafter)

    assert drafter.seen_examples is not None
    assert any(ex.final_reply == "Thanks Lee, come back soon!" for ex in drafter.seen_examples)


def test_run_poll_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(poller, "send_digest", lambda cfg, reviews: bool(reviews))
    monkeypatch.setattr(poller, "send_manager_alert", lambda cfg, reviews: bool(reviews))
    db = Database(str(tmp_path / "t.db"))
    google = FakeGoogle([remote("a", 5)])

    poller.run_poll(config=FakeConfig(), db=db, google=google, drafter=FakeDrafter())
    second = poller.run_poll(config=FakeConfig(), db=db, google=google, drafter=FakeDrafter())
    assert second.new_reviews == 0  # already seen


def test_run_poll_skips_already_replied(tmp_path, monkeypatch):
    routed = {"good": [], "bad": []}

    def fake_digest(cfg, reviews):
        routed["good"] = [r.review_id for r in reviews]
        return True

    def fake_alert(cfg, reviews):
        routed["bad"] = [r.review_id for r in reviews]
        return True

    monkeypatch.setattr(poller, "send_digest", fake_digest)
    monkeypatch.setattr(poller, "send_manager_alert", fake_alert)

    db = Database(str(tmp_path / "t.db"))
    google = FakeGoogle([remote("a", 5), remote("b", 5, has_reply=True)])
    result = poller.run_poll(
        config=FakeConfig(), db=db, google=google, drafter=FakeDrafter()
    )

    assert result.new_reviews == 1
    assert result.skipped == 1
    assert db.get("b").status == STATUS_SKIPPED
    assert db.get("b").draft_reply is None  # never drafted
    assert routed["good"] == ["a"]  # b not emailed


def test_post_reply_missing_remote(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.insert_new(Review("a", "Pat", 5, "text", "2026-06-20T00:00:00Z"))
    google = FakeGoogle([remote("other", 5)])  # "a" is not present remotely

    with pytest.raises(RuntimeError, match="not found"):
        poller.post_reply(db, google, "a", "our reply")
    assert "a" not in google.posted


def test_email_send_failure_retries_next_poll(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky_digest(cfg, reviews):
        calls["n"] += 1
        if calls["n"] == 1:
            raise smtplib.SMTPException("smtp down")
        return True

    monkeypatch.setattr(poller, "send_digest", flaky_digest)
    monkeypatch.setattr(poller, "send_manager_alert", lambda cfg, reviews: True)

    db = Database(str(tmp_path / "t.db"))
    google = FakeGoogle([remote("a", 5)])

    # First poll: drafting succeeds but the digest send fails → not notified.
    r1 = poller.run_poll(config=FakeConfig(), db=db, google=google, drafter=FakeDrafter())
    assert r1.digest_sent is False
    assert db.get("a").status == STATUS_DRAFTED
    assert db.get("a").notified is False

    # Second poll: no new reviews, but the un-notified draft is retried and sent.
    r2 = poller.run_poll(config=FakeConfig(), db=db, google=google, drafter=FakeDrafter())
    assert r2.new_reviews == 0
    assert r2.digest_sent is True
    assert db.get("a").notified is True


def test_post_reply_refuses_to_overwrite(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.insert_new(Review("a", "Pat", 5, "text", "2026-06-20T00:00:00Z"))
    google = FakeGoogle([remote("a", 5, has_reply=True)])

    try:
        poller.post_reply(db, google, "a", "our reply")
        assert False, "expected refusal"
    except RuntimeError as exc:
        assert "already exists" in str(exc)
    assert "a" not in google.posted


def test_post_reply_posts_and_marks(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.insert_new(Review("a", "Pat", 5, "text", "2026-06-20T00:00:00Z"))
    google = FakeGoogle([remote("a", 5, has_reply=False)])

    poller.post_reply(db, google, "a", "our reply")
    assert google.posted["a"] == "our reply"
    assert db.get("a").status == STATUS_POSTED

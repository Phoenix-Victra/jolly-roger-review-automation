"""Poller orchestration test with Google + Claude + email all mocked."""

from jolly_roger.db import STATUS_DRAFTED, STATUS_POSTED, Database, Review
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
    def draft(self, review: Review) -> DraftResult:
        needs_human = review.star_rating <= 2
        return DraftResult(
            draft_reply=f"Thanks {review.author}!",
            needs_human=needs_human,
            flag_reason="low rating" if needs_human else "",
        )


def remote(review_id, stars, has_reply=False):
    return RemoteReview(
        review_id=review_id,
        author="Pat",
        star_rating=stars,
        comment="text",
        created_at="2026-06-20T00:00:00Z",
        has_reply=has_reply,
    )


def test_run_poll_drafts_and_flags(tmp_path, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        poller, "send_digest", lambda cfg, reviews: sent.setdefault("n", len(reviews)) or True
    )

    db = Database(str(tmp_path / "t.db"))
    google = FakeGoogle([remote("a", 5), remote("b", 1)])
    result = poller.run_poll(config=None, db=db, google=google, drafter=FakeDrafter())

    assert result.new_reviews == 2
    assert result.flagged == 1
    assert sent["n"] == 2
    assert db.get("a").status == STATUS_DRAFTED
    assert db.get("b").needs_human is True


def test_run_poll_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(poller, "send_digest", lambda cfg, reviews: bool(reviews))
    db = Database(str(tmp_path / "t.db"))
    google = FakeGoogle([remote("a", 5)])

    poller.run_poll(config=None, db=db, google=google, drafter=FakeDrafter())
    second = poller.run_poll(config=None, db=db, google=google, drafter=FakeDrafter())
    assert second.new_reviews == 0  # already seen


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

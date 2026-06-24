"""Dashboard auth, CSRF, and the approve/reject workflow."""

from __future__ import annotations

import pytest

from jolly_roger.dashboard.app import create_app
from jolly_roger.db import STATUS_DRAFTED, STATUS_POSTED, STATUS_REJECTED, Database, Review
from jolly_roger.google_client import RemoteReview
from tests.conftest import make_config


class FakeGoogle:
    """Stand-in for GoogleBusinessClient used by the dashboard."""

    def __init__(self, reviews):
        self._reviews = reviews
        self.posted = {}

    def iter_reviews(self):
        yield from self._reviews

    def reply_to_review(self, review_id, comment):
        self.posted[review_id] = comment


def remote(review_id, stars=5, has_reply=False):
    return RemoteReview(review_id, "Pat", stars, "text", "2026-06-20T00:00:00Z", has_reply)


def build(tmp_path, reviews=None, **cfg):
    config = make_config(tmp_path, **cfg)
    db = Database(config.database_path)
    google = FakeGoogle(reviews or [])
    app = create_app(config, google=google)
    app.config["TESTING"] = True
    return app, db, google


def login(client):
    """Log in and return the CSRF token tied to the session."""
    client.get("/login")  # renders the form → seeds session csrf_token
    with client.session_transaction() as s:
        token = s["csrf_token"]
    resp = client.post("/login", data={"password": "hunter2", "csrf_token": token})
    assert resp.status_code == 302
    return token


def seed_drafted(db, review_id="r1"):
    db.insert_new(Review(review_id, "Pat", 5, "text", "2026-06-20T00:00:00Z"))
    db.save_draft(review_id, "Thanks Pat!", needs_human=False, flag_reason=None)


def test_index_requires_login(tmp_path):
    app, _, _ = build(tmp_path)
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_approve_requires_login(tmp_path):
    app, db, _ = build(tmp_path)
    seed_drafted(db)
    client = app.test_client()
    resp = client.post("/review/r1/approve", data={"reply": "hi"})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert db.get("r1").status == STATUS_DRAFTED  # untouched


def test_wrong_password_is_rejected(tmp_path):
    app, _, _ = build(tmp_path)
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as s:
        token = s["csrf_token"]
    resp = client.post("/login", data={"password": "nope", "csrf_token": token})
    assert resp.status_code == 401


def test_csrf_token_required_on_post(tmp_path):
    app, db, _ = build(tmp_path, reviews=[remote("r1")])
    seed_drafted(db)
    client = app.test_client()
    login(client)
    # No csrf_token in the form → rejected.
    resp = client.post("/review/r1/reject", data={})
    assert resp.status_code == 400
    assert db.get("r1").status == STATUS_DRAFTED


def test_approve_success_posts_and_marks(tmp_path):
    app, db, google = build(tmp_path, reviews=[remote("r1", has_reply=False)])
    seed_drafted(db)
    client = app.test_client()
    token = login(client)

    resp = client.post(
        "/review/r1/approve", data={"reply": "Our reply", "csrf_token": token}
    )
    assert resp.status_code == 302
    assert google.posted["r1"] == "Our reply"
    assert db.get("r1").status == STATUS_POSTED
    assert db.get("r1").final_reply == "Our reply"


def test_approve_failure_keeps_drafted(tmp_path):
    # Remote already has a reply → post_reply raises → stays drafted.
    app, db, google = build(tmp_path, reviews=[remote("r1", has_reply=True)])
    seed_drafted(db)
    client = app.test_client()
    token = login(client)

    resp = client.post(
        "/review/r1/approve", data={"reply": "Our reply", "csrf_token": token}
    )
    assert resp.status_code == 502
    assert "already exists" in resp.get_data(as_text=True)
    assert db.get("r1").status == STATUS_DRAFTED
    assert "r1" not in google.posted


def test_reject_flow(tmp_path):
    app, db, _ = build(tmp_path)
    seed_drafted(db)
    client = app.test_client()
    token = login(client)

    resp = client.post("/review/r1/reject", data={"csrf_token": token})
    assert resp.status_code == 302
    assert db.get("r1").status == STATUS_REJECTED


def test_missing_secret_key_raises(tmp_path):
    with pytest.raises(RuntimeError, match="FLASK_SECRET_KEY"):
        create_app(make_config(tmp_path, flask_secret_key=""))

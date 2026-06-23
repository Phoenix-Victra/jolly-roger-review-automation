"""Approval dashboard.

A reviewer sees drafted replies and can Approve (post as-is), Edit then approve
(post their version), or Reject (no reply). On approval the reply is posted to
Google via the Business Profile API. Nothing posts automatically.

Run:  flask --app jolly_roger.dashboard.app run
"""

from __future__ import annotations

from flask import Flask, abort, redirect, render_template, request, url_for

from ..config import Config
from ..db import (
    STATUS_APPROVED,
    STATUS_DRAFTED,
    STATUS_NEW,
    STATUS_POSTED,
    STATUS_REJECTED,
    Database,
)
from ..google_client import GoogleBusinessClient
from ..poller import post_reply


def create_app(config: Config | None = None) -> Flask:
    config = config or Config.from_env()
    app = Flask(__name__)
    db = Database(config.database_path)

    # The Google client opens an authorized session; build it lazily so the
    # dashboard still loads (for review/edit) even before OAuth is set up.
    _google: dict[str, GoogleBusinessClient] = {}

    def google() -> GoogleBusinessClient:
        if "client" not in _google:
            _google["client"] = GoogleBusinessClient(config)
        return _google["client"]

    @app.route("/")
    def index():
        pending = db.list_by_status(STATUS_NEW, STATUS_DRAFTED)
        done = db.list_by_status(STATUS_POSTED, STATUS_REJECTED)
        return render_template("index.html", pending=pending, done=done)

    @app.route("/review/<review_id>")
    def review_detail(review_id: str):
        review = db.get(review_id)
        if not review:
            abort(404)
        return render_template("review.html", review=review)

    @app.route("/review/<review_id>/approve", methods=["POST"])
    def approve(review_id: str):
        review = db.get(review_id)
        if not review:
            abort(404)
        # "Edit then approve" sends an edited body; plain approve sends the draft.
        final_reply = request.form.get("reply", "").strip() or (
            review.draft_reply or ""
        )
        if not final_reply:
            abort(400, "Cannot post an empty reply.")
        db.set_status(review_id, STATUS_APPROVED)
        try:
            post_reply(db, google(), review_id, final_reply)
        except Exception as exc:  # surface posting errors to the reviewer
            db.set_status(review_id, STATUS_DRAFTED)
            return render_template(
                "review.html", review=db.get(review_id), error=str(exc)
            ), 502
        return redirect(url_for("index"))

    @app.route("/review/<review_id>/reject", methods=["POST"])
    def reject(review_id: str):
        if not db.get(review_id):
            abort(404)
        db.set_status(review_id, STATUS_REJECTED)
        return redirect(url_for("index"))

    return app


# Allow `flask --app jolly_roger.dashboard.app run`
app = None
try:
    app = create_app()
except Exception:  # pragma: no cover - lets imports succeed without a full env
    pass

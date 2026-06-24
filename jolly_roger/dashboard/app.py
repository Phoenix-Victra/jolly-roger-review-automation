"""Approval dashboard.

A reviewer logs in, sees drafted replies, and can Approve (post as-is), Edit
then approve (post their version), or Reject (no reply). On approval the reply
is posted to Google via the Business Profile API. **Nothing posts
automatically**, and the review is only marked posted after Google confirms.

Auth is intentionally simple for a small private app: a single shared password
plus a signed session cookie, and a per-session CSRF token on every form.

Run:  flask --app jolly_roger.dashboard.app run
"""

from __future__ import annotations

import hmac
import secrets
from typing import Optional

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ..config import Config
from ..db import (
    STATUS_DRAFTED,
    STATUS_NEW,
    STATUS_POSTED,
    STATUS_REJECTED,
    STATUS_SKIPPED,
    Database,
)
from ..google_client import GoogleBusinessClient
from ..poller import post_reply


def create_app(config: Optional[Config] = None, google=None) -> Flask:
    """Build the Flask app.

    ``google`` may be a pre-built client (used by tests); otherwise one is
    created lazily on first use so the dashboard still loads before OAuth runs.
    """
    config = config or Config.from_env()

    if not config.flask_secret_key:
        raise RuntimeError(
            "FLASK_SECRET_KEY is required to run the dashboard. Set it in .env "
            "(any long random string)."
        )
    if not config.dashboard_password:
        raise RuntimeError(
            "DASHBOARD_PASSWORD is required to run the dashboard. Set it in .env."
        )

    app = Flask(__name__)
    app.secret_key = config.flask_secret_key
    db = Database(config.database_path)

    _google: dict[str, GoogleBusinessClient] = {}
    if google is not None:
        _google["client"] = google

    def google_client() -> GoogleBusinessClient:
        if "client" not in _google:
            _google["client"] = GoogleBusinessClient(config)
        return _google["client"]

    def _require_csrf() -> None:
        token = session.get("csrf_token", "")
        form_token = request.form.get("csrf_token", "")
        if not token or not hmac.compare_digest(token, form_token):
            abort(400, "Invalid or missing CSRF token.")

    @app.context_processor
    def _inject_csrf() -> dict:
        # Ensure a CSRF token exists whenever a template is rendered.
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf_token"]}

    @app.before_request
    def _guard() -> Optional[object]:
        # Static assets and the login page are open; everything else needs auth.
        if request.endpoint in ("static", "login"):
            return None
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        if request.method == "POST":
            _require_csrf()
        return None

    # --- auth ---------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            _require_csrf()
            supplied = request.form.get("password", "")
            if hmac.compare_digest(supplied, config.dashboard_password):
                session["authenticated"] = True
                return redirect(url_for("index"))
            error = "Incorrect password."
        return render_template("login.html", error=error), (
            401 if error else 200
        )

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # --- review workflow ----------------------------------------------------

    @app.route("/")
    def index():
        pending = db.list_by_status(STATUS_NEW, STATUS_DRAFTED)
        done = db.list_by_status(STATUS_POSTED, STATUS_REJECTED, STATUS_SKIPPED)
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

        # Post FIRST. Only post_reply marks the review posted, and only on
        # success — so a posting failure leaves it in `drafted`.
        try:
            post_reply(db, google_client(), review_id, final_reply)
        except Exception as exc:  # noqa: BLE001 - surface the error to the user
            return (
                render_template(
                    "review.html", review=db.get(review_id), error=str(exc)
                ),
                502,
            )
        return redirect(url_for("index"))

    @app.route("/review/<review_id>/reject", methods=["POST"])
    def reject(review_id: str):
        if not db.get(review_id):
            abort(404)
        db.set_status(review_id, STATUS_REJECTED)
        return redirect(url_for("index"))

    return app

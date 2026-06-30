"""Google Business Profile client: OAuth, listing reviews, posting replies.

The review *list* and *reply* methods live on the older v4 surface
(``mybusiness.googleapis.com/v4``), which is not in the standard discovery
documents, so we call it with an authorized REST session. Account and location
IDs come from the Account Management and Business Information APIs.

Access to these endpoints is gated: the business owner must grant your Google
Cloud project access to the Business Profile, and Google must approve your
Business Profile API access request. See the README, Phase 0.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Iterator, Optional

import requests
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import GBP_SCOPE, Config
from .db import Review

log = logging.getLogger("jolly_roger.google_client")

_V4_BASE = "https://mybusiness.googleapis.com/v4"
_ACCOUNTS_BASE = "https://mybusinessaccountmanagement.googleapis.com/v1"
_INFO_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"

# Status codes worth retrying — transient server/rate-limit conditions. A 4xx
# like 403/404 is a real error and is NOT retried (it won't fix itself).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_BACKOFFS = (2, 4, 8)  # seconds; len == number of retries after the first try


def _request_with_retry(session, method, url, *, _sleep=time.sleep, **kwargs):
    """Make an HTTP request, retrying transient failures with backoff.

    Retries on connection errors and 429/5xx up to ``len(_BACKOFFS)`` times,
    then raises. Non-retryable HTTP errors raise immediately via
    ``raise_for_status``.
    """
    for attempt in range(len(_BACKOFFS) + 1):
        try:
            resp = session.request(method, url, **kwargs)
        except requests.exceptions.RequestException as exc:
            if attempt == len(_BACKOFFS):
                raise
            log.warning("Google request error (%s); retrying in %ss",
                        exc, _BACKOFFS[attempt])
            _sleep(_BACKOFFS[attempt])
            continue
        if resp.status_code in _RETRYABLE_STATUS and attempt < len(_BACKOFFS):
            log.warning("Google returned %s; retrying in %ss",
                        resp.status_code, _BACKOFFS[attempt])
            _sleep(_BACKOFFS[attempt])
            continue
        resp.raise_for_status()
        return resp

# Google returns star ratings as an enum string.
_STAR_MAP = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


@dataclass
class RemoteReview:
    """A review as it exists on Google right now."""

    review_id: str
    author: str
    star_rating: int
    comment: str
    created_at: str
    has_reply: bool

    def to_db_review(self) -> Review:
        return Review(
            review_id=self.review_id,
            author=self.author,
            star_rating=self.star_rating,
            comment=self.comment,
            created_at=self.created_at,
        )


def _load_credentials(config: Config) -> Credentials:
    """Load cached OAuth credentials, refreshing or running the flow as needed."""
    creds: Optional[Credentials] = None
    token_path = config.google_token_file

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, [GBP_SCOPE])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.google_client_secrets_file, [GBP_SCOPE]
            )
            # One-time consent. open_browser=False prints the auth URL instead
            # of trying to launch a browser, which fails on headless hosts. For
            # a truly headless box, run this once on a machine with a browser
            # and copy the resulting token.json over (see the README).
            creds = flow.run_local_server(port=0, open_browser=False)
        with open(token_path, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())

    return creds


class GoogleBusinessClient:
    def __init__(self, config: Config):
        self._config = config
        self._session = AuthorizedSession(_load_credentials(config))

    def _req(self, method: str, url: str, **kwargs):
        return _request_with_retry(self._session, method, url, **kwargs)

    # --- account / location discovery (Phase 1) -----------------------------

    def list_accounts(self) -> list[dict]:
        """Return the Business Profile accounts the authorized user can manage."""
        resp = self._req("GET", f"{_ACCOUNTS_BASE}/accounts")
        return resp.json().get("accounts", [])

    def list_locations(self, account_name: str) -> list[dict]:
        """Return locations under an account. ``account_name`` like accounts/123."""
        locations: list[dict] = []
        params = {
            "readMask": "name,title,storefrontAddress",
            "pageSize": 100,
        }
        url = f"{_INFO_BASE}/{account_name}/locations"
        while True:
            resp = self._req("GET", url, params=params)
            body = resp.json()
            locations.extend(body.get("locations", []))
            token = body.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
        return locations

    # --- reviews (Phase 1 read path) -----------------------------------------

    def _review_parent(self) -> str:
        account = self._config.gbp_account_name
        location = self._config.gbp_location_name
        if not account or not location:
            raise RuntimeError(
                "GBP_ACCOUNT_NAME and GBP_LOCATION_NAME must be set. Run "
                "`python -m jolly_roger.cli discover` once access is granted."
            )
        # v4 expects accounts/{a}/locations/{l}. Locations from the Business
        # Information API come back as bare "locations/{l}".
        loc_id = location.split("/")[-1]
        return f"{account}/locations/{loc_id}"

    def iter_reviews(self) -> Iterator[RemoteReview]:
        """Yield every review for the configured location, newest first."""
        parent = self._review_parent()
        url = f"{_V4_BASE}/{parent}/reviews"
        params = {"pageSize": 50}
        while True:
            resp = self._req("GET", url, params=params)
            body = resp.json()
            for raw in body.get("reviews", []):
                yield self._parse_review(raw)
            token = body.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token

    def fetch_review_summary(self) -> Optional[tuple[float, int]]:
        """Return Google's (averageRating, totalReviewCount) for the location.

        These come back at the top level of the v4 reviews list response.
        Best-effort: returns None if unavailable so it never blocks a poll.
        """
        parent = self._review_parent()
        resp = self._req("GET", f"{_V4_BASE}/{parent}/reviews", params={"pageSize": 1})
        body = resp.json()
        avg = body.get("averageRating")
        total = body.get("totalReviewCount")
        if avg is None or total is None:
            return None
        return float(avg), int(total)

    @staticmethod
    def _parse_review(raw: dict) -> RemoteReview:
        reviewer = raw.get("reviewer", {})
        return RemoteReview(
            review_id=raw["reviewId"],
            author=reviewer.get("displayName", "Anonymous"),
            star_rating=_STAR_MAP.get(raw.get("starRating", ""), 0),
            comment=raw.get("comment", ""),
            created_at=raw.get("createTime", ""),
            has_reply="reviewReply" in raw,
        )

    # --- posting replies (Phase 4) -------------------------------------------

    def reply_to_review(self, review_id: str, comment: str) -> None:
        """Post (or update) the business reply to a review.

        Uses PUT on the review's reply sub-resource. Callers should first check
        that no manual reply already exists (see ``RemoteReview.has_reply``).
        """
        parent = self._review_parent()
        url = f"{_V4_BASE}/{parent}/reviews/{review_id}/reply"
        self._req("PUT", url, json={"comment": comment})

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

import os
from dataclasses import dataclass
from typing import Iterator, Optional

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import GBP_SCOPE, Config
from .db import Review

_V4_BASE = "https://mybusiness.googleapis.com/v4"
_ACCOUNTS_BASE = "https://mybusinessaccountmanagement.googleapis.com/v1"
_INFO_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"

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

    # --- account / location discovery (Phase 1) -----------------------------

    def list_accounts(self) -> list[dict]:
        """Return the Business Profile accounts the authorized user can manage."""
        resp = self._session.get(f"{_ACCOUNTS_BASE}/accounts")
        resp.raise_for_status()
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
            resp = self._session.get(url, params=params)
            resp.raise_for_status()
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
            resp = self._session.get(url, params=params)
            resp.raise_for_status()
            body = resp.json()
            for raw in body.get("reviews", []):
                yield self._parse_review(raw)
            token = body.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token

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
        resp = self._session.put(url, json={"comment": comment})
        resp.raise_for_status()

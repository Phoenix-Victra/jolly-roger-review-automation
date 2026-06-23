"""The poller: pull new reviews, dedupe, draft replies, send the digest.

Run on a schedule (cron). Idempotent: reviews already in the DB are skipped, so
running it more often than needed is harmless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config
from .db import STATUS_DRAFTED, Database, Review
from .drafting import Drafter
from .email_digest import send_digest
from .google_client import GoogleBusinessClient

log = logging.getLogger("jolly_roger.poller")


@dataclass
class PollResult:
    new_reviews: int
    drafted: int
    flagged: int
    digest_sent: bool


def run_poll(
    config: Config,
    db: Database,
    google: GoogleBusinessClient,
    drafter: Drafter,
) -> PollResult:
    """One full poll cycle. Returns counts for logging/monitoring."""
    new_reviews: list[Review] = []

    # 1. Pull reviews; insert ones we haven't seen as `new`.
    for remote in google.iter_reviews():
        if db.has_review(remote.review_id):
            continue
        review = remote.to_db_review()
        db.insert_new(review)
        new_reviews.append(review)
        log.info("new review %s (%d★) by %s",
                 review.review_id, review.star_rating, review.author)

    # 2. Draft a reply for each new review and flag sensitive ones.
    flagged = 0
    for review in new_reviews:
        result = drafter.draft(review)
        db.save_draft(
            review.review_id,
            result.draft_reply,
            result.needs_human,
            result.flag_reason or None,
        )
        # Reflect the draft back onto the in-memory object for the digest.
        review.draft_reply = result.draft_reply
        review.needs_human = result.needs_human
        review.flag_reason = result.flag_reason
        review.status = STATUS_DRAFTED
        if result.needs_human:
            flagged += 1

    # 3. Send the digest (only the reviews from this run).
    digest_sent = send_digest(config, new_reviews)

    log.info(
        "poll complete: %d new, %d drafted, %d flagged, digest_sent=%s",
        len(new_reviews), len(new_reviews), flagged, digest_sent,
    )
    return PollResult(
        new_reviews=len(new_reviews),
        drafted=len(new_reviews),
        flagged=flagged,
        digest_sent=digest_sent,
    )


def post_reply(
    db: Database,
    google: GoogleBusinessClient,
    review_id: str,
    final_reply: str,
) -> None:
    """Post an approved reply to Google and mark it posted.

    Guards against overwriting a reply that already exists on Google (e.g. a
    manual one added since the review was pulled).
    """
    # Check current remote state for an existing reply before posting.
    for remote in google.iter_reviews():
        if remote.review_id == review_id:
            if remote.has_reply:
                raise RuntimeError(
                    "A reply already exists on Google for this review; "
                    "not overwriting it."
                )
            break

    google.reply_to_review(review_id, final_reply)
    db.mark_posted(review_id, final_reply)
    log.info("posted reply for review %s", review_id)

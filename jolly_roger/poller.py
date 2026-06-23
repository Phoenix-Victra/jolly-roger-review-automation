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
from .email_digest import send_digest, send_manager_alert
from .google_client import GoogleBusinessClient

log = logging.getLogger("jolly_roger.poller")


@dataclass
class PollResult:
    new_reviews: int
    drafted: int
    flagged: int
    digest_sent: bool
    manager_alerted: bool


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

    # Past approved replies become few-shot examples so good-review drafts
    # match the voice we've already established.
    examples = db.list_examples(config.example_count) if config else []

    # 2. Draft a reply for each new review and flag sensitive ones. Split into
    #    "good" (digest for approval) and "bad" (routed to the manager).
    flagged = 0
    good: list[Review] = []
    bad: list[Review] = []
    for review in new_reviews:
        result = drafter.draft(review, examples)
        db.save_draft(
            review.review_id,
            result.draft_reply,
            result.needs_human,
            result.flag_reason or None,
        )
        # Reflect the draft back onto the in-memory object for the emails.
        review.draft_reply = result.draft_reply
        review.needs_human = result.needs_human
        review.flag_reason = result.flag_reason
        review.status = STATUS_DRAFTED
        if result.needs_human:
            flagged += 1

        max_stars = config.bad_review_max_stars if config else 2
        if review.star_rating <= max_stars or review.needs_human:
            bad.append(review)
        else:
            good.append(review)

    # 3. Route: good reviews → owner digest, bad reviews → manager.
    digest_sent = send_digest(config, good) if good else False
    manager_alerted = send_manager_alert(config, bad) if bad else False

    log.info(
        "poll complete: %d new, %d flagged, %d good→digest, %d bad→manager",
        len(new_reviews), flagged, len(good), len(bad),
    )
    return PollResult(
        new_reviews=len(new_reviews),
        drafted=len(new_reviews),
        flagged=flagged,
        digest_sent=digest_sent,
        manager_alerted=manager_alerted,
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

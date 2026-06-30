"""The poller: pull new reviews, dedupe, draft replies, send the digest.

Run on a schedule (cron). Idempotent: reviews already in the DB are skipped, so
running it more often than needed is harmless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from typing import Callable

from .config import Config
from .db import STATUS_DRAFTED, STATUS_SKIPPED, Database, Review
from .drafting import Drafter
from .email_digest import send_digest, send_manager_alert
from .google_client import GoogleBusinessClient

log = logging.getLogger("jolly_roger.poller")


@dataclass
class PollResult:
    new_reviews: int
    drafted: int
    flagged: int
    skipped: int
    digest_sent: bool
    manager_alerted: bool


def _is_bad(review: Review, max_stars: int) -> bool:
    return review.star_rating <= max_stars or review.needs_human


def _notify(
    db: Database,
    send_fn: Callable[[Config, list[Review]], bool],
    config: Config,
    reviews: list[Review],
) -> bool:
    """Email a batch and mark it notified only if sending actually succeeds.

    On failure the reviews stay ``notified = 0`` so a later poll retries them.
    """
    if not reviews:
        return False
    try:
        send_fn(config, reviews)
    except Exception as exc:  # noqa: BLE001 - a failed send must not crash the poll
        log.error("email send failed (%s); will retry next poll", exc)
        return False
    db.mark_notified([r.review_id for r in reviews])
    return True


def run_poll(
    config: Config,
    db: Database,
    google: GoogleBusinessClient,
    drafter: Drafter,
) -> PollResult:
    """One full poll cycle. Returns counts for logging/monitoring."""
    new_reviews: list[Review] = []
    skipped = 0

    # 1. Pull reviews. Insert ones we haven't seen. If Google already shows a
    #    reply (e.g. a manual one), record it as `skipped` and never draft or
    #    email it — we don't touch reviews that already have a response.
    for remote in google.iter_reviews():
        if db.has_review(remote.review_id):
            continue
        db.insert_new(remote.to_db_review())
        if remote.has_reply:
            db.set_status(remote.review_id, STATUS_SKIPPED)
            skipped += 1
            log.info("skipping %s — already has a reply on Google", remote.review_id)
            continue
        new_reviews.append(remote.to_db_review())
        log.info("new review %s (%d★)", remote.review_id, remote.star_rating)

    # Cache Google's official overall rating + review count for the dashboard.
    try:
        summary = google.fetch_review_summary()
        if summary:
            avg, total = summary
            db.set_meta("google_average_rating", str(avg))
            db.set_meta("google_total_reviews", str(total))
    except Exception as exc:  # noqa: BLE001 - summary is best-effort
        log.warning("could not fetch review summary: %s", exc)

    # Past approved replies become few-shot examples so good-review drafts
    # match the voice we've already established.
    examples = db.list_examples(config.example_count) if config else []

    # 2. Draft a reply for each new review and flag sensitive ones. Drafting
    #    never raises: on failure it returns a needs_human result.
    flagged = 0
    for review in new_reviews:
        result = drafter.draft(review, examples)
        db.save_draft(
            review.review_id,
            result.draft_reply,
            result.needs_human,
            result.flag_reason or None,
        )
        if result.needs_human:
            flagged += 1

    # 3. Notify. We pull from the DB (not just this run) so any draft whose
    #    email previously failed gets retried. Good → owner digest, bad/
    #    sensitive → manager. Each batch is marked notified only on success.
    max_stars = config.bad_review_max_stars if config else 2
    pending = db.list_unnotified()
    good = [r for r in pending if not _is_bad(r, max_stars)]
    bad = [r for r in pending if _is_bad(r, max_stars)]

    digest_sent = _notify(db, send_digest, config, good)
    manager_alerted = _notify(db, send_manager_alert, config, bad)

    log.info(
        "poll complete: %d new, %d skipped, %d flagged, %d→digest, %d→manager",
        len(new_reviews), skipped, flagged, len(good), len(bad),
    )
    return PollResult(
        new_reviews=len(new_reviews),
        drafted=len(new_reviews),
        flagged=flagged,
        skipped=skipped,
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
    # Confirm the review still exists remotely and has no reply before posting.
    found = False
    for remote in google.iter_reviews():
        if remote.review_id == review_id:
            found = True
            if remote.has_reply:
                raise RuntimeError(
                    "A reply already exists on Google for this review; "
                    "not overwriting it."
                )
            break
    if not found:
        raise RuntimeError(
            f"Review {review_id} was not found on Google; not posting."
        )

    google.reply_to_review(review_id, final_reply)
    db.mark_posted(review_id, final_reply)
    log.info("posted reply for review %s", review_id)

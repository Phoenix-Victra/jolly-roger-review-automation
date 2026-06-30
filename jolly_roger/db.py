"""SQLite storage for reviews and their reply workflow status.

The ``reviews`` table is the single source of truth that prevents
double-processing and double-replying. ``review_id`` is Google's own review ID
and is the dedupe key.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

# Workflow states a review moves through.
STATUS_NEW = "new"            # just pulled, no draft yet
STATUS_DRAFTED = "drafted"    # Claude wrote a draft, awaiting approval
STATUS_APPROVED = "approved"  # human approved (transient; about to post)
STATUS_POSTED = "posted"      # reply posted to Google
STATUS_REJECTED = "rejected"  # human chose not to reply
STATUS_SKIPPED = "skipped"    # reply already existed on Google, left alone

ALL_STATUSES = {
    STATUS_NEW,
    STATUS_DRAFTED,
    STATUS_APPROVED,
    STATUS_POSTED,
    STATUS_REJECTED,
    STATUS_SKIPPED,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    review_id     TEXT PRIMARY KEY,   -- Google's review ID (dedupe key)
    author        TEXT,
    star_rating   INTEGER,            -- 1..5
    comment       TEXT,
    created_at    TEXT,               -- ISO 8601, from Google
    draft_reply   TEXT,               -- Claude's suggestion
    status        TEXT NOT NULL,      -- see STATUS_* constants
    needs_human   INTEGER DEFAULT 0,  -- 1 if flagged sensitive
    flag_reason   TEXT,               -- why it was flagged
    notified      INTEGER DEFAULT 0,  -- 1 once it's been emailed (digest/manager)
    final_reply   TEXT,               -- what actually got posted
    posted_at     TEXT,
    inserted_at   TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status);

-- Small key/value store for things like Google's official overall rating and
-- review count, cached from the last poll.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class Review:
    review_id: str
    author: str
    star_rating: int
    comment: str
    created_at: str
    draft_reply: Optional[str] = None
    status: str = STATUS_NEW
    needs_human: bool = False
    flag_reason: Optional[str] = None
    notified: bool = False
    final_reply: Optional[str] = None
    posted_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Review":
        return cls(
            review_id=row["review_id"],
            author=row["author"],
            star_rating=row["star_rating"],
            comment=row["comment"],
            created_at=row["created_at"],
            draft_reply=row["draft_reply"],
            status=row["status"],
            needs_human=bool(row["needs_human"]),
            flag_reason=row["flag_reason"],
            notified=bool(row["notified"]),
            final_reply=row["final_reply"],
            posted_at=row["posted_at"],
        )


class Database:
    """Thin wrapper over a SQLite connection with the operations the app needs."""

    def __init__(self, path: str):
        self.path = path
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # timeout + WAL let the cron poller and the dashboard write to the same
        # database concurrently without hitting "database is locked".
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Migrate older databases that predate the `notified` column.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(reviews)")}
            if "notified" not in cols:
                conn.execute(
                    "ALTER TABLE reviews ADD COLUMN notified INTEGER DEFAULT 0"
                )

    # --- reads ---------------------------------------------------------------

    def has_review(self, review_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM reviews WHERE review_id = ?", (review_id,)
            )
            return cur.fetchone() is not None

    def get(self, review_id: str) -> Optional[Review]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
            )
            row = cur.fetchone()
            return Review.from_row(row) if row else None

    def list_by_status(self, *statuses: str) -> list[Review]:
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT * FROM reviews WHERE status IN ({placeholders}) "
                "ORDER BY created_at DESC",
                statuses,
            )
            return [Review.from_row(r) for r in cur.fetchall()]

    def list_examples(self, limit: int = 5) -> list[Review]:
        """Recent reviews whose replies we actually posted.

        These human-approved replies are the few-shot examples the drafter
        learns the established house voice from.
        """
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT * FROM reviews
                 WHERE status = ? AND final_reply IS NOT NULL AND final_reply != ''
                 ORDER BY posted_at DESC
                 LIMIT ?
                """,
                (STATUS_POSTED, limit),
            )
            return [Review.from_row(r) for r in cur.fetchall()]

    def list_unnotified(self) -> list[Review]:
        """Drafted reviews that haven't been emailed yet.

        Lets a later poll re-send anything that was drafted but whose digest /
        manager email failed (e.g. SMTP was down), so a draft is never stranded.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM reviews WHERE status = ? AND notified = 0 "
                "ORDER BY created_at ASC",
                (STATUS_DRAFTED,),
            )
            return [Review.from_row(r) for r in cur.fetchall()]

    def list_all(self) -> list[Review]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM reviews ORDER BY created_at DESC"
            )
            return [Review.from_row(r) for r in cur.fetchall()]

    def list_recent(self, limit: int = 8) -> list[Review]:
        """Most recently created reviews (any status), newest first."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM reviews ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [Review.from_row(r) for r in cur.fetchall()]

    # --- dashboard stats -----------------------------------------------------

    def rating_summary(self) -> tuple[Optional[float], int]:
        """(average_rating, review_count).

        Prefers Google's official numbers cached in `meta` (set during a poll);
        falls back to the average of the reviews we've stored locally.
        """
        official_avg = self.get_meta("google_average_rating")
        official_total = self.get_meta("google_total_reviews")
        if official_avg and official_total:
            try:
                return round(float(official_avg), 1), int(official_total)
            except ValueError:
                pass
        with self._connect() as conn:
            row = conn.execute(
                "SELECT AVG(star_rating) AS avg, COUNT(*) AS n FROM reviews "
                "WHERE star_rating > 0"
            ).fetchone()
        avg = round(row["avg"], 1) if row["avg"] is not None else None
        return avg, row["n"]

    def count_new_today(self) -> int:
        """Reviews first seen by us today (uses our local insert time)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM reviews "
                "WHERE date(inserted_at) = date('now')"
            ).fetchone()
            return row["n"]

    def rating_trend(self, points: int = 14) -> list[float]:
        """Cumulative average rating across reviews in chronological order.

        Returns up to ``points`` samples showing how the running average has
        moved — a smooth line suitable for a sparkline.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT star_rating FROM reviews WHERE star_rating > 0 "
                "ORDER BY created_at ASC"
            ).fetchall()
        ratings = [r["star_rating"] for r in rows]
        if not ratings:
            return []
        running, total = [], 0
        for i, val in enumerate(ratings, start=1):
            total += val
            running.append(round(total / i, 3))
        if len(running) <= points:
            return running
        # Sample evenly down to `points` values, keeping the latest.
        step = (len(running) - 1) / (points - 1)
        return [running[round(i * step)] for i in range(points)]

    # --- meta key/value ------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    # --- writes --------------------------------------------------------------

    def insert_new(self, review: Review) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO reviews
                    (review_id, author, star_rating, comment, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    review.author,
                    review.star_rating,
                    review.comment,
                    review.created_at,
                    STATUS_NEW,
                ),
            )

    def save_draft(
        self,
        review_id: str,
        draft_reply: str,
        needs_human: bool,
        flag_reason: Optional[str],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE reviews
                   SET draft_reply = ?, needs_human = ?, flag_reason = ?,
                       status = ?, updated_at = datetime('now')
                 WHERE review_id = ?
                """,
                (
                    draft_reply,
                    1 if needs_human else 0,
                    flag_reason,
                    STATUS_DRAFTED,
                    review_id,
                ),
            )

    def mark_posted(self, review_id: str, final_reply: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE reviews
                   SET status = ?, final_reply = ?,
                       posted_at = datetime('now'), updated_at = datetime('now')
                 WHERE review_id = ?
                """,
                (STATUS_POSTED, final_reply, review_id),
            )

    def mark_notified(self, review_ids: list[str]) -> None:
        if not review_ids:
            return
        placeholders = ",".join("?" for _ in review_ids)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE reviews SET notified = 1, updated_at = datetime('now') "
                f"WHERE review_id IN ({placeholders})",
                review_ids,
            )

    def set_status(self, review_id: str, status: str) -> None:
        if status not in ALL_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE reviews SET status = ?, updated_at = datetime('now') "
                "WHERE review_id = ?",
                (status, review_id),
            )

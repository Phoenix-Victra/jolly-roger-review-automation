from jolly_roger.db import (
    STATUS_DRAFTED,
    STATUS_NEW,
    STATUS_POSTED,
    Database,
    Review,
)


def make_db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def sample_review(review_id="r1", stars=5):
    return Review(
        review_id=review_id,
        author="Sandy",
        star_rating=stars,
        comment="Best hush puppies on the beach!",
        created_at="2026-06-20T12:00:00Z",
    )


def test_insert_and_dedupe(tmp_path):
    db = make_db(tmp_path)
    db.insert_new(sample_review())
    assert db.has_review("r1")

    # Inserting the same review_id again is a no-op (INSERT OR IGNORE).
    db.insert_new(sample_review())
    assert len(db.list_all()) == 1


def test_draft_then_post_flow(tmp_path):
    db = make_db(tmp_path)
    db.insert_new(sample_review())

    db.save_draft("r1", "Thanks Sandy! See you soon.", needs_human=False, flag_reason=None)
    drafted = db.get("r1")
    assert drafted.status == STATUS_DRAFTED
    assert drafted.draft_reply == "Thanks Sandy! See you soon."
    assert drafted.needs_human is False

    db.mark_posted("r1", "Thanks Sandy! See you soon.")
    posted = db.get("r1")
    assert posted.status == STATUS_POSTED
    assert posted.final_reply == "Thanks Sandy! See you soon."
    assert posted.posted_at is not None


def test_flagging_persists(tmp_path):
    db = make_db(tmp_path)
    db.insert_new(sample_review(stars=1))
    db.save_draft("r1", "", needs_human=True, flag_reason="mentions illness")
    r = db.get("r1")
    assert r.needs_human is True
    assert r.flag_reason == "mentions illness"


def test_list_by_status(tmp_path):
    db = make_db(tmp_path)
    db.insert_new(sample_review("r1"))
    db.insert_new(sample_review("r2"))
    db.save_draft("r2", "hi", needs_human=False, flag_reason=None)

    assert {r.review_id for r in db.list_by_status(STATUS_NEW)} == {"r1"}
    assert {r.review_id for r in db.list_by_status(STATUS_DRAFTED)} == {"r2"}

"""Dashboard stat queries on the DB."""

from jolly_roger.db import Database, Review


def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def review(rid, stars, created):
    return Review(rid, "Guest " + rid, stars, "comment", created)


def test_rating_summary_from_stored_reviews(tmp_path):
    d = db(tmp_path)
    d.insert_new(review("a", 5, "2026-06-01T00:00:00Z"))
    d.insert_new(review("b", 4, "2026-06-02T00:00:00Z"))
    d.insert_new(review("c", 3, "2026-06-03T00:00:00Z"))
    avg, total = d.rating_summary()
    assert avg == 4.0
    assert total == 3


def test_rating_summary_prefers_google_meta(tmp_path):
    d = db(tmp_path)
    d.insert_new(review("a", 1, "2026-06-01T00:00:00Z"))  # local avg would be 1.0
    d.set_meta("google_average_rating", "4.3")
    d.set_meta("google_total_reviews", "1284")
    avg, total = d.rating_summary()
    assert avg == 4.3
    assert total == 1284


def test_rating_summary_empty(tmp_path):
    avg, total = db(tmp_path).rating_summary()
    assert avg is None
    assert total == 0


def test_rating_trend_is_running_average(tmp_path):
    d = db(tmp_path)
    d.insert_new(review("a", 5, "2026-06-01T00:00:00Z"))
    d.insert_new(review("b", 3, "2026-06-02T00:00:00Z"))  # running avg → 4.0
    trend = d.rating_trend(14)
    assert trend == [5.0, 4.0]


def test_rating_trend_samples_down_to_points(tmp_path):
    d = db(tmp_path)
    for i in range(50):
        d.insert_new(review(f"r{i}", 5, f"2026-06-01T00:00:{i:02d}Z"))
    trend = d.rating_trend(14)
    assert len(trend) == 14


def test_list_recent_orders_newest_first(tmp_path):
    d = db(tmp_path)
    d.insert_new(review("old", 5, "2026-06-01T00:00:00Z"))
    d.insert_new(review("new", 5, "2026-06-09T00:00:00Z"))
    recent = d.list_recent(8)
    assert [r.review_id for r in recent] == ["new", "old"]


def test_meta_roundtrip_and_upsert(tmp_path):
    d = db(tmp_path)
    assert d.get_meta("missing") is None
    d.set_meta("k", "1")
    d.set_meta("k", "2")  # upsert
    assert d.get_meta("k") == "2"

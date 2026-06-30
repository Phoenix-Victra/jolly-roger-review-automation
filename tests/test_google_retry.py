"""Transient Google API failures retry; real errors fail fast."""

import pytest
import requests

from jolly_roger.google_client import _request_with_retry


class FakeResp:
    def __init__(self, status, json_body=None):
        self.status_code = status
        self._json = json_body or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Returns/raises queued items, one per request() call."""

    def __init__(self, items):
        self._items = list(items)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _no_sleep(_seconds):
    pass


def test_retries_then_succeeds_on_503():
    sess = FakeSession([FakeResp(503), FakeResp(200, {"ok": True})])
    resp = _request_with_retry(sess, "GET", "u", _sleep=_no_sleep)
    assert resp.status_code == 200
    assert sess.calls == 2


def test_retries_on_connection_error():
    sess = FakeSession([requests.ConnectionError("boom"), FakeResp(200)])
    resp = _request_with_retry(sess, "GET", "u", _sleep=_no_sleep)
    assert resp.status_code == 200
    assert sess.calls == 2


def test_does_not_retry_404():
    sess = FakeSession([FakeResp(404)])
    with pytest.raises(requests.HTTPError):
        _request_with_retry(sess, "GET", "u", _sleep=_no_sleep)
    assert sess.calls == 1  # failed fast, no retry


def test_gives_up_after_exhausting_retries():
    sess = FakeSession([FakeResp(503)] * 4)  # 1 try + 3 retries
    with pytest.raises(requests.HTTPError):
        _request_with_retry(sess, "GET", "u", _sleep=_no_sleep)
    assert sess.calls == 4

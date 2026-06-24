"""Drafting must degrade to needs_human instead of crashing the poller."""

from __future__ import annotations

import json

from jolly_roger.db import Review
from jolly_roger.drafting import Drafter
from tests.conftest import make_config


class FakeBlock:
    def __init__(self, type_, text=None):
        self.type = type_
        self.text = text


class FakeResponse:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    def create(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._response


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.messages = FakeMessages(response, exc)


def _drafter(tmp_path, response=None, exc=None) -> Drafter:
    return Drafter(make_config(tmp_path), client=FakeClient(response, exc))


def _review():
    return Review("r1", "Sandy", 5, "Great food", "2026-06-20T00:00:00Z")


def test_happy_path(tmp_path):
    body = json.dumps(
        {"draft_reply": "Thanks Sandy!", "needs_human": False, "flag_reason": ""}
    )
    d = _drafter(tmp_path, FakeResponse([FakeBlock("text", body)]))
    result = d.draft(_review())
    assert result.draft_reply == "Thanks Sandy!"
    assert result.needs_human is False


def test_malformed_json_becomes_needs_human(tmp_path):
    d = _drafter(tmp_path, FakeResponse([FakeBlock("text", "not json {oops")]))
    result = d.draft(_review())
    assert result.needs_human is True
    assert "parse" in result.flag_reason.lower()


def test_missing_text_becomes_needs_human(tmp_path):
    d = _drafter(tmp_path, FakeResponse([]))  # no text block
    result = d.draft(_review())
    assert result.needs_human is True
    assert "no text" in result.flag_reason.lower()


def test_refusal_becomes_needs_human(tmp_path):
    d = _drafter(tmp_path, FakeResponse([], stop_reason="refusal"))
    result = d.draft(_review())
    assert result.needs_human is True
    assert "declined" in result.flag_reason.lower()


def test_max_tokens_truncation_becomes_needs_human(tmp_path):
    d = _drafter(tmp_path, FakeResponse([FakeBlock("text", "{")], stop_reason="max_tokens"))
    result = d.draft(_review())
    assert result.needs_human is True
    assert "max_tokens" in result.flag_reason.lower()


def test_api_exception_becomes_needs_human(tmp_path):
    d = _drafter(tmp_path, exc=RuntimeError("boom"))
    result = d.draft(_review())
    assert result.needs_human is True
    assert "api error" in result.flag_reason.lower()

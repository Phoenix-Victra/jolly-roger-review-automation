"""Shared test fixtures/helpers."""

from __future__ import annotations

import pytest

from jolly_roger.config import Config


def make_config(tmp_path, **overrides) -> Config:
    """Build a fully-populated Config for tests, with safe placeholder values."""
    base = dict(
        anthropic_api_key="test-key",
        draft_model="claude-sonnet-4-6",
        google_client_secrets_file="client_secret.json",
        google_token_file="token.json",
        gbp_account_name="accounts/1",
        gbp_location_name="locations/2",
        smtp_host="localhost",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        digest_from="bot@example.com",
        digest_to="owner@example.com",
        manager_to="manager@example.com",
        bad_review_max_stars=2,
        example_count=5,
        dashboard_base_url="http://localhost:5000",
        flask_secret_key="test-secret-key",
        dashboard_password="hunter2",
        database_path=str(tmp_path / "test.db"),
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)

"""Configuration loaded from environment (`.env` in development).

Keep all secrets here so the rest of the code never reads ``os.environ``
directly. Nothing in this module is logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional in production (real env vars)
    pass

# OAuth scope required to list reviews and post replies as the business.
GBP_SCOPE = "https://www.googleapis.com/auth/business.manage"

# Where the tone/voice guidance for the drafting step lives.
TONE_PROMPT_FILE = Path(__file__).resolve().parent.parent / "tone_prompt.md"


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class Config:
    # Anthropic
    anthropic_api_key: str
    draft_model: str

    # Google Business Profile
    google_client_secrets_file: str
    google_token_file: str
    gbp_account_name: str
    gbp_location_name: str

    # Email
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    digest_from: str
    digest_to: str
    manager_to: str
    bad_review_max_stars: int
    example_count: int
    dashboard_base_url: str

    # Storage
    database_path: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            anthropic_api_key=_require("ANTHROPIC_API_KEY"),
            draft_model=os.getenv("DRAFT_MODEL", "claude-sonnet-4-6"),
            google_client_secrets_file=os.getenv(
                "GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json"
            ),
            google_token_file=os.getenv("GOOGLE_TOKEN_FILE", "token.json"),
            gbp_account_name=os.getenv("GBP_ACCOUNT_NAME", ""),
            gbp_location_name=os.getenv("GBP_LOCATION_NAME", ""),
            smtp_host=os.getenv("SMTP_HOST", "localhost"),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_username=os.getenv("SMTP_USERNAME", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            digest_from=os.getenv("DIGEST_FROM", "jolly-roger-bot@example.com"),
            digest_to=os.getenv("DIGEST_TO", ""),
            manager_to=os.getenv("MANAGER_TO", ""),
            bad_review_max_stars=int(os.getenv("BAD_REVIEW_MAX_STARS", "2")),
            example_count=int(os.getenv("EXAMPLE_COUNT", "5")),
            dashboard_base_url=os.getenv(
                "DASHBOARD_BASE_URL", "http://localhost:5000"
            ),
            database_path=os.getenv("DATABASE_PATH", "jolly_roger.db"),
        )


def load_tone_prompt() -> str:
    """Return the tone/voice guidance used as Claude's system prompt.

    Falls back to a sensible default if the file is missing so the drafting
    step never hard-fails on a fresh checkout.
    """
    if TONE_PROMPT_FILE.exists():
        return TONE_PROMPT_FILE.read_text(encoding="utf-8").strip()
    return (
        "You write friendly, warm, specific replies to Google reviews for a "
        "casual beachside restaurant and bar. Thank reviewers by name, keep it "
        "short, never include personal data, and never promise refunds or comps."
    )

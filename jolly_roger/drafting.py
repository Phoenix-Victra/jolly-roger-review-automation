"""Reply drafting with the Claude API.

For each review we ask Claude for a draft reply AND a judgement on whether the
review needs a human (1–2 star, illness, refunds, legal threats, etc.) so those
never get a canned-feeling auto-reply. We use structured outputs so the response
is always valid JSON we can store directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic

from .config import Config, load_tone_prompt
from .db import Review

log = logging.getLogger("jolly_roger.drafting")

# JSON schema the model must fill in. additionalProperties:false + required are
# mandatory for structured outputs.
_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "draft_reply": {
            "type": "string",
            "description": "The suggested public reply to post (2-4 sentences).",
        },
        "needs_human": {
            "type": "boolean",
            "description": (
                "True if this review should be handled by a human rather than "
                "an auto-draft: 1-2 stars, mentions of illness/injury, refund "
                "demands, legal threats, discrimination, or safety issues."
            ),
        },
        "flag_reason": {
            "type": "string",
            "description": (
                "Short reason it was flagged for a human, or an empty string "
                "if needs_human is false."
            ),
        },
    },
    "required": ["draft_reply", "needs_human", "flag_reason"],
    "additionalProperties": False,
}


@dataclass
class DraftResult:
    draft_reply: str
    needs_human: bool
    flag_reason: str


class Drafter:
    """Wraps the Anthropic client and the tone/voice system prompt."""

    def __init__(self, config: Config, client: Any | None = None):
        # `client` is injectable for tests; defaults to a real Anthropic client.
        self._client = client or anthropic.Anthropic(
            api_key=config.anthropic_api_key
        )
        self._model = config.draft_model
        self._tone_prompt = load_tone_prompt()

    @staticmethod
    def _needs_human(reason: str) -> "DraftResult":
        """A safe result that routes the review to a human instead of crashing."""
        return DraftResult(draft_reply="", needs_human=True, flag_reason=reason)

    @staticmethod
    def _format_examples(examples: list[Review] | None) -> str:
        """Render past review→reply pairs as few-shot context, or empty string."""
        if not examples:
            return ""
        blocks = ["Here are replies we've posted before. Match their voice:\n"]
        for ex in examples:
            blocks.append(
                f"--- Past review ({ex.star_rating}/5): "
                f"{ex.comment or '(no comment)'}\n"
                f"Our reply: {ex.final_reply}\n"
            )
        return "\n".join(blocks) + "\n"

    def draft(
        self, review: Review, examples: list[Review] | None = None
    ) -> DraftResult:
        """Generate a draft reply and human-needed flag for one review.

        ``examples`` are past reviews plus the replies we actually posted.
        Passing them lets the model match the house voice we've already
        established rather than writing from the tone guide alone.
        """
        instruction = (
            "Write a reply following the voice and rules above"
            + (", matching the style of the example replies" if examples else "")
            + ", and decide whether this review needs a human."
        )
        user_content = (
            f"{self._format_examples(examples)}"
            f"Now draft a reply for this review.\n\n"
            f"Review by {review.author or 'a guest'} "
            f"({review.star_rating}/5 stars):\n\n"
            f"{review.comment or '(no written comment)'}\n\n"
            f"{instruction}"
        )

        # Any failure here must route the review to a human, never crash the
        # poller. We handle: API errors, refusal, truncation, missing text,
        # and malformed/incomplete JSON.
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=self._tone_prompt,
                messages=[{"role": "user", "content": user_content}],
                output_config={
                    "format": {"type": "json_schema", "schema": _DRAFT_SCHEMA}
                },
            )
        except Exception as exc:  # noqa: BLE001 - never let drafting crash the poll
            log.warning("drafting API error for %s: %s", review.review_id, exc)
            return self._needs_human(
                f"Claude API error while drafting: {type(exc).__name__}"
            )

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            return self._needs_human("Claude declined to draft a reply.")
        if stop_reason == "max_tokens":
            return self._needs_human(
                "Claude's reply was cut off (max_tokens); needs a human."
            )

        text = next(
            (
                b.text
                for b in getattr(response, "content", [])
                if getattr(b, "type", None) == "text"
            ),
            None,
        )
        if not text:
            return self._needs_human("Claude returned no text output.")

        try:
            data = json.loads(text)
            return DraftResult(
                draft_reply=str(data["draft_reply"]),
                needs_human=bool(data["needs_human"]),
                flag_reason=str(data.get("flag_reason", "")),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("could not parse draft for %s: %s", review.review_id, exc)
            return self._needs_human(f"Could not parse Claude output: {exc}")

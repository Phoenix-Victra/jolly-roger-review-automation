"""Build and send the daily digest email of new reviews + draft replies."""

from __future__ import annotations

import smtplib
import textwrap
from email.message import EmailMessage
from email.utils import formatdate
from html import escape

from .config import Config
from .db import Review

_STARS = {1: "★☆☆☆☆", 2: "★★☆☆☆", 3: "★★★☆☆", 4: "★★★★☆", 5: "★★★★★"}


def _stars(rating: int) -> str:
    return _STARS.get(rating, "?")


def render_text(reviews: list[Review], dashboard_url: str) -> str:
    """Plain-text body, used as a fallback for non-HTML clients."""
    lines = [
        f"Jolly Roger — {len(reviews)} new review(s)",
        "",
    ]
    for r in reviews:
        lines.append(f"{_stars(r.star_rating)}  {r.author}  ({r.created_at})")
        if r.needs_human:
            lines.append(f"  ⚠ NEEDS A HUMAN: {r.flag_reason}")
        lines.append("  Review:")
        lines.append(textwrap.indent(r.comment or "(no comment)", "    "))
        lines.append("  Suggested reply:")
        lines.append(textwrap.indent(r.draft_reply or "(none)", "    "))
        lines.append(f"  Approve / edit / reject: {dashboard_url}/review/{r.review_id}")
        lines.append("")
    lines.append(f"Open the dashboard: {dashboard_url}")
    return "\n".join(lines)


def render_html(reviews: list[Review], dashboard_url: str) -> str:
    cards = []
    for r in reviews:
        flag = (
            f'<p style="color:#b00;font-weight:bold">⚠ Needs a human: '
            f"{escape(r.flag_reason or '')}</p>"
            if r.needs_human
            else ""
        )
        cards.append(
            f"""
            <div style="border:1px solid #ddd;border-radius:8px;padding:16px;margin:12px 0">
              <p style="margin:0 0 4px">
                <span style="color:#e8a33d;font-size:18px">{_stars(r.star_rating)}</span>
                &nbsp;<strong>{escape(r.author)}</strong>
                <span style="color:#888">&nbsp;{escape(r.created_at)}</span>
              </p>
              {flag}
              <p style="margin:8px 0"><em>{escape(r.comment or '(no comment)')}</em></p>
              <p style="margin:8px 0;background:#f6f6f6;padding:10px;border-radius:6px">
                <strong>Suggested reply:</strong><br>{escape(r.draft_reply or '(none)')}
              </p>
              <a href="{dashboard_url}/review/{escape(r.review_id)}"
                 style="display:inline-block;background:#1a73e8;color:#fff;
                        padding:8px 14px;border-radius:6px;text-decoration:none">
                Approve / edit / reject
              </a>
            </div>
            """
        )
    return f"""\
<html><body style="font-family:system-ui,Arial,sans-serif;max-width:640px;margin:auto">
  <h2>Jolly Roger — {len(reviews)} new review(s)</h2>
  {''.join(cards)}
  <p><a href="{dashboard_url}">Open the dashboard</a></p>
</body></html>"""


def build_message(config: Config, reviews: list[Review]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"Jolly Roger reviews — {len(reviews)} new"
    msg["From"] = config.digest_from
    msg["To"] = config.digest_to
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(render_text(reviews, config.dashboard_base_url))
    msg.add_alternative(
        render_html(reviews, config.dashboard_base_url), subtype="html"
    )
    return msg


def send_digest(config: Config, reviews: list[Review]) -> bool:
    """Send the digest. Returns False (and sends nothing) if there's nothing new."""
    if not reviews:
        return False
    if not config.digest_to:
        raise RuntimeError("DIGEST_TO is not set; cannot send the digest.")

    msg = build_message(config, reviews)
    with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
        server.ehlo()
        if config.smtp_port == 587:
            server.starttls()
            server.ehlo()
        if config.smtp_username:
            server.login(config.smtp_username, config.smtp_password)
        server.send_message(msg)
    return True

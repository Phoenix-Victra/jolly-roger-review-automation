# Jolly Roger — Review Monitor & Reply Assistant

A small service that watches the Google reviews for **The Jolly Roger Restaurant
and Bar** (Kill Devil Hills, NC), emails a daily digest of new reviews with
Claude-drafted replies, and posts approved replies back to Google **only after a
human approves them**. Nothing posts automatically.

```
  ┌─────────────┐   new reviews   ┌────────────┐  drafts  ┌─────────────┐
  │ Google      │ ──────────────► │ Poller +   │ ───────► │ Claude API  │
  │ Business    │                 │ local DB   │ ◄─────── │ (drafts)    │
  │ Profile API │ ◄────────────── │ (SQLite)   │          └─────────────┘
  └─────────────┘  post approved  └─────┬──────┘
                      reply             │ digest email + approval links
                                        ▼
                                 ┌────────────┐
                                 │  Dashboard │  approve / edit / reject
                                 └────────────┘
```

## What it does

1. **Pull reviews** — fetch new Google reviews since the last run (dedupe by review ID).
2. **Draft replies** — Claude writes a draft reply for each review and flags any
   that need a human (1–2 star, illness, refunds, legal threats).
3. **Email digest** — send the new reviews + draft replies to a chosen inbox,
   each with a link into the dashboard.
4. **Approve & post** — in the dashboard you Approve (post as-is), Edit then
   approve (post your version), or Reject. On approval the reply is posted via
   the Business Profile API.

## Project layout

```
jolly_roger/
  config.py        env/config loading; OAuth scope; tone-prompt loader
  db.py            SQLite schema + Review model + workflow status
  drafting.py      Claude drafting + sensitive-review flagging (structured output)
  google_client.py OAuth + GBP v4 review list/reply + account/location discovery
  email_digest.py  HTML/text digest builder + SMTP send
  poller.py        orchestration: pull → dedupe → draft → digest; post_reply()
  cli.py           `discover` and `poll` commands
  dashboard/       Flask Approve / Edit / Reject UI
tone_prompt.md     voice/tone guidance used as Claude's system prompt
tests/             unit tests (DB, drafting, digest — all mocked, no network)
```

## The one thing that can block you: API access (do this first)

The public Google Places API only returns ~5 reviews and **cannot post
replies**. Listing all reviews and replying as the business lives in the
**Google Business Profile APIs**, which are gated:

- The verified owner must add your Google account as a **Manager/Owner** on the
  Business Profile.
- You must submit Google's **Business Profile API access request form** and be
  approved before the project can call the review endpoints in production
  (often takes days — start now). See https://developers.google.com/my-business.

The review list/reply methods live on the older `mybusiness.googleapis.com/v4`
surface; account/location IDs come from the Account Management and Business
Information APIs. This is **Google-only** — Yelp and TripAdvisor have no
programmatic reply API.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in real values
```

Put your Google OAuth **Desktop app** client secret JSON next to the project as
`client_secret.json` (path configurable via `GOOGLE_CLIENT_SECRETS_FILE`).

### Phase 1 — find your account & location

Once access is granted, run a one-time discovery to get the resource names:

```bash
python -m jolly_roger.cli discover
```

Copy the printed `accounts/…` and `locations/…` names into `GBP_ACCOUNT_NAME`
and `GBP_LOCATION_NAME` in `.env`.

### Phases 2–3 — poll, draft, and email

```bash
python -m jolly_roger.cli poll
```

Wire that to cron on an always-on host (a poll every few hours is plenty):

```cron
0 */4 * * *  cd /opt/jolly-roger && /opt/jolly-roger/.venv/bin/python -m jolly_roger.cli poll >> /var/log/jolly-roger.log 2>&1
```

### Phase 4 — the approval dashboard

```bash
flask --app jolly_roger.dashboard.app run
```

Open http://localhost:5000, review each draft, and Approve / Edit / Reject.
Approval posts the reply to Google and records `final_reply` + `posted_at`.

## Data model (SQLite)

The `reviews` table tracks every review and its status so you never
double-process or double-reply:

`review_id` (PK / dedupe), `author`, `star_rating`, `comment`, `created_at`,
`draft_reply`, `status` (`new` → `drafted` → `approved` → `posted` /
`rejected` / `skipped`), `needs_human` + `flag_reason`, `final_reply`,
`posted_at`.

## Good practice baked in

- **Never auto-post.** Human approval is mandatory.
- **Flag sensitive reviews** (1–2 star, illness, refunds, legal) so they never
  get a canned-feeling reply.
- **Don't overwrite an existing reply** — `post_reply` checks Google first.
- **Secrets stay out of git** — `.env` and tokens are git-ignored.

## Tests

```bash
pip install pytest
pytest
```

Tests are fully mocked — no Google or Anthropic calls, no network.

## Models

Reply drafting uses `claude-sonnet-4-6` by default (set `DRAFT_MODEL` to
override) — strong writing quality at lower cost for this short-form task.

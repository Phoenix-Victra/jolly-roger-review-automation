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

**Requires Python 3.10 or newer** (`python3 --version` to check).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # runtime deps
pip install -r requirements-dev.txt      # + pytest, for running tests
cp .env.example .env                      # then fill in real values
```

Put your Google OAuth **Desktop app** client secret JSON next to the project as
`client_secret.json` (path configurable via `GOOGLE_CLIENT_SECRETS_FILE`).

The dashboard requires two extra settings in `.env`:

```bash
# A long random string that signs the session cookie:
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into FLASK_SECRET_KEY
DASHBOARD_PASSWORD=...   # the password you'll type to sign in
```

### Phase 1 — find your account & location

Once access is granted, run a one-time discovery to get the resource names:

```bash
python -m jolly_roger.cli discover
```

Copy the printed `accounts/…` and `locations/…` names into `GBP_ACCOUNT_NAME`
and `GBP_LOCATION_NAME` in `.env`.

> **First-time Google login on a headless/always-on host.** The OAuth consent
> step needs a browser. On a server or Raspberry Pi without one, run the login
> **once on a laptop** (any machine with a browser), then copy the generated
> `token.json` to the host next to the project. After that, the token refreshes
> automatically and no browser is needed again. (The flow prints the auth URL
> rather than forcing a browser launch, so an SSH session with port forwarding
> can also work.)

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

Open http://localhost:5000, sign in with `DASHBOARD_PASSWORD`, review each draft,
and Approve / Edit / Reject. Approval posts the reply to Google and records
`final_reply` + `posted_at` **only after Google confirms** — if posting fails the
review stays in `drafted` and the error is shown. All forms are CSRF-protected
and every route except login requires a signed-in session.

## Review routing (this branch)

New reviews are split on the way out:

- **Bad reviews → the manager.** Any review at or below `BAD_REVIEW_MAX_STARS`
  (default 2★), or one Claude flags as needing a human, is emailed straight to
  `MANAGER_TO` for hands-on handling instead of going into the auto-approval
  digest. A draft is still included for reference, but the manager decides.
- **Good reviews → the owner digest, written in our own voice.** For higher-star
  reviews, the drafter is given the last `EXAMPLE_COUNT` replies we actually
  posted (from the DB) as few-shot examples, so new drafts match the style of
  responses we've approved before rather than sounding generic.

Set `MANAGER_TO`, `BAD_REVIEW_MAX_STARS`, and `EXAMPLE_COUNT` in `.env`.

## Data model (SQLite)

The `reviews` table tracks every review and its status so you never
double-process or double-reply:

`review_id` (PK / dedupe), `author`, `star_rating`, `comment`, `created_at`,
`draft_reply`, `status` (`new` → `drafted` → `approved` → `posted` /
`rejected` / `skipped`), `needs_human` + `flag_reason`, `final_reply`,
`posted_at`.

## Good practice baked in

- **Never auto-post.** Human approval is mandatory; a review is marked `posted`
  only after Google confirms the reply went through.
- **Flag sensitive reviews** (1–2 star, illness, refunds, legal) so they never
  get a canned-feeling reply. Drafting failures (API error, refusal, truncation,
  bad JSON) also degrade to `needs_human` rather than crashing the poller.
- **Don't overwrite or invent.** `post_reply` checks Google first: it refuses to
  overwrite an existing reply and refuses to post to a review it can't find
  remotely. Reviews that already have a reply are recorded as `skipped`.
- **No stranded drafts.** A `notified` flag tracks whether a draft was emailed;
  if SMTP fails, the next poll re-sends it (good → owner digest, bad → manager).
- **Resilient to Google hiccups.** Transient errors (429/5xx, dropped
  connections) are retried with backoff; real errors (403/404) fail fast.
- **Dashboard auth + CSRF** — password login over a signed session, CSRF tokens
  on every form.
- **Secrets stay out of git** — `.env` and tokens are git-ignored, and `Config`'s
  repr redacts secrets so they can't leak into logs.

## TODO / future

- **Possible browser-automation fallback.** The Business Profile API is
  access-gated (owner grant + Google approval of the access-request form, often
  days). If approval is slow or denied, evaluate a headless-browser fallback
  (e.g. Playwright) to read reviews and post approved replies through the
  Business Profile web UI. Revisit once API access status is known — keep the
  human-approval step regardless.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests are fully mocked — no Google, Anthropic, or SMTP calls, no network. They
cover the DB, the poller (routing, dedupe, skip-already-replied, email retry,
`post_reply` guards), drafting failure modes, and the dashboard (login required,
CSRF, approve success, approve-failure-keeps-drafted, reject).

## Models

Reply drafting uses `claude-sonnet-4-6` by default (set `DRAFT_MODEL` to
override) — strong writing quality at lower cost for this short-form task.

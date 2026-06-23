"""Command-line entry points.

    python -m jolly_roger.cli discover   # list accounts/locations (Phase 1 setup)
    python -m jolly_roger.cli poll        # one poll cycle (wire to cron)
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Config
from .db import Database
from .drafting import Drafter
from .google_client import GoogleBusinessClient
from .poller import run_poll


def _discover(config: Config) -> int:
    google = GoogleBusinessClient(config)
    accounts = google.list_accounts()
    if not accounts:
        print("No Business Profile accounts found for this Google user.")
        return 1
    for acct in accounts:
        print(f"\nAccount: {acct.get('name')}  ({acct.get('accountName', '')})")
        for loc in google.list_locations(acct["name"]):
            print(f"  Location: {loc.get('name')}  — {loc.get('title', '')}")
    print(
        "\nCopy the account/location resource names into GBP_ACCOUNT_NAME and "
        "GBP_LOCATION_NAME in your .env."
    )
    return 0


def _poll(config: Config) -> int:
    db = Database(config.database_path)
    google = GoogleBusinessClient(config)
    drafter = Drafter(config)
    result = run_poll(config, db, google, drafter)
    print(
        f"{result.new_reviews} new, {result.drafted} drafted, "
        f"{result.flagged} flagged, digest_sent={result.digest_sent}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="jolly_roger")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="List Business Profile accounts and locations")
    sub.add_parser("poll", help="Run one poll cycle (pull, draft, email digest)")

    args = parser.parse_args(argv)
    config = Config.from_env()

    if args.command == "discover":
        return _discover(config)
    if args.command == "poll":
        return _poll(config)
    return 2


if __name__ == "__main__":
    sys.exit(main())

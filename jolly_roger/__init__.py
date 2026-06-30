"""Jolly Roger — Review Monitor & Reply Assistant.

Watches Google reviews for The Jolly Roger Restaurant and Bar, emails a daily
digest of new reviews with Claude-drafted replies, and posts approved replies
back to Google after a human approves them in a small dashboard.
"""

import sys

if sys.version_info < (3, 10):  # the codebase uses 3.10+ typing syntax
    raise RuntimeError(
        "Jolly Roger requires Python 3.10 or newer "
        f"(found {sys.version_info.major}.{sys.version_info.minor})."
    )

__version__ = "0.1.0"

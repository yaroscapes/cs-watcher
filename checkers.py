"""Availability checkers — per-booking-system HTTP probes.

Each checker takes a target dict and returns True if a spot is available
matching the requested check-in / nights / party_size, False otherwise.

These are STUBS. Each one needs to be filled in by reverse-engineering
the target booking site's network requests. Implementation guidance is
deliberately not in this file — this code stays public and generic.

System identifiers (`system_a`, `system_b`, ...) are arbitrary opaque
labels. The mapping to real booking platforms is held privately by the
operator (you), not in the repo. The `TARGETS_JSON` secret references
these labels; this file dispatches on them.

Security expectations for any implementation added here:
  - HTTPS only.
  - 10-15 second timeouts on every network call.
  - Catch network errors and return False rather than raising; the
    watcher has top-level handling, but per-target failures shouldn't
    take down the whole run.
  - Do NOT print target details, response bodies, URLs, or anything
    derived from them. The public Actions log is world-readable.
  - Use a realistic User-Agent header.
"""
from __future__ import annotations

import urllib.error
import urllib.request


# Realistic desktop UA. Update periodically as browsers ship new versions.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15


def check_target(target: dict) -> bool:
    """Dispatch to the right checker based on `system` field."""
    system = target.get("system")
    if system == "system_a":
        return check_system_a(target)
    if system == "system_b":
        return check_system_b(target)
    raise ValueError(f"unknown system: {system!r}")


def check_system_a(target: dict) -> bool:
    """Booking system A. TODO: implement.

    Return True iff the underlying platform shows availability for
    target['checkin'] for at least target['nights'] nights at party
    size target['party_size']. Otherwise return False.

    On network/parse errors: prefer returning False over raising. The
    top-level watcher will treat any raised exception as an error and
    surface it via ntfy (deduped), but a transient blip shouldn't
    necessarily fire that path.
    """
    return False


def check_system_b(target: dict) -> bool:
    """Booking system B. TODO: implement.

    Same contract as check_system_a.
    """
    return False

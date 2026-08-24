"""ntfy.sh notification.

Two notification flavors:
  - notify(target, available_dates, requested_dates) — high-priority push
    when one or more nights (or day-use slots) become newly available.
  - notify_error(summary) — default-priority push when the watcher itself
    encounters errors (deduped by caller so you don't get spammed every
    5 minutes).

Security notes:
- Topic name is read from env (NTFY_TOPIC). Never logged, never echoed.
- Posts over HTTPS only.
- 10-second timeout to avoid hanging the workflow.
- On failure, raises with a generic message — caller logs only the error
  class, never the message body, so nothing about the topic or payload
  leaks to the public Actions log.
"""
from __future__ import annotations

import datetime as _dt
import os
import urllib.error
import urllib.request


NTFY_BASE = "https://ntfy.sh"
TIMEOUT_SECONDS = 10


def _resolve_topic() -> str:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        raise RuntimeError("NTFY_TOPIC not set")
    # ntfy topics are alphanumeric + dash/underscore. If something weird
    # is in the env, fail loudly rather than send to a surprise URL.
    if not all(c.isalnum() or c in "-_" for c in topic):
        raise RuntimeError("NTFY_TOPIC contains invalid characters")
    return topic


def _post(topic: str, body: bytes, headers: dict) -> None:
    headers = {**headers, "Content-Type": "text/plain; charset=utf-8"}
    req = urllib.request.Request(
        f"{NTFY_BASE}/{topic}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"ntfy http {resp.status}")
    except urllib.error.URLError as e:
        # Don't include str(e) — could echo URL bits.
        raise RuntimeError(f"ntfy network error: {type(e).__name__}") from None


def _format_dates(dates: list[str], details: dict[str, list[str]]) -> str:
    """Render dates, naming the specific slots that opened where known."""
    if not details:
        return ", ".join(dates)
    parts = []
    for date in dates:
        labels = details.get(date)
        parts.append(f"{date} ({', '.join(labels)})" if labels else date)
    return "; ".join(parts)


def _booking_url(target: dict, dates: list[str]) -> str:
    """Resolve the target's booking URL.

    A `{date}` placeholder is filled with the earliest date that just
    opened, so day-use pushes deep-link straight to the right day
    instead of the platform's default landing date. `{date+1}` is the
    day after — some booking front-ends express a single day as a
    half-open start/end pair and reject start == end.
    """
    url = target.get("booking_url") or ""
    if not url or "{date" not in url:
        return url
    if not dates:
        return ""
    day = _dt.date.fromisoformat(dates[0])
    url = url.replace("{date+1}", (day + _dt.timedelta(days=1)).isoformat())
    return url.replace("{date}", day.isoformat())


def notify(
    target: dict,
    newly_available: list[str],
    total_available: list[str],
    requested: list[str],
    details: dict[str, list[str]] | None = None,
) -> None:
    """Send a high-priority push for newly available dates.

    `newly_available` is the list of dates (sorted) that became available
    this run (not previously seen). `total_available` is everything in
    the requested window currently open. `requested` is everything
    requested. `details` optionally maps a date to the specific slots
    that are open on it (day-use targets, where one date has several
    bookable departure windows).

    Raises RuntimeError on any failure. Caller should catch broadly and
    log only the error class name to keep the public log clean.
    """
    topic = _resolve_topic()
    details = details or {}

    name = target.get("name", "unknown")
    # Nights for a campsite or hut, days for a day-use slot.
    unit = target.get("unit", "night")
    n_total = len(total_available)
    n_req = len(requested)

    if n_total == n_req:
        title = f"{name}: all {n_req} {unit}(s) open"
    else:
        title = f"{name}: {n_total} of {n_req} {unit}(s) open"

    body_lines = [name]
    body_lines.append(f"Newly available: {_format_dates(newly_available, details)}")
    if total_available != newly_available:
        body_lines.append(
            f"All open in window: {_format_dates(total_available, details)}"
        )
    body_lines.append(f"Party size: {target.get('party_size', '?')}")
    url = _booking_url(target, newly_available)
    if url:
        body_lines.append(f"Book: {url}")
    body = "\n".join(body_lines).encode("utf-8")

    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": str(target.get("tags") or "tent,bell"),
    }
    if url:
        headers["Click"] = url

    _post(topic, body, headers)


def notify_error(summary: str) -> None:
    """Send a default-priority push that the watcher hit errors.

    `summary` is a short, generic string like "2 target(s) failed:
    target 1 (HTTPError), target 2 (TimeoutError)". MUST NOT contain
    target names, dates, URLs, or response bodies — this gets echoed
    out via ntfy and is also visible in the public Actions log.

    Raises RuntimeError on any failure.
    """
    topic = _resolve_topic()
    body = summary.encode("utf-8")
    headers = {
        "Title": "Campsite watcher: errors",
        "Priority": "default",
        "Tags": "warning",
    }
    _post(topic, body, headers)

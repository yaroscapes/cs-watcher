"""ntfy.sh notification.

Two notification flavors:
  - notify(target)       — high-priority push when availability is found.
  - notify_error(summary) — default-priority push when the watcher
                            itself encounters errors (deduped by caller
                            so you don't get spammed every 5 minutes).

Security notes:
- Topic name is read from env (NTFY_TOPIC). Never logged, never echoed.
- Posts over HTTPS only.
- 10-second timeout to avoid hanging the workflow.
- On failure, raises with a generic message — caller logs only the error
  class, never the message body, so nothing about the topic or payload
  leaks to the public Actions log.
"""
from __future__ import annotations

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


def notify(target: dict) -> None:
    """Send a high-priority push about an availability hit.

    Raises RuntimeError on any failure. Caller should catch broadly and
    log only the error class name to keep the public log clean.
    """
    topic = _resolve_topic()

    title = f"Campsite open: {target.get('name', 'unknown')}"
    body_lines = [
        target.get("name", "unknown"),
        f"Check-in: {target.get('checkin', '?')} ({target.get('nights', '?')} night(s))",
        f"Party size: {target.get('party_size', '?')}",
    ]
    if target.get("booking_url"):
        body_lines.append(f"Book: {target['booking_url']}")
    body = "\n".join(body_lines).encode("utf-8")

    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "tent,bell",
    }
    if target.get("booking_url"):
        headers["Click"] = target["booking_url"]

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

"""Availability checkers — per-booking-system HTTP probes.

Each checker takes a target dict and returns a frozenset of ISO-format
date strings (YYYY-MM-DD) for which the underlying booking platform
shows availability, *within the requested window*. Empty set means
nothing is available right now.

These checkers are HTTP-only and never persist anything. They raise on
unexpected response shapes / HTTP errors so the watcher's error path
fires (deduped on the watcher side, so a sustained outage = one push).

System identifiers (`system_a`, `system_b`, ...) are arbitrary opaque
labels. The mapping to real booking platforms is held privately by the
operator (you), not in the repo. The `TARGETS_JSON` secret references
these labels; this file dispatches on them.

Security expectations for any implementation added here:
  - HTTPS only.
  - 10-15 second timeouts on every network call.
  - Raise on unexpected status / shape; the watcher catches and reports
    via the deduped error notification.
  - Do NOT print target details, response bodies, URLs, or anything
    derived from them. The public Actions log is world-readable.
  - Use a realistic User-Agent header.
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.error
import urllib.parse
import urllib.request


# Realistic desktop UA. Update periodically as browsers ship new versions.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15


def _requested_dates(target: dict) -> list[str]:
    """Expand checkin + nights into a list of ISO date strings."""
    checkin = _dt.date.fromisoformat(target["checkin"])
    nights = int(target["nights"])
    if nights < 1:
        raise ValueError("nights must be >= 1")
    return [(checkin + _dt.timedelta(days=i)).isoformat() for i in range(nights)]


def check_target(target: dict) -> frozenset[str]:
    """Dispatch to the right checker. Returns set of available dates."""
    system = target.get("system")
    if system == "system_a":
        return check_system_a(target)
    if system == "system_b":
        return check_system_b(target)
    raise ValueError(f"unknown system: {system!r}")


def check_system_a(target: dict) -> frozenset[str]:
    """Booking system A. TODO: implement.

    Should return a frozenset of ISO date strings (subset of
    _requested_dates(target)) that the platform reports as available
    right now. Empty set = nothing available.
    """
    return frozenset()


def check_system_b(target: dict) -> frozenset[str]:
    """Booking system B.

    Calls a JSON map-availability endpoint. Required `target['params']`:
      - host: API host (e.g. "reservation.example.com")
      - map_id: int (campground map ID)
      - resource_location_id: int (facility ID)
      - equipment_category_id: int (equipment / site type)

    Returns the set of dates within target's [checkin, checkin+nights)
    window that show availability == 0 for at least one resource.
    Raises on HTTP error, parse failure, or unexpected shape.
    """
    requested = _requested_dates(target)
    if not requested:
        return frozenset()

    params = target.get("params") or {}
    for required_key in ("host", "map_id", "resource_location_id", "equipment_category_id"):
        if required_key not in params:
            raise ValueError(f"system_b: missing param ({required_key})")

    host = params["host"]
    if not isinstance(host, str) or "/" in host or " " in host:
        raise ValueError("system_b: invalid host")

    checkin = _dt.date.fromisoformat(target["checkin"])
    end_date = checkin + _dt.timedelta(days=int(target["nights"]))

    qs = urllib.parse.urlencode({
        "mapId": int(params["map_id"]),
        "resourceLocationId": int(params["resource_location_id"]),
        "startDate": checkin.isoformat(),
        "endDate": end_date.isoformat(),
        "equipmentCategoryId": int(params["equipment_category_id"]),
        "partySize": int(target.get("party_size", 1)),
        "numEquipment": 1,
    })
    url = f"https://{host}/api/availability/map?{qs}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                raise RuntimeError(f"http {resp.status}")
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # Cloudflare / WAF will surface here as 403/429/503.
        raise RuntimeError(f"http {e.code}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"network ({type(e.reason).__name__})") from None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("non-JSON response") from None

    return _parse_system_b_response(data, requested)


def _parse_system_b_response(data: object, requested_dates: list[str]) -> frozenset[str]:
    """Extract the set of available dates from a Going-To-Camp / Aspira
    style /api/availability/map response.

    Response shape (best-effort — the platform's exact schema isn't
    publicly documented and may evolve):
      {
        "resourceAvailabilities": {
          "<resource_id>": [
            {"date": "2026-08-01T00:00:00", "availability": 0, ...},
            ...
          ],
          ...
        },
        ...
      }

    `availability == 0` means available (camply convention). A date is
    "available" overall if at least one resource has it open.
    """
    if not isinstance(data, dict):
        raise RuntimeError("response not an object")

    resources = data.get("resourceAvailabilities")
    if not isinstance(resources, dict):
        raise RuntimeError("missing resourceAvailabilities")

    requested_set = set(requested_dates)
    open_dates: set[str] = set()

    for _resource_id, availabilities in resources.items():
        if not isinstance(availabilities, list):
            continue
        for entry in availabilities:
            if not isinstance(entry, dict):
                continue
            avail = entry.get("availability")
            date_raw = entry.get("date")
            if not isinstance(date_raw, str):
                continue
            # Date may be "2026-08-01" or "2026-08-01T00:00:00".
            iso_date = date_raw[:10]
            if iso_date in requested_set and avail == 0:
                open_dates.add(iso_date)

    return frozenset(open_dates)

"""Availability checkers — per-booking-system probes.

Each checker takes a target dict and returns a frozenset of ISO-format
date strings (YYYY-MM-DD) for which the underlying booking platform
shows availability, *within the requested window*. Empty set means
nothing is available right now.

Checkers raise on unexpected response shapes / HTTP errors so the
watcher's error path fires (deduped on the watcher side).

System identifiers (`system_a`, `system_b`, ...) are arbitrary opaque
labels. The mapping to real booking platforms is held privately by the
operator (you), not in the repo. The `TARGETS_JSON` secret references
these labels; this file dispatches on them.

Security expectations:
  - HTTPS only.
  - 10-15 second timeouts on every network call.
  - Raise on unexpected status / shape.
  - Never print target details, response bodies, URLs, or anything
    derived from them. The public Actions log is world-readable.
"""
from __future__ import annotations

import datetime as _dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15
BROWSER_TIMEOUT_MS = 60000

# Pause between consecutive per-night probes of the same target. A wide
# window is dozens of requests; without this they'd land as a burst.
NIGHT_PROBE_DELAY_SECONDS = 0.25

_WEEKDAY_NAMES = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


def _parse_weekdays(raw: object) -> frozenset[int]:
    """Accept ["Thu","Fri",...] or [3,4,...]; Monday is 0."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("weekdays must be a non-empty list")
    out: set[int] = set()
    for item in raw:
        # bool is an int subclass — reject it before the int branch.
        if isinstance(item, bool):
            raise ValueError("weekdays entries must be names or ints")
        if isinstance(item, int):
            if not 0 <= item <= 6:
                raise ValueError("weekday int out of range")
            out.add(item)
        elif isinstance(item, str):
            key = item.strip()[:3].lower()
            if key not in _WEEKDAY_NAMES:
                raise ValueError("unknown weekday name")
            out.add(_WEEKDAY_NAMES[key])
        else:
            raise ValueError("weekdays entries must be names or ints")
    return frozenset(out)


def requested_dates(target: dict) -> list[str]:
    """Expand checkin + nights into a list of ISO date strings.

    Optional `target['weekdays']` narrows the span to specific nights of
    the week, so one wide target can cover e.g. every Thursday-through-
    Tuesday night across two months without also watching Wednesdays.
    """
    checkin = _dt.date.fromisoformat(target["checkin"])
    nights = int(target["nights"])
    if nights < 1:
        raise ValueError("nights must be >= 1")
    span = [checkin + _dt.timedelta(days=i) for i in range(nights)]
    wanted = target.get("weekdays")
    if wanted:
        allowed = _parse_weekdays(wanted)
        span = [d for d in span if d.weekday() in allowed]
    return [d.isoformat() for d in span]


def check_target(target: dict) -> frozenset[str]:
    system = target.get("system")
    if system == "system_a":
        return check_system_a(target)
    if system == "system_b":
        return check_system_b(target)
    raise ValueError(f"unknown system: {system!r}")


# ---------------------------------------------------------------------------
# system_a — booking engine fronted by a CDN that fingerprints non-browser
# clients, so we use real Chromium (Playwright) to obtain a valid session
# before the API will accept our request.
# ---------------------------------------------------------------------------

def check_system_a(target: dict) -> frozenset[str]:
    """Booking engine accessed via headless browser.

    Required `target['params']`:
      - host: API host
      - booking_url: page URL to load to obtain a valid session
      - trigger_selector: CSS / text selector for a button that triggers
        the page to call the underlying API
      - service_id: parent service UUID
      - booking_engine_id: booking engine UUID
      - enterprise_id: enterprise UUID
      - category_id: specific resource category UUID for this target
      - tz: IANA timezone for "midnight local" date conversion
        (DST-aware, e.g. a Mountain Time zone)
    """
    requested = requested_dates(target)
    if not requested:
        return frozenset()

    params = target.get("params") or {}
    required = (
        "host", "booking_url", "trigger_selector",
        "service_id", "booking_engine_id", "enterprise_id",
        "category_id", "tz",
    )
    for key in required:
        if not params.get(key):
            raise ValueError(f"system_a: missing param ({key})")

    if "/" in params["host"] or " " in params["host"]:
        raise ValueError("system_a: invalid host")

    # Lazy import — Playwright is a heavy dep, only needed for this checker.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("playwright not installed") from None

    checkin = _dt.date.fromisoformat(target["checkin"])
    nights = int(target["nights"])
    end = checkin + _dt.timedelta(days=nights)

    tz = ZoneInfo(params["tz"])
    start_utc = _dt.datetime.combine(checkin, _dt.time.min, tzinfo=tz).astimezone(_dt.timezone.utc)
    end_utc = _dt.datetime.combine(end, _dt.time.min, tzinfo=tz).astimezone(_dt.timezone.utc)

    captured = {"session": None, "client": None}

    def on_request(req):
        if (
            "getAvailability" in req.url
            and req.method == "POST"
            and not captured["session"]
        ):
            try:
                body = json.loads(req.post_data or "{}")
                captured["session"] = body.get("session")
                captured["client"] = body.get("client")
            except (json.JSONDecodeError, AttributeError):
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=USER_AGENT)
        page = ctx.new_page()
        page.on("request", on_request)

        try:
            page.goto(
                params["booking_url"],
                wait_until="domcontentloaded",
                timeout=BROWSER_TIMEOUT_MS // 2,
            )
        except Exception:
            # Even if the page goto times out, we may still capture a request.
            pass
        page.wait_for_timeout(4000)

        try:
            page.locator(params["trigger_selector"]).first.click(timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(8000)

        if not captured["session"] or not captured["client"]:
            browser.close()
            raise RuntimeError("session not captured")

        body = {
            "serviceId": params["service_id"],
            "bookingEngineId": params["booking_engine_id"],
            "enterpriseId": params["enterprise_id"],
            "startUtc": start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "endUtc": end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "fullAmounts": False,
            "languageCode": "en-US",
            "client": captured["client"],
            "session": captured["session"],
        }
        url = f"https://{params['host']}/api/bookingEngine/v1/services/getAvailability"

        try:
            result = page.evaluate(
                """
                async ({url, body}) => {
                  const r = await fetch(url, {
                    method: 'POST',
                    headers: {
                      'Content-Type': 'application/json',
                      'Accept': 'application/json',
                      'X-Accept-Casing': 'Camel',
                      'X-Mews-CorrelationId': crypto.randomUUID(),
                    },
                    body: JSON.stringify(body),
                  });
                  return { status: r.status, body: await r.text() };
                }
                """,
                {"url": url, "body": body},
            )
        except Exception:
            browser.close()
            raise RuntimeError("evaluate failed") from None

        browser.close()

    if result.get("status") != 200:
        raise RuntimeError(f"http {result.get('status')}")

    try:
        data = json.loads(result.get("body") or "")
    except json.JSONDecodeError:
        raise RuntimeError("non-JSON response") from None

    return _parse_system_a_response(data, requested, params["category_id"])


def _parse_system_a_response(data: object, requested_dates: list[str], category_id: str) -> frozenset[str]:
    """Response shape:

      {
        "timeUnitStartsUtc": ["2026-08-01T06:00:00Z", ...],
        "categoryAvailabilities": [
          {"categoryId": "<uuid>", "availabilities": [3, 0, 1, ...]},
          ...
        ],
      }
    """
    if not isinstance(data, dict):
        raise RuntimeError("response not an object")

    units = data.get("timeUnitStartsUtc")
    cats = data.get("categoryAvailabilities")
    if not isinstance(units, list) or not isinstance(cats, list):
        raise RuntimeError("missing timeUnitStartsUtc or categoryAvailabilities")

    target_cat = next(
        (c for c in cats if isinstance(c, dict) and c.get("categoryId") == category_id),
        None,
    )
    if target_cat is None:
        raise RuntimeError("target category not in response")

    avs = target_cat.get("availabilities")
    if not isinstance(avs, list):
        raise RuntimeError("availabilities not a list")

    requested_set = set(requested_dates)
    open_dates: set[str] = set()
    for i, unit in enumerate(units):
        if not isinstance(unit, str) or i >= len(avs):
            continue
        iso_date = unit[:10]
        n = avs[i]
        if iso_date in requested_set and isinstance(n, int) and n > 0:
            open_dates.add(iso_date)

    return frozenset(open_dates)


# ---------------------------------------------------------------------------
# system_b — Going-To-Camp / Aspira-style availability map. Plain HTTP works.
# ---------------------------------------------------------------------------

def check_system_b(target: dict) -> frozenset[str]:
    """Going-To-Camp / Aspira /api/availability/map endpoint.

    Required `target['params']`:
      - host: API host
      - map_id: int
      - resource_location_id: int
      - equipment_category_id: int

    Optional:
      - resource_id: int — if set, only this specific resource (e.g. one
        campground within a multi-campground trail) counts as "open".
        If unset, any resource at the location with availability counts.

    Queries one night at a time so the result is per-night even on
    deployments (e.g. BC Parks) that return aggregate availability per
    resource without explicit per-date entries.
    """
    requested = requested_dates(target)
    if not requested:
        return frozenset()

    params = target.get("params") or {}
    for key in ("host", "map_id", "resource_location_id", "equipment_category_id"):
        if key not in params:
            raise ValueError(f"system_b: missing param ({key})")

    host = params["host"]
    if not isinstance(host, str) or "/" in host or " " in host:
        raise ValueError("system_b: invalid host")

    open_dates: set[str] = set()
    for i, date_iso in enumerate(requested):
        if i:
            time.sleep(NIGHT_PROBE_DELAY_SECONDS)
        if _system_b_night_open(date_iso, host, params, target):
            open_dates.add(date_iso)
    return frozenset(open_dates)


def _system_b_night_open(date_iso: str, host: str, params: dict, target: dict) -> bool:
    """One-night availability probe."""
    night = _dt.date.fromisoformat(date_iso)
    next_day = night + _dt.timedelta(days=1)

    qs = urllib.parse.urlencode({
        "mapId": int(params["map_id"]),
        "resourceLocationId": int(params["resource_location_id"]),
        "startDate": night.isoformat(),
        "endDate": next_day.isoformat(),
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
        raise RuntimeError(f"http {e.code}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"network ({type(e.reason).__name__})") from None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("non-JSON response") from None

    return _system_b_response_indicates_open(data, params)


def _any_open(entries: object) -> bool:
    """Availability code 0 means bookable; every other code means it isn't."""
    if not isinstance(entries, list):
        return False
    return any(isinstance(e, dict) and e.get("availability") == 0 for e in entries)


def _system_b_response_indicates_open(data: object, params: dict) -> bool:
    """True if the requested night is bookable.

    Three shapes occur in the wild:

      - Leaf maps list individual sites under `resourceAvailabilities`.
      - Some campgrounds are a *parent* map over per-loop child maps. They
        return an empty `resourceAvailabilities` and report availability in
        `mapAvailabilities` / `mapLinkAvailabilities` instead. Reading only
        the resource field would report such a site as never available.
      - `mapAvailabilities` is a bare list of codes, not dicts.
    """
    if not isinstance(data, dict):
        raise RuntimeError("response not an object")

    resources = data.get("resourceAvailabilities")
    map_levels = data.get("mapAvailabilities")
    map_links = data.get("mapLinkAvailabilities")

    if (
        not isinstance(resources, dict)
        and not isinstance(map_levels, list)
        and not isinstance(map_links, dict)
    ):
        raise RuntimeError("no availability fields in response")

    filter_id = params.get("resource_id")
    if filter_id is not None:
        # Precise mode: only the named resource counts. Deliberately no
        # fallback to map-level signals — those aggregate every sibling,
        # which would defeat the point of filtering to one campground.
        if not isinstance(resources, dict) or not resources:
            raise RuntimeError("resource_id set but no resources returned")
        key = str(filter_id)
        if key not in resources:
            # A typo'd resource_id would otherwise look like "never open"
            # forever. Fail loudly instead.
            raise RuntimeError("resource_id not present in response")
        return _any_open(resources[key])

    if isinstance(resources, dict) and any(_any_open(v) for v in resources.values()):
        return True
    if isinstance(map_levels, list) and any(v == 0 for v in map_levels):
        return True
    if isinstance(map_links, dict):
        return any(
            isinstance(v, list) and any(code == 0 for code in v)
            for v in map_links.values()
        )
    return False

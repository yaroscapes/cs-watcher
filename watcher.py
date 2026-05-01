"""Campsite availability watcher — entry point.

Runs on a GitHub Actions cron. Reads target list and ntfy topic from
environment (sourced from GitHub Secrets). For each target, calls the
matching checker, which returns the set of dates within the requested
window that are currently available.

Notifications (all via ntfy.sh):
  - High priority push: one or more dates are *newly* available (not
    seen in the previous run for this target). Notification specifies
    full vs partial match and which specific dates opened.
  - Default priority push: this run hit errors AND the previous run was
    clean. Deduped via state so a sustained outage = one push, not 84.

Logging policy (this is a public repo — Actions logs are world-readable):
  - Print only generic status: "Target N: <state>".
  - Never print target names, dates, party sizes, URLs, response bodies,
    secret values, or stack traces.
  - On exceptions, print only the exception class name.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

from checkers import check_target
from notify import notify, notify_error


STATE_FILE = Path(".state.json")


def _load_state() -> dict:
    """Load previous-run state. Empty/default on first run / parse fail."""
    default = {"availability": {}, "had_errors": False}
    if not STATE_FILE.exists():
        return default
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
    if not isinstance(data, dict):
        return default
    avail = data.get("availability")
    if not isinstance(avail, dict):
        avail = {}
    # Each target's availability is now a list[str] of ISO dates.
    # Coerce legacy bool format (from earlier scaffold) to empty list.
    cleaned: dict[str, list[str]] = {}
    for key, val in avail.items():
        if isinstance(val, list) and all(isinstance(x, str) for x in val):
            cleaned[key] = sorted(set(val))
        else:
            cleaned[key] = []
    return {"availability": cleaned, "had_errors": bool(data.get("had_errors", False))}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        print("warning: could not persist state")


def _target_key(target: dict) -> str:
    """Stable identity for state-tracking. Never logged."""
    return "|".join(
        str(target.get(k, "")) for k in ("system", "name", "checkin", "nights", "party_size")
    )


def _validate_target(target: dict, idx: int) -> bool:
    if not isinstance(target, dict):
        print(f"Target {idx}: invalid (not an object)")
        return False
    required = ("system", "name", "checkin", "nights")
    for key in required:
        if key not in target:
            print(f"Target {idx}: invalid (missing required field)")
            return False
    if target["system"] not in ("system_a", "system_b"):
        print(f"Target {idx}: invalid (unknown system)")
        return False
    try:
        _dt.date.fromisoformat(str(target["checkin"]))
    except ValueError:
        print(f"Target {idx}: invalid (bad checkin date)")
        return False
    try:
        if int(target["nights"]) < 1:
            raise ValueError
    except (TypeError, ValueError):
        print(f"Target {idx}: invalid (bad nights)")
        return False
    return True


def _requested_dates(target: dict) -> list[str]:
    checkin = _dt.date.fromisoformat(target["checkin"])
    nights = int(target["nights"])
    return [(checkin + _dt.timedelta(days=i)).isoformat() for i in range(nights)]


def main() -> int:
    raw = os.environ.get("TARGETS_JSON", "").strip()
    if not raw:
        print("ERROR: TARGETS_JSON not set", file=sys.stderr)
        return 1

    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        print("ERROR: TARGETS_JSON is not valid JSON", file=sys.stderr)
        return 1

    if not isinstance(targets, list) or not targets:
        print("ERROR: TARGETS_JSON must be a non-empty array", file=sys.stderr)
        return 1

    if not os.environ.get("NTFY_TOPIC", "").strip():
        print("ERROR: NTFY_TOPIC not set", file=sys.stderr)
        return 1

    print(f"Checking {len(targets)} target(s)...")

    prev_state = _load_state()
    prev_avail = prev_state["availability"]
    prev_had_errors = prev_state["had_errors"]

    new_avail: dict[str, list[str]] = {}
    new_hits = 0
    error_summaries: list[str] = []

    for idx, target in enumerate(targets, start=1):
        if not _validate_target(target, idx):
            error_summaries.append(f"target {idx} (invalid config)")
            continue

        key = _target_key(target)
        prev_dates_set = set(prev_avail.get(key, []))

        try:
            available = check_target(target)
        except Exception as e:
            cls = type(e).__name__
            print(f"Target {idx}: check failed ({cls})")
            error_summaries.append(f"target {idx} ({cls})")
            # On error, keep previous state so a transient failure doesn't
            # make recovery look like all-new availability next run.
            new_avail[key] = sorted(prev_dates_set)
            continue

        avail_set = set(available)
        newly_open = sorted(avail_set - prev_dates_set)
        new_avail[key] = sorted(avail_set)

        if not avail_set:
            print(f"Target {idx}: no availability")
            continue

        if not newly_open:
            print(f"Target {idx}: {len(avail_set)} night(s) available (already notified)")
            continue

        # New availability — notify.
        try:
            requested = _requested_dates(target)
            notify(
                target,
                newly_available=newly_open,
                total_available=sorted(avail_set),
                requested=requested,
            )
            new_hits += 1
            print(
                f"Target {idx}: AVAILABLE — notified "
                f"({len(newly_open)} new, {len(avail_set)} total)"
            )
        except Exception as e:
            cls = type(e).__name__
            print(f"Target {idx}: AVAILABLE but notify failed ({cls})")
            error_summaries.append(f"target {idx} notify ({cls})")
            # Roll back state for the newly-open dates so we retry next run.
            new_avail[key] = sorted(avail_set - set(newly_open))

    has_errors = bool(error_summaries)

    # Error notification: only on clean -> error transition.
    if has_errors and not prev_had_errors:
        summary = f"{len(error_summaries)} issue(s): " + ", ".join(error_summaries)
        try:
            notify_error(summary)
            print("Error notification sent")
        except Exception as e:
            print(f"Error notification failed ({type(e).__name__})")

    _save_state({"availability": new_avail, "had_errors": has_errors})

    print(
        f"Done. {new_hits} availability notification(s), "
        f"{len(error_summaries)} error(s)."
    )
    return 0 if not has_errors else 1


if __name__ == "__main__":
    sys.exit(main())

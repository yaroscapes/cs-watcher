"""Campsite availability watcher — entry point.

Runs on a GitHub Actions cron. Reads target list and ntfy topic from
environment (sourced from GitHub Secrets). For each target, calls the
matching checker.

Notifications (all via ntfy.sh):
  - High priority push: a target transitions from unavailable -> available.
  - Default priority push: this run hit errors AND the previous run was
    clean. Deduped via state so a sustained outage = one push, not 84.
    On recovery + new error you'll get notified again.

Logging policy (this is a public repo — Actions logs are world-readable):
  - Print only generic status: "Target N: <state>".
  - Never print target names, dates, party sizes, URLs, response bodies,
    secret values, or stack traces.
  - On exceptions, print only the exception class name.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from checkers import check_target
from notify import notify, notify_error


STATE_FILE = Path(".state.json")


def _load_state() -> dict:
    """Load the previous-run state. Empty/default on first run or parse
    failure — that's fine, we just re-notify any current hits."""
    default = {"availability": {}, "had_errors": False}
    if not STATE_FILE.exists():
        return default
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
    if not isinstance(data, dict):
        return default
    # Backwards-compat: older runs stored a flat {key: bool} map.
    if "availability" not in data:
        return {"availability": data, "had_errors": False}
    avail = data.get("availability", {})
    if not isinstance(avail, dict):
        avail = {}
    return {"availability": avail, "had_errors": bool(data.get("had_errors", False))}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        # State persistence is best-effort. If it fails we'll just
        # re-notify on the next run — annoying but not broken.
        print("warning: could not persist state")


def _target_key(target: dict) -> str:
    """Stable identity for state-tracking. Never logged."""
    return "|".join(
        str(target.get(k, "")) for k in ("system", "name", "checkin", "nights", "party_size")
    )


def _validate_target(target: dict, idx: int) -> bool:
    """Reject anything that doesn't look like a target. Don't echo
    contents — just say which index is bad."""
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
    return True


def main() -> int:
    raw = os.environ.get("TARGETS_JSON", "").strip()
    if not raw:
        print("ERROR: TARGETS_JSON not set", file=sys.stderr)
        return 1

    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        # Don't print the parse error — could echo secret JSON.
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

    new_avail: dict[str, bool] = {}
    new_hits = 0
    error_summaries: list[str] = []  # generic strings, safe to echo to ntfy

    for idx, target in enumerate(targets, start=1):
        if not _validate_target(target, idx):
            error_summaries.append(f"target {idx} (invalid config)")
            continue

        try:
            available = check_target(target)
        except Exception as e:
            cls = type(e).__name__
            print(f"Target {idx}: check failed ({cls})")
            error_summaries.append(f"target {idx} ({cls})")
            continue

        key = _target_key(target)
        was_available = prev_avail.get(key, False)
        new_avail[key] = available

        if available and not was_available:
            try:
                notify(target)
                new_hits += 1
                print(f"Target {idx}: AVAILABLE — notification sent")
            except Exception as e:
                cls = type(e).__name__
                print(f"Target {idx}: AVAILABLE but notify failed ({cls})")
                error_summaries.append(f"target {idx} notify ({cls})")
                # Keep state as not-yet-notified so we retry next run.
                new_avail[key] = False
        elif available:
            print(f"Target {idx}: still available (already notified)")
        else:
            print(f"Target {idx}: no availability")

    has_errors = bool(error_summaries)

    # Error notification: only on clean -> error transition. Avoids
    # 84 pushes/day if a target site is down for a few hours.
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
    # Non-zero exit on errors so the run shows red and GitHub's
    # workflow-failure email kicks in too (belt and suspenders).
    return 0 if not has_errors else 1


if __name__ == "__main__":
    sys.exit(main())

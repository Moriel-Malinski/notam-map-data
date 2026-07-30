"""Writes one timestamp field into docs/heartbeat.json, preserving the others.

The app reads this file to distinguish two different questions:

  lastRun        — when the daily source pipeline last got far enough to say
                   "the sources were checked". Written by update-data.yml.
  lastHeartbeat  — an hourly liveness pulse written by heartbeat.yml. Moves
                   even when the pipeline crashed or found nothing new, so a
                   frozen lastRun can be told apart from a dead scheduler.

Both writers go through this script so neither clobbers the other's field —
the previous shell one-liner rewrote the whole file with a single key.

Usage:
    python pipeline/write_heartbeat.py lastRun
    python pipeline/write_heartbeat.py lastHeartbeat
"""
import argparse
import json
import os
from datetime import datetime, timezone

from common import DOCS_DIR

FIELDS = ("lastRun", "lastHeartbeat")

HEARTBEAT_PATH = os.path.join(DOCS_DIR, "heartbeat.json")


def write_field(field, now=None):
    """Sets `field` to now (UTC, second precision) and returns the new state."""
    if field not in FIELDS:
        raise ValueError(f"unknown field {field!r}; expected one of {FIELDS}")

    state = {}
    if os.path.exists(HEARTBEAT_PATH):
        try:
            with open(HEARTBEAT_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, ValueError):
            # A corrupt or truncated heartbeat is not worth failing a run over;
            # rewriting it from scratch loses at most the sibling timestamp.
            print(f"WARN: {HEARTBEAT_PATH} unreadable; rewriting from scratch")

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state[field] = stamp

    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(HEARTBEAT_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("field", choices=FIELDS,
                        help="which timestamp to set to now")
    args = parser.parse_args()
    state = write_field(args.field)
    print(f"{args.field}={state[args.field]} "
          f"(kept: {', '.join(k for k in sorted(state) if k != args.field) or 'nothing'})")


if __name__ == "__main__":
    main()

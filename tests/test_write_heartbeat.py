import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
)

import pytest  # noqa: E402

import write_heartbeat  # noqa: E402


@pytest.fixture
def heartbeat(tmp_path, monkeypatch):
    """Redirects the writer at a temp file instead of the real docs/."""
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(write_heartbeat, "HEARTBEAT_PATH", str(path))
    monkeypatch.setattr(write_heartbeat, "DOCS_DIR", str(tmp_path))
    return path


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_creates_file_when_missing(heartbeat):
    write_heartbeat.write_field("lastRun")
    assert set(read(heartbeat)) == {"lastRun"}


def test_preserves_the_sibling_field(heartbeat):
    """The whole point: neither writer may clobber the other's timestamp."""
    write_heartbeat.write_field(
        "lastRun", now=datetime(2026, 7, 29, 6, 8, 23, tzinfo=timezone.utc)
    )
    write_heartbeat.write_field(
        "lastHeartbeat", now=datetime(2026, 7, 30, 10, 0, 0, tzinfo=timezone.utc)
    )
    assert read(heartbeat) == {
        "lastRun": "2026-07-29T06:08:23Z",
        "lastHeartbeat": "2026-07-30T10:00:00Z",
    }

    # And the daily run must not wipe the hourly pulse on its next write.
    write_heartbeat.write_field(
        "lastRun", now=datetime(2026, 7, 30, 3, 20, 0, tzinfo=timezone.utc)
    )
    state = read(heartbeat)
    assert state["lastRun"] == "2026-07-30T03:20:00Z"
    assert state["lastHeartbeat"] == "2026-07-30T10:00:00Z"


def test_timestamp_format_matches_what_the_app_parses(heartbeat):
    write_heartbeat.write_field(
        "lastRun", now=datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=timezone.utc)
    )
    # Second precision, explicit Z — DateTime.tryParse in the app needs both.
    assert read(heartbeat)["lastRun"] == "2026-01-02T03:04:05Z"


def test_corrupt_file_is_rewritten_not_fatal(heartbeat):
    heartbeat.write_text("{truncated", encoding="utf-8")
    write_heartbeat.write_field("lastHeartbeat")
    assert set(read(heartbeat)) == {"lastHeartbeat"}


def test_non_dict_json_is_discarded(heartbeat):
    heartbeat.write_text("[1, 2, 3]", encoding="utf-8")
    write_heartbeat.write_field("lastRun")
    assert set(read(heartbeat)) == {"lastRun"}


def test_unknown_field_is_rejected(heartbeat):
    with pytest.raises(ValueError):
        write_heartbeat.write_field("lastPotato")
    assert not os.path.exists(heartbeat)

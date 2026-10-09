import copy
import json
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
)

import fetch_a17  # noqa: E402
from fetch_a17 import dms_to_dec, parse_segment  # noqa: E402

# Annex B circle row (radius spelled "רדיוסו100 ... מטר", altitudes present).
SEG_CIRCLE_METERS = """ תחנת
אתגל
1500
GND מעגל ש
רדיוסו100

:מטר ומרכזו בנקודה
34° 39' 32.76" E
31° 50' 16.80" N
"""

# Annex B circle with a worded radius ("חצי ק"מ").
SEG_CIRCLE_HALF_KM = """ הר קרן
3000
GND
מעגל שרדיוסו חצי
:ק"מ ומרכזו בנקודה
34° 29' 30.00" E
30° 59' 37.00" N
"""

# Annex C row: the bare number is a RADIUS in meters, not an altitude, and
# the stray "433" belongs to the name (להב 433).
SEG_ANNEX_C = """ להב
 433

150
 מטר
34°53'56"E
31°58'02"N
"""

# Regular polygon row.
SEG_POLYGON = """ פלוגות
1100
GND
34° 45' 27.49" E
31° 37' 45.25" N
34° 45' 30.20" E
31° 38' 32.21" N
34° 46' 00.00" E
31° 38' 00.00" N
"""


def test_dms_to_dec():
    assert dms_to_dec("31", "37", "45.25", "N") == 31.629236
    assert dms_to_dec("34", "45", "27.49", "E") == 34.757636


def test_circle_radius_in_meters():
    z = parse_segment("LLP44", SEG_CIRCLE_METERS)
    assert z["shape"] == "circle"
    assert z["radius_km"] == 0.1
    assert z["max_alt_ft"] == 1500
    assert z["min_alt_ft"] == 0
    assert z["center"] == [31.838, 34.659100]


def test_circle_radius_worded_half_km():
    z = parse_segment("LLP23", SEG_CIRCLE_HALF_KM)
    assert z["shape"] == "circle"
    assert z["radius_km"] == 0.5
    assert z["max_alt_ft"] == 3000


def test_annex_c_number_is_radius_not_altitude():
    z = parse_segment("LLU38", SEG_ANNEX_C)
    assert z["shape"] == "circle"
    assert z["radius_km"] == 0.15
    # no altitude column in annex C — must not swallow "433" as an altitude
    assert z["max_alt_ft"] == 0


def test_polygon_row():
    z = parse_segment("LLP41", SEG_POLYGON)
    assert z["shape"] == "polygon"
    assert len(z["coords"]) == 3
    assert z["max_alt_ft"] == 1100
    assert z["coords"][0] == [31.629236, 34.757636]


def _zone(code, i):
    lat = 31.0 + i * 0.01
    return {"code": code, "shape": "polygon",
            "coords": [[lat, 34.8], [lat, 34.81], [lat + 0.005, 34.805]],
            "center": None, "radius_km": None,
            "max_alt_ft": 1000, "min_alt_ft": 0}


# Codes the PDF yields: 20 managed in zones_data.json, 12 in the additions.
BASE = [_zone(f"LLP{i}", i) for i in range(1, 21)]
ADDITIONS = [_zone(f"LLU{i}", 30 + i) for i in range(1, 13)]
# Hand-curated: lives in the additions, never parses out of the PDF.
HAND = _zone("LLU_HAND", 99)
ALL_CODES = [z["code"] for z in BASE + ADDITIONS]


@pytest.fixture
def a17(tmp_path, monkeypatch):
    """Runs fetch_a17.main() --apply --guard against a temp data dir, with
    the PDF scan replaced by the given list of parsed codes."""
    with open(tmp_path / "zones_data.json", "w", encoding="utf-8") as f:
        json.dump(BASE, f)
    with open(tmp_path / "a17_additions.json", "w", encoding="utf-8") as f:
        json.dump(ADDITIONS + [HAND], f)
    monkeypatch.setattr(fetch_a17, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_a17, "REPO_ROOT", str(tmp_path))
    issues = []
    monkeypatch.setattr(fetch_a17, "open_github_issue",
                        lambda title, body: issues.append(title))
    monkeypatch.setattr(sys, "argv",
                        ["fetch_a17.py", "--apply", "--guard", "--pdf", "x.pdf"])
    by_code = {z["code"]: z for z in BASE + ADDITIONS}

    class Env:
        path = tmp_path

        def set_state(self, state):
            with open(tmp_path / ".a17_guard_state.json", "w",
                      encoding="utf-8") as f:
                json.dump(state, f)

        def state(self):
            with open(tmp_path / ".a17_guard_state.json", encoding="utf-8") as f:
                return json.load(f)

        def scan(self, codes):
            monkeypatch.setattr(
                fetch_a17, "extract_zones",
                lambda _: {c: copy.deepcopy(by_code[c]) for c in codes})
            fetch_a17.main()

    env = Env()
    env.issues = issues
    env.set_state({"lastParsed": sorted(ALL_CODES),
                   "baselineMissing": ["LLU_HAND"]})
    return env


def test_full_scan_opens_nothing(a17):
    a17.scan(ALL_CODES)
    assert a17.issues == []
    assert a17.state()["lastParsed"] == sorted(ALL_CODES)


def test_scan_that_only_loses_zones_trips_the_guard(a17):
    # The scan stopped early: 12 codes gone, nothing added or changed. This
    # used to exit as "no changes" and fold the lost codes into the baseline.
    before = (a17.path / "a17_additions.json").read_text(encoding="utf-8")
    a17.scan(ALL_CODES[12:])
    assert len(a17.issues) == 1
    assert a17.state()["lastParsed"] == sorted(ALL_CODES)  # not absorbed
    assert (a17.path / "a17_additions.json").read_text(encoding="utf-8") == before

    a17.scan(ALL_CODES[12:])  # same broken scan next run
    assert len(a17.issues) == 1


def test_small_loss_alerts_once_and_is_accepted(a17):
    lost = ["LLP1", "LLP2"]
    a17.scan([c for c in ALL_CODES if c not in lost])
    assert len(a17.issues) == 1
    assert set(lost).isdisjoint(a17.state()["lastParsed"])

    # The same two flicker back and vanish again: no repeat alert.
    a17.scan(ALL_CODES)
    a17.scan([c for c in ALL_CODES if c not in lost])
    assert len(a17.issues) == 1


def test_state_without_snapshot_falls_back_to_curated_baseline(a17):
    # The committed state file predates lastParsed.
    a17.set_state({"baselineMissing": ["LLU_HAND"]})
    lost = [f"LLU{i}" for i in range(1, 12)]
    a17.scan([c for c in ALL_CODES if c not in lost])
    assert len(a17.issues) == 1
    assert "lastParsed" not in a17.state()

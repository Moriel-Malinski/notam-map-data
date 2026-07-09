import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
)

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

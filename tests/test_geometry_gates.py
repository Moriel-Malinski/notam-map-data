"""Tests for the meaning-level gates.

Each case is modelled on a real airspace error these gates were written to
catch, so a regression here means that class of bug can reach a published
file again.
"""
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
)

import geometry_gates  # noqa: E402


def keys(findings):
    return {f.key for f in findings}


def circle(code, lat, lon, r, **extra):
    z = {"code": code, "shape": "circle", "center": [lat, lon], "radius_km": r,
         "coords": [], "notes": "", "name_he": ""}
    z.update(extra)
    return z


def polygon(code, coords, **extra):
    z = {"code": code, "shape": "polygon", "coords": [list(c) for c in coords],
         "center": None, "radius_km": None, "notes": "", "name_he": ""}
    z.update(extra)
    return z


SQUARE = [(31.00, 34.00), (31.00, 34.05), (31.05, 34.05), (31.05, 34.00)]
# Same four corners, but visiting them in the order a table lists them —
# this is the shape LLU_ESHKOL was actually published as.
BOWTIE = [(31.00, 34.00), (31.05, 34.05), (31.00, 34.05), (31.05, 34.00)]


def test_same_code_in_both_files_is_flagged():
    # LLU_NATAF was in both files; the merge dropped the larger official copy.
    found = geometry_gates.run({
        "zones_data.json": [circle("LLU_X", 31.0, 34.0, 1.0)],
        "a17_additions.json": [circle("LLU_X", 31.0, 34.0, 1.5)],
    })
    assert "dup-code:LLU_X" in keys(found)


def test_distinct_codes_are_not_flagged_as_duplicates():
    found = geometry_gates.run({
        "zones_data.json": [circle("LLU_A", 31.0, 34.0, 1.0)],
        "a17_additions.json": [circle("LLU_B", 32.0, 35.0, 1.0)],
    })
    assert not any(k.startswith("dup-code") for k in keys(found))


def test_identical_geometry_under_two_codes_is_flagged():
    # LLU_TAIBE / LLU_TAYIBE — one area, two codes, both drawn.
    found = geometry_gates.run({
        "zones_data.json": [circle("LLU_TAIBE", 32.2539, 35.0322, 1.5)],
        "a17_additions.json": [circle("LLU_TAYIBE", 32.2539, 35.0322, 1.5)],
    })
    assert "same-geom:LLU_TAIBE|LLU_TAYIBE" in keys(found)


def test_corridor_text_with_circle_geometry_is_flagged():
    # קרן: "גליל ... בין הנ"צים הבאים" stored as a circle on one endpoint.
    found = geometry_gates.run({
        "zones_data.json": [
            circle("LLU_C", 31.2, 34.4, 3.704,
                   notes='גליל ברדיוס 2 מייל ימי בין הנ"צים הבאים.')
        ],
    })
    assert "shape-vs-text:LLU_C" in keys(found)


def test_parachuting_cylinder_is_not_flagged():
    # "גליל הצנחה" really is a circle in plan view — the word alone must not
    # trip the gate, only "between the coordinates" does.
    found = geometry_gates.run({
        "zones_data.json": [
            circle("LLD99", 32.9, 35.5, 2.5, name_he="גליל הצנחה מחניים")
        ],
    })
    assert not any(k.startswith("shape-vs-text") for k in keys(found))


def test_partial_circle_text_is_flagged():
    # LLD28 "חצי מעגל צפוני" stored as a whole circle.
    found = geometry_gates.run({
        "zones_data.json": [circle("LLD28", 32.0, 34.8, 3.0, notes="חצי מעגל צפוני")],
    })
    assert "shape-vs-text:LLD28" in keys(found)


def test_self_intersecting_outline_is_flagged():
    found = geometry_gates.run({"zones_data.json": [polygon("LLP16", BOWTIE)]})
    assert "self-intersect:LLP16" in keys(found)


def test_simple_outline_is_not_flagged():
    found = geometry_gates.run({"zones_data.json": [polygon("LLP17", SQUARE)]})
    assert not any(k.startswith("self-intersect") for k in keys(found))


def test_line_stored_as_area_is_flagged():
    # LLU_KEREN_SEP: the north/south separation LINE saved as a 3-point area.
    line = [(30.98944, 34.55333), (30.98653, 34.51445), (30.98361, 34.47556)]
    found = geometry_gates.run({"zones_data.json": [polygon("LLU_SEP", line)]})
    assert "degenerate-poly:LLU_SEP" in keys(found)


def test_real_corridor_polygon_passes_every_gate():
    # LLU_ESHKOL as it should be: a 2 km corridor, cut at one end.
    eshkol = [
        (31.231389, 34.421111), (31.189061, 34.316402), (31.183083, 34.308072),
        (31.174345, 34.304354), (31.165186, 34.306242), (31.158060, 34.313230),
        (31.154880, 34.323446), (31.156495, 34.334154), (31.198889, 34.439167),
    ]
    found = geometry_gates.run({"zones_data.json": [polygon("LLU_ESHKOL", eshkol)]})
    assert keys(found) == set()

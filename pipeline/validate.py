"""Validation gates for the published data files.

Run before build_manifest.py. Exits non-zero (and prints a readable report)
when any gate fails, so the workflow never publishes bad data — the app keeps
serving the last good version.

Usage:
    python pipeline/validate.py [--allow-shrink]
"""
import argparse
import os
import sys

from common import DATA_DIR, DOCS_DIR, MANAGED_FILES, in_israel_bbox, load_json

# name -> (min_count, max_count)
COUNT_BOUNDS = {
    "zones_data.json": (200, 500),
    "a17_additions.json": (40, 200),
    "ratag_output.json": (400, 2000),
    "rtg_closures.json": (300, 900),
}

# Files whose entries are Zone objects the app feeds to Zone.fromJson.
ZONE_FILES = {"zones_data.json", "a17_additions.json"}

VALID_SHAPES = {"polygon", "circle", "polyline", "point", "linestring"}

errors = []


def err(msg):
    errors.append(msg)


def check_coord(name, code, lat, lon):
    if not in_israel_bbox(lat, lon):
        err(f"{name}: {code}: coordinate ({lat}, {lon}) outside Israel bbox")


def validate_zone_file(name, entries):
    codes = set()
    for i, z in enumerate(entries):
        code = str(z.get("code", "")).strip().upper()
        if not code:
            err(f"{name}: entry #{i} has empty code")
            continue
        if code in codes:
            err(f"{name}: duplicate code {code}")
        codes.add(code)

        shape = str(z.get("shape", "polygon")).lower()
        if shape not in VALID_SHAPES:
            err(f"{name}: {code}: unknown shape '{shape}'")
        coords = z.get("coords") or []
        center = z.get("center")
        if shape == "polygon" and len(coords) < 3:
            err(f"{name}: {code}: polygon with {len(coords)} points")
        if shape == "circle" and not center:
            err(f"{name}: {code}: circle without center")
        for pt in coords:
            check_coord(name, code, float(pt[0]), float(pt[1]))
        if center:
            check_coord(name, code, float(center[0]), float(center[1]))
        if not isinstance(z.get("max_alt_ft", 0), int):
            err(f"{name}: {code}: max_alt_ft is not an integer")


def validate_ratag_output(name, entries):
    for i, item in enumerate(entries):
        label = item.get("code") or f"entry #{i}"
        if not item.get("name"):
            err(f"{name}: {label}: missing name")
        shape = str(item.get("shape", "")).lower()
        if shape not in VALID_SHAPES:
            err(f"{name}: {label}: unknown shape '{shape}'")
        coords = item.get("coords") or []
        if not coords:
            err(f"{name}: {label}: no coordinates")
        for pt in coords:
            check_coord(name, label, float(pt[0]), float(pt[1]))


def validate_rtg_closures(name, entries):
    for i, c in enumerate(entries):
        if not c.get("name"):
            err(f"{name}: entry #{i} missing name")
        if "maxAltFt" in c and not isinstance(c["maxAltFt"], int):
            err(f"{name}: entry #{i} maxAltFt not an integer")


def validate_counts(name, entries, previous_counts, allow_shrink):
    lo, hi = COUNT_BOUNDS[name]
    n = len(entries)
    if not (lo <= n <= hi):
        err(f"{name}: count {n} outside sane bounds [{lo}, {hi}]")

    prev = previous_counts.get(name)
    if prev:
        if name == "ratag_output.json" and abs(n - prev) > prev * 0.2:
            err(
                f"{name}: count moved {prev} -> {n} (more than 20%); "
                "pass --allow-shrink if intentional"
                if not allow_shrink
                else ""
            )
        elif name in ZONE_FILES and n < prev * 0.9 and not allow_shrink:
            err(
                f"{name}: count shrank {prev} -> {n} (more than 10%); "
                "pass --allow-shrink if intentional"
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-shrink", action="store_true")
    args = parser.parse_args()

    # Previous per-file counts from the committed manifest (delta gates).
    previous_counts = {}
    manifest_path = os.path.join(DOCS_DIR, "manifest.json")
    if os.path.exists(manifest_path):
        manifest = load_json(manifest_path)
        for fname, info in (manifest.get("files") or {}).items():
            if isinstance(info.get("count"), int):
                previous_counts[fname] = info["count"]

    for name in MANAGED_FILES:
        path = os.path.join(DATA_DIR, name)
        if not os.path.exists(path):
            err(f"{name}: file missing from data/")
            continue
        try:
            entries = load_json(path)
        except Exception as e:  # noqa: BLE001
            err(f"{name}: invalid JSON: {e}")
            continue
        if not isinstance(entries, list):
            err(f"{name}: top level is not a list")
            continue

        if name in ZONE_FILES:
            validate_zone_file(name, entries)
        elif name == "ratag_output.json":
            validate_ratag_output(name, entries)
        elif name == "rtg_closures.json":
            validate_rtg_closures(name, entries)
        validate_counts(name, entries, previous_counts, args.allow_shrink)

    errs = [e for e in errors if e]
    if errs:
        print(f"VALIDATION FAILED — {len(errs)} problem(s):")
        for e in errs[:100]:
            print(f"  - {e}")
        if len(errs) > 100:
            print(f"  ... and {len(errs) - 100} more")
        sys.exit(1)
    print("Validation passed for all data files.")


if __name__ == "__main__":
    main()

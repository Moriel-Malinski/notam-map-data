"""Parses the official פמ"ת א-17 PDF (restricted/prohibited/dangerous areas)
and produces a reviewed-by-PR update for data/a17_additions.json.

Best-effort by design: the PDF is a Hebrew RTL table, so parsing is heuristic
and every change goes through a pull request a human merges (the a17-refresh
workflow). This script never publishes directly.

Flow:
  1. Download the PDF (to an ASCII filename — Hebrew names break tooling).
  2. Extract zone rows: code, altitudes, polygon DMS pairs / circle radius.
  3. Merge with data/a17_additions.json, preserving hand-curated fields
     (names, contact, notes, type_he) for codes that already exist.
  4. Write the merged result + a Markdown change report.

Usage:
    python pipeline/fetch_a17.py            # writes candidate + report only
    python pipeline/fetch_a17.py --apply    # also overwrites a17_additions.json
"""
import argparse
import json
import os
import re
import sys

import fitz  # PyMuPDF
import requests

from common import DATA_DIR, REPO_ROOT, in_israel_bbox, load_json, save_json

A17_URL = os.environ.get(
    "A17_PDF_URL",
    "https://www.gov.il/BlobFolder/guide/aip/he/aip_%D7%90'-17.pdf",
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DMS_RE = re.compile(r"(\d+)°\s*(\d+)'\s*(\d+(?:\.\d+)?)\"?\s*([ENSW])")
CODE_RE = re.compile(r"\b(LL[PRDU])\s?(\d{1,4}[A-Z]?)\b")
RADIUS_RE = re.compile(r"רדיוסו\s*(חצי|\d+(?:\.\d+)?)")
ALT_LINE_RE = re.compile(r"^(GND|FL\s*\d+|\d{2,6})\s*$")

TYPE_HE = {
    "LLP": "אזור אסור",
    "LLR": "אזור מוגבל",
    "LLD": "אזור מסוכן",
    "LLU": 'אסור לכטב"מ/טיסן',
}


def dms_to_dec(deg, minutes, seconds, direction):
    val = float(deg) + float(minutes) / 60.0 + float(seconds) / 3600.0
    if direction in ("S", "W"):
        val = -val
    return round(val, 6)


def parse_alt(token):
    token = token.strip()
    if token == "GND":
        return 0
    if token.startswith("FL"):
        return int(token[2:].strip()) * 100
    return int(token)


def parse_segment(code, segment):
    """Extracts one zone's data from the text between its code and the next."""
    # Coordinates: the A-17 table prints E (lon) then N (lat) per point.
    pending_lon = None
    coords = []
    for m in DMS_RE.finditer(segment):
        val = dms_to_dec(m.group(1), m.group(2), m.group(3), m.group(4))
        if m.group(4) == "E":
            pending_lon = val
        elif m.group(4) == "N" and pending_lon is not None:
            coords.append([val, pending_lon])  # [lat, lon]
            pending_lon = None
    coords = [c for c in coords if in_israel_bbox(c[0], c[1])]
    if not coords:
        return None

    # Radius, wording 1 (annex B circles): "מעגל שרדיוסו X מטר/ק"מ".
    radius_km = None
    rm = RADIUS_RE.search(segment)
    if rm and "מעגל" in segment:
        r = 0.5 if rm.group(1) == "חצי" else float(rm.group(1))
        window = segment[rm.end() : rm.end() + 40]
        in_km = rm.group(1) == "חצי" or 'ק"מ' in window or "קמ" in window
        radius_km = r if in_km else r / 1000.0

    # Altitudes and radius, wording 2 (annex C rows): a bare number line whose
    # next line starts with מטר/ק"מ is a RADIUS, not an altitude.
    lines = [ln.strip() for ln in segment.splitlines()]
    alts = []
    for idx, line in enumerate(lines):
        m = ALT_LINE_RE.match(line)
        if not m:
            continue
        token = m.group(1)
        following = next((ln for ln in lines[idx + 1 : idx + 3] if ln), "")
        if not token.startswith(("GND", "FL")) and following.startswith(
            ("מטר", 'ק"מ', "קמ")
        ):
            r = float(token)
            radius_km = r / 1000.0 if following.startswith("מטר") else r
            continue
        if len(alts) < 2:
            alts.append(parse_alt(token))

    # Some rows carry no altitude column at all (annex C radius rows, several
    # airspace rows); stray numbers there belong to the zone name (e.g.
    # "להב 433"). Flag it so the merge keeps curated altitudes.
    alts_missing = False
    if radius_km is not None and "GND" not in segment:
        alts = []
    if not alts:
        alts_missing = True
    max_alt = alts[0] if alts else 0
    min_alt = alts[1] if len(alts) > 1 else 0

    # Hebrew name: first Hebrew run right after the code token.
    name_m = re.search(r"[֐-׿][֐-׿\s\"'()\-]{1,40}", segment)
    name_he = re.sub(r"\s+", " ", name_m.group(0)).strip() if name_m else code

    zone = {
        "code": code,
        "name_he": name_he,
        "name_en": code,
        "type": code[:3],
        "type_he": TYPE_HE.get(code[:3], ""),
        "max_alt_ft": max_alt,
        "min_alt_ft": min_alt,
        "shape": "circle" if radius_km else "polygon",
        "coords": [] if radius_km else coords,
        "center": coords[0] if radius_km else None,
        "radius_km": radius_km,
        "contact": "",
        "notes": 'מקור: פמ"ת א-17 (חילוץ אוטומטי — לבדוק מול המסמך)',
    }
    if zone["shape"] == "polygon" and len(coords) < 3:
        # Degenerate geometry (e.g. two-point balloon lines): represent as a
        # circle spanning the points so the zone stays visible. The radius is
        # a GUESS — the merge keeps a curated radius when one exists.
        lat = sum(c[0] for c in coords) / len(coords)
        lon = sum(c[1] for c in coords) / len(coords)
        zone.update(shape="circle", center=[round(lat, 6), round(lon, 6)],
                    radius_km=0.75, coords=coords)
        zone["_radius_guessed"] = True
    if alts_missing:
        zone["_alts_missing"] = True
    return zone


def extract_zones(pdf_path):
    doc = fitz.open(pdf_path)
    zones = {}
    for page in doc:
        text = page.get_text()
        # Only table pages: they carry many DMS coordinates.
        if len(DMS_RE.findall(text)) < 4:
            continue
        matches = list(CODE_RE.finditer(text))
        for i, m in enumerate(matches):
            code = f"{m.group(1)}{m.group(2)}"
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            segment = text[m.end() : end]
            zone = parse_segment(code, segment)
            if zone and code not in zones:
                zones[code] = zone
    return zones


def close_enough(a, b, tol=2e-5):
    return abs(a - b) <= tol


def geometry_equal(z1, z2):
    if (z1.get("shape") or "polygon") != (z2.get("shape") or "polygon"):
        return False
    c1, c2 = z1.get("coords") or [], z2.get("coords") or []
    if len(c1) != len(c2):
        return False
    for p1, p2 in zip(c1, c2):
        if not (close_enough(p1[0], p2[0]) and close_enough(p1[1], p2[1])):
            return False
    ctr1, ctr2 = z1.get("center"), z2.get("center")
    if (ctr1 is None) != (ctr2 is None):
        return False
    if ctr1 and not (
        close_enough(ctr1[0], ctr2[0]) and close_enough(ctr1[1], ctr2[1])
    ):
        return False
    r1, r2 = z1.get("radius_km"), z2.get("radius_km")
    if (r1 is None) != (r2 is None):
        return False
    if r1 is not None and abs(r1 - r2) > 0.01:
        return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="overwrite data/a17_additions.json with the merge")
    parser.add_argument("--pdf", help="use a local PDF instead of downloading")
    args = parser.parse_args()

    if args.pdf:
        pdf_path = args.pdf
    else:
        pdf_path = os.path.join(REPO_ROOT, "a17_download.pdf")  # ASCII name
        res = requests.get(
            A17_URL, headers={"User-Agent": USER_AGENT}, timeout=120
        )
        res.raise_for_status()
        with open(pdf_path, "wb") as f:
            f.write(res.content)
        print(f"Downloaded A-17 PDF ({len(res.content):,} bytes)")

    parsed = extract_zones(pdf_path)
    if len(parsed) < 20:
        print(f"ERROR: parsed only {len(parsed)} zones — layout may have "
              "changed; refusing to produce a candidate.")
        sys.exit(1)
    print(f"Parsed {len(parsed)} zones from the PDF")

    existing = load_json(os.path.join(DATA_DIR, "a17_additions.json"))
    base_zone_codes = {
        str(z["code"]).upper()
        for z in load_json(os.path.join(DATA_DIR, "zones_data.json"))
    }
    by_code = {str(z["code"]).upper(): z for z in existing}

    added, changed, missing = [], [], []
    merged = {code: dict(z) for code, z in by_code.items()}

    for code, zone in parsed.items():
        if code in base_zone_codes:
            continue  # managed in zones_data.json, not in the additions file
        old = by_code.get(code)
        if zone.pop("_radius_guessed", False) and old and old.get("radius_km"):
            # The PDF gave no radius (degenerate geometry); a curated radius
            # exists — keep it (and its center) instead of the guess.
            zone["radius_km"] = old["radius_km"]
            zone["coords"] = old.get("coords") or []
            if old.get("center"):
                zone["center"] = old["center"]
        if zone.pop("_alts_missing", False) and old:
            # Row had no altitude column; keep the curated altitudes.
            zone["max_alt_ft"] = old.get("max_alt_ft", 0)
            zone["min_alt_ft"] = old.get("min_alt_ft", 0)
        if old is not None:
            # Multi-part rows (e.g. LLU22 "מרחב אווירי 1"/"מס' 2") were split
            # by hand into sibling codes (LLU22 + LLU22B). When the parsed
            # point count equals the sum of the curated parts, keep the split.
            sibling_pts = sum(
                len(z.get("coords") or [])
                for c, z in by_code.items()
                if c != code and c.startswith(code)
            )
            if sibling_pts and len(zone.get("coords") or []) == len(
                old.get("coords") or []
            ) + sibling_pts:
                zone["coords"] = old.get("coords") or []
                zone["shape"] = old.get("shape", zone["shape"])
                zone["center"] = old.get("center")
                zone["radius_km"] = old.get("radius_km")
        if old is None:
            merged[code] = zone
            added.append(code)
        else:
            updated = dict(old)
            # Geometry and altitudes come from the PDF; curated text stays.
            for key in ("shape", "coords", "center", "radius_km",
                        "max_alt_ft", "min_alt_ft"):
                updated[key] = zone[key]
            if not geometry_equal(old, zone) or (
                old.get("max_alt_ft") != zone["max_alt_ft"]
                or old.get("min_alt_ft") != zone["min_alt_ft"]
            ):
                changed.append(code)
                merged[code] = updated

    for code in by_code:
        if code not in parsed and code not in base_zone_codes:
            missing.append(code)  # reported, never auto-deleted

    report = [
        "# דוח עדכון א-17 (אוטומטי)",
        "",
        f"- אזורים שפוענחו מהמסמך: {len(parsed)}",
        f"- חדשים: {len(added)}" + (f" — {', '.join(sorted(added))}" if added else ""),
        f"- השתנו (גיאומטריה/גבהים): {len(changed)}"
        + (f" — {', '.join(sorted(changed))}" if changed else ""),
        f"- קיימים אצלנו אך לא נמצאו במסמך (לבדיקה ידנית, לא נמחקו): "
        f"{len(missing)}" + (f" — {', '.join(sorted(missing))}" if missing else ""),
        "",
        "יש לאמת את השינויים מול המסמך הרשמי לפני מיזוג.",
    ]
    report_text = "\n".join(report)
    print(report_text)

    out = sorted(merged.values(), key=lambda z: str(z["code"]))
    candidate_path = os.path.join(DATA_DIR, "a17_additions.candidate.json")
    save_json(candidate_path, out)
    with open(os.path.join(REPO_ROOT, "a17_report.md"), "w",
              encoding="utf-8") as f:
        f.write(report_text + "\n")

    if args.apply and (added or changed):
        save_json(os.path.join(DATA_DIR, "a17_additions.json"), out)
        os.remove(candidate_path)
        print("Applied merge to data/a17_additions.json")
    elif not (added or changed):
        os.remove(candidate_path)
        print("No changes vs current data.")


if __name__ == "__main__":
    main()

"""Fetches the RATAG (רשות הטבע והגנים) nature-reserve closures KMZ and
regenerates data/ratag_output.json.

The KML parsing is ported from the proven notam_map_app/scratch/read_ratag_kmz.py.
The download URL is intentionally NOT hardcoded: set RATAG_KMZ_URL (repo
variable in GitHub Actions). When unset the script exits 0 without touching
data/ — the last good file keeps being published.

Env:
    RATAG_KMZ_URL     download URL for the KMZ/ZIP/KML (required to run)
    RATAG_MAX_POINTS  polygon thinning target, default 40 (app renders <= 20)
"""
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

import requests

from common import DATA_DIR

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def clean_coords(coords_str):
    points = []
    for item in coords_str.strip().split():
        if not item:
            continue
        parts = item.split(",")
        if len(parts) >= 2:
            try:
                lon = round(float(parts[0]), 5)
                lat = round(float(parts[1]), 5)
                points.append([lat, lon])
            except ValueError:
                continue
    return points


def parse_description_fields(desc_html):
    fields = {}
    if not desc_html:
        return fields
    normalized_desc = re.sub(r"\s+", " ", desc_html)
    for field in ["Code", "Place", "maxAlt", "minAlt", "reasoning"]:
        pattern = r"<td>" + re.escape(field) + r"</td>\s*<td>(.*?)</td>"
        match = re.search(pattern, normalized_desc, re.IGNORECASE)
        if match:
            val = re.sub(r"<[^>]*>", "", match.group(1)).strip()
            fields[field] = val
    return fields


def thin_polygon(coords, max_points):
    """Evenly samples long polygons; app-side rendering uses <= 20 points, so
    40 keeps full visual fidelity while cutting raw size ~10x."""
    if len(coords) <= max_points:
        return coords
    step = (len(coords) - 1) / (max_points - 1)
    return [coords[round(i * step)] for i in range(max_points)]


def parse_kml_content(kml_bytes, source_name, max_points=40):
    try:
        root = ET.fromstring(kml_bytes)
    except Exception as e:  # noqa: BLE001
        print(f"Error parsing XML for {source_name}: {e}")
        return []

    ns_match = re.match(r"({.*})", root.tag)
    ns = ns_match.group(1) if ns_match else ""

    def ns_tag(tag):
        return f"{ns}{tag}" if ns else tag

    placemarks_data = []
    code_counts = {}

    for placemark in root.iter(ns_tag("Placemark")):
        name_elem = placemark.find(ns_tag("name"))
        name = (
            name_elem.text.strip()
            if name_elem is not None and name_elem.text
            else "Unnamed"
        )

        desc_elem = placemark.find(ns_tag("description"))
        desc_html = (
            desc_elem.text.strip()
            if desc_elem is not None and desc_elem.text
            else ""
        )

        fields = parse_description_fields(desc_html)
        code = fields.get("Code", "").strip()
        place = fields.get("Place", "").strip()
        max_alt = fields.get("maxAlt", "").strip()
        min_alt = fields.get("minAlt", "").strip()
        reasoning = fields.get("reasoning", "").strip()

        if not place or place in ("שמורות טבע", "גנים לאומיים"):
            place = name

        geometries = []
        for poly in placemark.findall(f".//{ns_tag('Polygon')}"):
            coords_elem = poly.find(f".//{ns_tag('coordinates')}")
            if coords_elem is not None and coords_elem.text:
                coords = clean_coords(coords_elem.text)
                if coords:
                    geometries.append(("polygon", thin_polygon(coords, max_points)))
        if not geometries:
            for pt in placemark.findall(f".//{ns_tag('Point')}"):
                coords_elem = pt.find(f".//{ns_tag('coordinates')}")
                if coords_elem is not None and coords_elem.text:
                    coords = clean_coords(coords_elem.text)
                    if coords:
                        geometries.append(("point", coords))
        if not geometries:
            for ls in placemark.findall(f".//{ns_tag('LineString')}"):
                coords_elem = ls.find(f".//{ns_tag('coordinates')}")
                if coords_elem is not None and coords_elem.text:
                    coords = clean_coords(coords_elem.text)
                    if coords:
                        geometries.append(("linestring", coords))

        for shape_type, coords in geometries:
            base_code = code.upper() if code else "LLP_RTG_GEN"
            if base_code not in code_counts:
                code_counts[base_code] = 0
                final_code = base_code
            else:
                code_counts[base_code] += 1
                final_code = f"{base_code}_{code_counts[base_code]}"

            placemarks_data.append(
                {
                    "name": place,
                    "code": final_code,
                    "maxAltFt": int(max_alt) if max_alt.isdigit() else 300,
                    "minAltFt": int(min_alt) if min_alt.isdigit() else 0,
                    "kind": reasoning
                    or ("גן לאומי" if "גן" in place else "שמורת טבע"),
                    "shape": shape_type,
                    "coords": coords,
                    "source": source_name,
                }
            )

    return placemarks_data


def extract_all(blob, label, max_points):
    """Handles KMZ (zip of KML), plain KML, or a ZIP containing either."""
    if blob[:2] == b"PK":
        data = []
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for name in z.namelist():
                lower = name.lower()
                if lower.endswith((".kmz", ".zip")):
                    data.extend(
                        extract_all(z.read(name), f"{label}/{name}", max_points)
                    )
                elif lower.endswith(".kml"):
                    data.extend(
                        parse_kml_content(z.read(name), f"{label}/{name}", max_points)
                    )
        return data
    return parse_kml_content(blob, label, max_points)


def download(url, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            res = requests.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=120
            )
            res.raise_for_status()
            return res.content
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 5 * (attempt + 1)
            print(f"Download attempt {attempt + 1} failed: {e}; retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Failed to download {url}: {last_err}")


def main():
    url = os.environ.get("RATAG_KMZ_URL", "").strip()
    if not url:
        print(
            "RATAG_KMZ_URL is not set — skipping RATAG refresh "
            "(last good data stays published)."
        )
        return

    max_points = int(os.environ.get("RATAG_MAX_POINTS", "40"))
    blob = download(url)
    data = extract_all(blob, os.path.basename(url) or "ratag", max_points)

    if len(data) < 400:
        print(
            f"ERROR: extracted only {len(data)} placemarks (expected 400+); "
            "refusing to overwrite data/ratag_output.json"
        )
        sys.exit(1)

    out_path = os.path.join(DATA_DIR, "ratag_output.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"Extracted {len(data)} placemarks -> {out_path}")


if __name__ == "__main__":
    main()

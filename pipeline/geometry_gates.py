"""Meaning-level gates for the hand-maintained zone files.

The structural checks in validate.py (well-formed JSON, coordinates in the
bbox, required fields) passed on every airspace error found so far. What they
could not see is whether a record still *means* what the source says: a
corridor entered as a circle, the same area entered twice under two codes, a
line stored as an area, a polygon whose vertices are out of order.

Each gate returns findings keyed by a stable string. Keys listed in
data/.geometry_gate_baseline.json are known and outstanding — they report but
do not fail. Anything new fails the build, so this family of bug cannot reach
a published file again unnoticed.
"""
import math
import re

R_KM = 6371.0

# A record whose text says "between the coordinates" describes a corridor —
# the axis runs between two points, so a circle cannot represent it. Note that
# "גליל" alone is NOT enough: a parachuting cylinder ("גליל הצנחה") really is
# a circle in plan view. It is the "between" that makes it a capsule.
CORRIDOR_PHRASES = ('בין הנ"צ', 'בין הנצ', 'בין שתי הנקודות', 'מסדרון')

# Text describing part of a circle. Stored as a full circle it over-covers,
# which is the safe direction but still wrong. "קשת" right after a ב is
# "בקשת" (a request), not an arc; other prefixes (הקשת, וקשת) still count.
PARTIAL_CIRCLE_RE = re.compile(r"חצי מעגל|חצי עיגול|(?<!ב)קשת")

MIN_POLY_AREA_KM2 = 0.05


class Finding:
    def __init__(self, key, message):
        self.key = key
        self.message = message


def _local_xy(coords):
    """Equirectangular projection about the centroid — fine over a few tens
    of km, which is more than any single zone spans."""
    lat0 = math.radians(sum(c[0] for c in coords) / len(coords))
    return [(math.radians(c[1]) * math.cos(lat0) * R_KM,
             math.radians(c[0]) * R_KM) for c in coords]


def polygon_area_km2(coords):
    if len(coords) < 3:
        return 0.0
    pts = _local_xy(coords)
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def _segments_cross(p1, p2, p3, p4):
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(v) < 1e-12:
            return 0
        return 1 if v > 0 else -1

    o1, o2 = orient(p1, p2, p3), orient(p1, p2, p4)
    o3, o4 = orient(p3, p4, p1), orient(p3, p4, p2)
    return o1 != o2 and o3 != o4 and 0 not in (o1, o2, o3, o4)


def polygon_self_intersects(coords):
    """True when two non-adjacent edges cross — a 'bowtie'. Such a polygon
    renders wrong and, worse, gives wrong point-in-polygon answers, which is
    what tap detection and route-conflict checks rely on."""
    pts = _local_xy(coords)
    n = len(pts)
    if n < 4:
        return False
    for i in range(n):
        a1, a2 = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            if _segments_cross(a1, a2, pts[j], pts[(j + 1) % n]):
                return True
    return False


def _geometry_signature(z):
    """Normalised shape, for spotting the same area entered twice."""
    if str(z.get("shape", "")).lower() == "circle":
        c = z.get("center") or [0, 0]
        return ("circle", round(float(c[0]), 4), round(float(c[1]), 4),
                round(float(z.get("radius_km") or 0), 3))
    coords = z.get("coords") or []
    if len(coords) < 3:
        return None
    pts = tuple(sorted((round(float(c[0]), 5), round(float(c[1]), 5)) for c in coords))
    return ("polygon", pts)


def _text_of(z):
    return f"{z.get('notes') or ''} {z.get('name_he') or ''}"


def run(zone_files):
    """zone_files: {filename: [zone dicts]}. Returns a list of Finding."""
    findings = []
    everything = [(name, z) for name, entries in zone_files.items() for z in entries]

    # 1 — the same code in two files. The app merges with
    #     `where((z) => !existing.contains(z.code))`, so the second copy is
    #     dropped in silence; twice that hid the larger, official area.
    per_file = {name: {str(z.get("code", "")).upper() for z in entries}
                for name, entries in zone_files.items()}
    names = sorted(per_file)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for code in sorted(per_file[a] & per_file[b]):
                findings.append(Finding(
                    f"dup-code:{code}",
                    f"{code} exists in both {a} and {b} — one copy is dropped "
                    f"silently by the code-based merge"))

    # 2 — identical geometry under different codes. Both get drawn, stacked on
    #     each other, and each claims to be a separate area.
    by_sig = {}
    for name, z in everything:
        sig = _geometry_signature(z)
        if sig is None:
            continue
        by_sig.setdefault(sig, []).append((name, str(z.get("code", "")).upper()))
    for sig, group in by_sig.items():
        codes = sorted({c for _, c in group})
        if len(codes) > 1:
            findings.append(Finding(
                "same-geom:" + "|".join(codes),
                f"{', '.join(codes)} share identical {sig[0]} geometry under "
                f"different codes"))

    # 3 — the text and the shape disagree.
    for name, z in everything:
        code = str(z.get("code", "")).upper()
        shape = str(z.get("shape", "")).lower()
        if shape != "circle":
            continue
        text = _text_of(z)
        if any(p in text for p in CORRIDOR_PHRASES):
            findings.append(Finding(
                f"shape-vs-text:{code}",
                f"{name}: {code} is described as running between coordinates "
                f"(a corridor) but is stored as a circle"))
        elif PARTIAL_CIRCLE_RE.search(text):
            findings.append(Finding(
                f"shape-vs-text:{code}",
                f"{name}: {code} is described as part of a circle "
                f"(חצי מעגל / קשת) but is stored as a whole one"))

    # 4 — self-intersecting outlines.
    # 5 — polygons with no real area: a line or a point saved as an area.
    for name, z in everything:
        code = str(z.get("code", "")).upper()
        if str(z.get("shape", "")).lower() != "polygon":
            continue
        coords = [(float(c[0]), float(c[1])) for c in (z.get("coords") or [])]
        if len(coords) < 3:
            continue
        # Self-intersection first: the shoelace area of a crossed outline is
        # meaningless (a symmetric bowtie sums to exactly zero), so testing
        # area first would report the wrong thing about the wrong polygons.
        if polygon_self_intersects(coords):
            findings.append(Finding(
                f"self-intersect:{code}",
                f"{name}: {code} outline crosses itself — vertices are out of "
                f"order; point-in-polygon tests on it are unreliable"))
            continue
        area = polygon_area_km2(coords)
        if area < MIN_POLY_AREA_KM2:
            findings.append(Finding(
                f"degenerate-poly:{code}",
                f"{name}: {code} is a polygon of {area:.4f} km² "
                f"({len(coords)} points) — a line stored as an area?"))

    return findings

import json
import os
import subprocess
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
)

from common import canonical_bytes, gzip_deterministic, sha256_hex  # noqa: E402
from fetch_ratag import clean_coords, parse_kml_content, thin_polygon  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>שמורת בדיקה</name>
      <description><![CDATA[
        <table>
          <tr><td>Code</td><td>LLP9999</td></tr>
          <tr><td>Place</td><td>שמורת בדיקה</td></tr>
          <tr><td>maxAlt</td><td>500</td></tr>
          <tr><td>reasoning</td><td>שמורת טבע</td></tr>
        </table>]]></description>
      <Polygon><outerBoundaryIs><LinearRing><coordinates>
        35.0,32.0,0 35.1,32.0,0 35.1,32.1,0 35.0,32.1,0 35.0,32.0,0
      </coordinates></LinearRing></outerBoundaryIs></Polygon>
    </Placemark>
  </Document>
</kml>
"""


def test_clean_coords_swaps_to_lat_lon():
    pts = clean_coords("35.0,32.0,0 35.1,32.05,0")
    assert pts == [[32.0, 35.0], [32.05, 35.1]]


def test_parse_kml_fixture():
    items = parse_kml_content(KML_FIXTURE.encode("utf-8"), "fixture.kml")
    assert len(items) == 1
    item = items[0]
    assert item["code"] == "LLP9999"
    assert item["name"] == "שמורת בדיקה"
    assert item["maxAltFt"] == 500
    assert item["shape"] == "polygon"
    assert len(item["coords"]) == 5
    # lat first, inside Israel bbox
    assert 29.0 <= item["coords"][0][0] <= 33.6


def test_thin_polygon_keeps_endpoints():
    coords = [[32.0 + i * 0.001, 35.0] for i in range(200)]
    thinned = thin_polygon(coords, 40)
    assert len(thinned) == 40
    assert thinned[0] == coords[0]
    assert thinned[-1] == coords[-1]


def test_gzip_deterministic():
    payload = canonical_bytes({"b": 1, "a": [1.5, "עברית"]})
    assert gzip_deterministic(payload) == gzip_deterministic(payload)
    assert sha256_hex(payload) == sha256_hex(payload)


def test_canonical_bytes_sorted_and_compact():
    raw = canonical_bytes({"b": 1, "a": 2}).decode("utf-8")
    assert raw == '{"a":2,"b":1}'


def test_validate_passes_on_committed_data():
    result = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "pipeline", "validate.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

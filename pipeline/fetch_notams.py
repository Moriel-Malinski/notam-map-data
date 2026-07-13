"""Fetches the IAA NOTAM page once and publishes it as docs/notams.json.

Runs every ~20 minutes from .github/workflows/update-notams.yml. Installed
apps read the published file from GitHub Pages instead of each scraping the
IAA site themselves — one polite request per run replaces one request per
user, which is what keeps the IAA happy at any user count.

Parsing mirrors the app's NotamService: on the AeroInfo page each NOTAM is
its own `<table class="tblMainInfo">`; its id and location sit in the first
row (`td.NotamID` / `td.Location`, blanked out in later rows) and the message
text is split across one `td.MsgText` cell per line, which we re-join.

Publish policy: rewrite the file when the NOTAM list changed, or when the
published fetchedAt is older than REFRESH_AFTER (a freshness "pulse", so apps
can tell a quiet feed from a dead one and fall back to fetching IAA directly).
An unreachable IAA keeps the last published file; parsing 0 NOTAMs out of an
HTTP 200 page fails the run — that means the page structure changed.
"""
import datetime as dt
import json
import os
import re
import sys

import requests
import urllib3
from bs4 import BeautifulSoup

from common import DOCS_DIR, load_json, save_json

IAA_NOTAM_URL = "https://brin.iaa.gov.il/aeroinfo/AeroInfo.aspx?msgType=Notam"
OUT_PATH = os.path.join(DOCS_DIR, "notams.json")
SCHEMA_VERSION = 1

# Honest identification, unlike a spoofed browser UA: lets the IAA see who
# is calling and reach out instead of blocking blindly.
USER_AGENT = (
    "notam-map-data pipeline "
    "(+https://github.com/Moriel-Malinski/notam-map-data)"
)

# Rewrite fetchedAt even without content changes once it gets this old.
REFRESH_AFTER = dt.timedelta(hours=3)


def _clean(text: str) -> str:
    """Collapse &nbsp;/whitespace runs and trim — same as the app's _clean."""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _first_non_empty(cells) -> str:
    for cell in cells:
        text = _clean(cell.get_text())
        if text:
            return text
    return ""


def parse_notams(html: str) -> list[dict]:
    """The IAA AeroInfo page -> [{id, location, eLine}, ...]."""
    soup = BeautifulSoup(html, "html.parser")
    notams = []
    for table in soup.select("table.tblMainInfo"):
        notam_id = _first_non_empty(table.select("td.NotamID"))
        location = _first_non_empty(table.select("td.Location"))
        message = " ".join(
            text
            for text in (_clean(c.get_text()) for c in table.select("td.MsgText"))
            if text
        )
        if not notam_id or not message:
            continue
        # The list view carries the E-line; strip the leading "E)" label and
        # the trailing NOTAM-closing ")".
        e_line = re.sub(r"^\s*E\)\s*", "", message)
        e_line = re.sub(r"\)\s*$", "", e_line).strip()
        notams.append({"id": notam_id, "location": location, "eLine": e_line})
    return notams


def _fetch_page() -> str | None:
    # brin.iaa.gov.il serves a certificate the standard trust store rejects
    # (the app carries the same per-host exception), hence verify=False for
    # this one government host.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        res = requests.get(
            IAA_NOTAM_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=60,
            verify=False,
        )
        res.raise_for_status()
        return res.text
    except requests.RequestException as e:
        print(f"IAA fetch failed: {e}")
        return None


def main() -> int:
    html = _fetch_page()
    if html is None:
        # Transient outage: keep the last published file. Apps notice the
        # stale fetchedAt and fall back to fetching IAA directly themselves.
        print("Keeping the previously published notams.json.")
        return 0

    notams = parse_notams(html)
    if not notams:
        print(
            "Parsed 0 NOTAMs out of an HTTP 200 page — structure changed?",
            file=sys.stderr,
        )
        return 1

    previous = load_json(OUT_PATH) if os.path.exists(OUT_PATH) else {}
    prev_fetched = None
    if isinstance(previous.get("fetchedAt"), str):
        try:
            prev_fetched = dt.datetime.fromisoformat(
                previous["fetchedAt"].replace("Z", "+00:00")
            )
        except ValueError:
            pass

    now = dt.datetime.now(dt.timezone.utc)
    fresh_enough = (
        prev_fetched is not None and now - prev_fetched < REFRESH_AFTER
    )
    if previous.get("notams") == notams and fresh_enough:
        print(f"{len(notams)} NOTAMs, unchanged and fresh — nothing to publish.")
        return 0

    save_json(
        OUT_PATH,
        {
            "schemaVersion": SCHEMA_VERSION,
            "fetchedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": IAA_NOTAM_URL,
            "notams": notams,
        },
    )
    changed = "changed" if previous.get("notams") != notams else "pulse"
    print(f"Published {len(notams)} NOTAMs ({changed}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Fetches the IAA NOTAM page once and publishes it as docs/notams.json.

Runs every ~20 minutes from .github/workflows/update-notams.yml. Installed
apps read the published file from GitHub Pages instead of each scraping the
IAA site themselves — one polite request per run replaces one request per
user, which is what keeps the IAA happy at any user count.

Parsing mirrors the app's NotamService: on the AeroInfo page each NOTAM is
its own `<table class="tblMainInfo">` inside a `<div id="divMainInfo_N">`
(N = the site's internal message number); its id and location sit in the
first row (`td.NotamID` / `td.Location`, blanked out in later rows) and the
message text is split across one `td.MsgText` cell per line, which we re-join.

Details (validity dates, airfield, daily schedule): the site's plus button
posts the page form back with the message number, and the response embeds a
`f_buildMoreMsgInfo('<Msg FromDate=... ToDate=...>...')` script with the full
NOTAM. We replay that browser postback — the direct .asmx endpoint is blocked
by the site's WAF. NOTAMs never change after publication (amendments get a
new id), so details are fetched once per NOTAM and reused from the previously
published notams.json on every later run; steady state is a handful of detail
requests per day, spaced DETAIL_DELAY_S apart.

Publish policy: rewrite the file when the NOTAM list changed, or when the
published fetchedAt is older than REFRESH_AFTER (a freshness "pulse", so apps
can tell a quiet feed from a dead one and fall back to fetching IAA directly).
An unreachable IAA keeps the last published file; parsing 0 NOTAMs out of an
HTTP 200 page fails the run — that means the page structure changed. A failed
detail fetch publishes the NOTAM without dates (retried on later runs).
"""
import datetime as dt
import json
import os
import re
import sys
import time
from xml.etree import ElementTree

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

# Details: at most this many postbacks per run, politely spaced. Steady
# state needs a handful; the cap only matters when seeding from scratch.
DETAILS_CAP = 150
DETAIL_DELAY_S = 0.4

# Keys that only exist after a successful details postback. Their presence
# on a previously published NOTAM means "no need to fetch again".
DETAIL_KEYS = ("validFrom", "validTo", "airfield", "dLine")


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
    """The IAA AeroInfo page -> [{id, location, eLine, msgNum}, ...].

    msgNum is the site-internal message number needed for the details
    postback; empty when the table has no divMainInfo_N ancestor.
    """
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
        parent = table.find_parent("div", id=re.compile(r"^divMainInfo_\d+$"))
        msg_num = parent["id"][len("divMainInfo_"):] if parent else ""
        # The list view carries the E-line; strip the leading "E)" label and
        # the trailing NOTAM-closing ")".
        e_line = re.sub(r"^\s*E\)\s*", "", message)
        e_line = re.sub(r"\)\s*$", "", e_line).strip()
        notams.append(
            {"id": notam_id, "location": location, "eLine": e_line, "msgNum": msg_num}
        )
    return notams


def _iso_from_notam_stamp(raw: str) -> str | None:
    """'202607140700' -> '2026-07-14T07:00Z'. Anything else (PERM, EST,
    empty) -> None."""
    if raw and re.fullmatch(r"\d{12}", raw):
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}T{raw[8:10]}:{raw[10:12]}Z"
    return None


def parse_details_xml(xml_str: str) -> dict | None:
    """The <Msg .../> blob from f_buildMoreMsgInfo -> detail fields.
    Returns None for an unparsable blob or the site's "expired" marker."""
    try:
        root = ElementTree.fromstring(xml_str)
    except ElementTree.ParseError:
        return None
    if root.tag != "Msg" or root.get("MsgNumber") == "0":
        return None
    texts = [(t.text or "").strip() for t in root.findall("MsgText")]
    d_line = next(
        (re.sub(r"^D\)\s*", "", t) for t in texts if t.startswith("D)")), ""
    )
    return {
        "validFrom": _iso_from_notam_stamp(root.get("FromDate", "")),
        "validTo": _iso_from_notam_stamp(root.get("ToDate", "")),
        "airfield": (root.get("Airfield") or "").strip(),
        "dLine": d_line,
    }


def extract_details_script(html: str) -> str | None:
    """Pulls the <Msg>...</Msg> XML out of the postback response."""
    m = re.search(r"f_buildMoreMsgInfo\('(<Msg .*?</Msg>)'\)", html, re.DOTALL)
    if not m:
        return None
    return m.group(1).replace("\\'", "'")


def _hidden_fields(soup: BeautifulSoup) -> dict:
    return {
        el["name"]: el.get("value", "")
        for el in soup.select("input[type=hidden]")
        if el.get("name")
    }


def _fetch_page(session: requests.Session) -> str | None:
    # brin.iaa.gov.il serves a certificate the standard trust store rejects
    # (the app carries the same per-host exception), hence verify=False for
    # this one government host.
    try:
        res = session.get(IAA_NOTAM_URL, timeout=60, verify=False)
        res.raise_for_status()
        return res.text
    except requests.RequestException as e:
        print(f"IAA fetch failed: {e}")
        return None


def _fetch_details(session: requests.Session, form: dict, msg_num: str) -> dict | None:
    """One browser-equivalent postback for one NOTAM's full details."""
    data = dict(form)
    data["hidMsgNum"] = msg_num
    data["hidMode"] = "more"
    data["hidCurOrHist"] = "Current"
    data["btnMoreInfo"] = ""
    try:
        res = session.post(
            IAA_NOTAM_URL, data=data, timeout=60, verify=False,
            headers={"Referer": IAA_NOTAM_URL},
        )
        res.raise_for_status()
    except requests.RequestException as e:
        print(f"details postback failed for msg {msg_num}: {e}")
        return None
    xml_str = extract_details_script(res.text)
    if xml_str is None:
        print(f"details script missing for msg {msg_num}")
        return None
    return parse_details_xml(xml_str)


def main() -> int:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    html = _fetch_page(session)
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
    prev_by_id = {
        n.get("id"): n for n in previous.get("notams", []) if isinstance(n, dict)
    }

    # Details: reuse what an earlier run already fetched (NOTAMs are
    # immutable once issued); postback only for ids we have never detailed.
    form = _hidden_fields(BeautifulSoup(html, "html.parser"))
    fetched = 0
    for notam in notams:
        prev = prev_by_id.get(notam["id"])
        if prev is not None and all(k in prev for k in DETAIL_KEYS):
            for k in DETAIL_KEYS:
                notam[k] = prev[k]
            continue
        if not notam["msgNum"] or fetched >= DETAILS_CAP:
            continue
        details = _fetch_details(session, form, notam["msgNum"])
        fetched += 1
        time.sleep(DETAIL_DELAY_S)
        if details is not None:
            notam.update(details)
    if fetched:
        print(f"Fetched details for {fetched} new NOTAM(s).")

    # msgNum is a session-scoped implementation detail — not published.
    for notam in notams:
        notam.pop("msgNum", None)

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

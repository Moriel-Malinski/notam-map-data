"""Fetches Israel's series-A NOTAMs from the NMS-API and publishes them as
docs/notams_nms.json.

A COMPLEMENTARY source, not a replacement for the IAA list: measured on
2026-11-12, the service holds only what Israel's international NOTAM office
sends abroad (series A). Series C (the domestic series) is not there at all,
so docs/notams.json (IAA) stays untouched by this script.

One request per run: every active NOTAM of the Israeli NOTAM office account
(NMS_ACCOUNTABILITY, default LLADYNYX), as GeoJSON.

Each published NOTAM keeps the fields the app already reads from the IAA feed
(id, location, eLine, validFrom, validTo, dLine, lowerLimit, upperLimit) and
adds the original text (rawText) and the Q-line data. A field the service
does not give stays empty - nothing is guessed.

Exit codes:
  0  published, or nothing to publish, or a transient failure (the previous
     file is kept; its fetchedAt shows the age)
  1  the answer was readable but held no usable Israeli NOTAM (the previous
     file is kept) - structure or account change, needs a look
  2  authentication failed (missing or rejected credentials)

Terms of the service: the data may be redistributed but must not be presented
as FAA data, so the published file names the source only as "NMS".
"""
import datetime as dt
import os
import re
import sys

import requests

from common import DOCS_DIR, load_json, save_json
from nms_client import NmsAuthError, NmsClient, NmsHttpError, is_israeli, notam_of

OUT_PATH = os.path.join(DOCS_DIR, "notams_nms.json")
SCHEMA_VERSION = 1
SOURCE = "NMS"
DEFAULT_ACCOUNTABILITY = "LLADYNYX"
REFRESH_AFTER = dt.timedelta(hours=3)

_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?Z$")
_NUMBER = re.compile(r"^[A-Z]\d{4}/\d{2}$")


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _one_line(value) -> str:
    return re.sub(r"\s+", " ", _text(value))


def _minute(value) -> str | None:
    """'2026-11-19T08:00:00.000Z' -> '2026-11-19T08:00Z' (the feed's form).
    Anything else (PERM, empty, another shape) -> None."""
    m = _ISO.match(_text(value))
    return f"{m.group(1)}T{m.group(2)}:{m.group(3)}Z" if m else None


def _parse_minute(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)


def _raw_text(feature: dict) -> str:
    """The original ICAO-format NOTAM, line breaks kept."""
    try:
        translations = feature["properties"]["coreNOTAMData"]["notamTranslation"]
    except (KeyError, TypeError):
        return ""
    if not isinstance(translations, list):
        return ""
    for t in translations:
        if isinstance(t, dict) and t.get("type") == "ICAO":
            return _text(t.get("formattedText")).replace("\r\n", "\n").replace("\r", "\n")
    return ""


def _item_from_raw(raw: str, letter: str) -> str:
    """Item D) / F) / G) as written in the original text ('' when absent)."""
    m = re.search(
        rf"(?:^|\n|\s){letter}\)\s*(.*?)(?=(?:\n|\s)[A-G]\)\s|\Z)", raw, re.DOTALL
    )
    return _one_line(m.group(1)) if m else ""


def _q_line(raw: str) -> str:
    m = re.search(r"^Q\)\s*(.+)$", raw, re.MULTILINE)
    return m.group(1).strip() if m else ""


def convert(feature: dict) -> dict | None:
    """One GeoJSON feature -> one feed entry; None when it is not a usable
    active Israeli NOTAM (no number, no text, a cancel notice, cancelled)."""
    n = notam_of(feature)
    number = _text(n.get("number")).upper()
    text = _one_line(n.get("text"))
    if not _NUMBER.match(number) or not text or not is_israeli(n):
        return None
    if _text(n.get("type")).upper() == "C" or _text(n.get("cancelationDate")):
        return None
    valid_from = _minute(n.get("effectiveStart"))
    if valid_from is None:
        return None
    end = _text(n.get("effectiveEnd"))
    permanent = end.upper() == "PERM"
    valid_to = _minute(end)
    if valid_to is None and not permanent:
        # An end we cannot read: do not publish a NOTAM with a guessed window.
        return None
    raw = _raw_text(feature)
    geometry = feature.get("geometry")
    return {
        "id": number,
        "location": _text(n.get("icaoLocation")) or _text(n.get("location")),
        "eLine": text,
        "validFrom": valid_from,
        "validTo": valid_to,  # None = permanent (PERM)
        "endEstimated": _text(n.get("estimated")).lower() == "true",
        "created": _minute(n.get("issued")),
        "dLine": _one_line(n.get("schedule")) or _item_from_raw(raw, "D"),
        "lowerLimit": _one_line(n.get("lowerLimit")),
        "upperLimit": _one_line(n.get("upperLimit")),
        "rawText": raw,
        "qLine": _q_line(raw),
        "qCode": _text(n.get("selectionCode")),
        "minimumFl": _text(n.get("minimumFl")),
        "maximumFl": _text(n.get("maximumFl")),
        "qCoordinates": _text(n.get("coordinates")),
        "qRadiusNm": _text(n.get("radius")),
        "series": _text(n.get("series")),
        "lastUpdated": _minute(n.get("lastUpdated")),
        "nmsId": _text(n.get("id")),
        "geometry": geometry if isinstance(geometry, dict) else None,
    }


def build(features: list[dict], now: dt.datetime) -> list[dict]:
    """Converted, de-duplicated (the service can return one NOTAM number
    under two ids - the latest update wins), without expired ones, sorted."""
    by_id: dict[str, dict] = {}
    for feature in features:
        entry = convert(feature)
        if entry is None:
            continue
        end = _parse_minute(entry["validTo"])
        if end is not None and end < now:
            continue
        old = by_id.get(entry["id"])
        if old is None or (entry["lastUpdated"] or "", entry["nmsId"]) > (
            old["lastUpdated"] or "", old["nmsId"]
        ):
            by_id[entry["id"]] = entry
    return [by_id[k] for k in sorted(by_id)]


def environment_of(host: str) -> str:
    return "staging" if "staging" in host else ("test" if "-fit" in host else "production")


def main() -> int:
    client = NmsClient()
    accountability = os.environ.get("NMS_ACCOUNTABILITY") or DEFAULT_ACCOUNTABILITY
    try:
        features = client.notams(accountability=accountability)
    except NmsAuthError as e:
        print(f"NMS authentication failed: {e}", file=sys.stderr)
        return 2
    except NmsHttpError as e:
        if e.status in (401, 403):
            print(f"NMS authentication failed: {e}", file=sys.stderr)
            return 2
        print(f"NMS fetch failed ({e}). Keeping the previously published file.")
        return 0
    except (requests.RequestException, ValueError) as e:
        print(f"NMS fetch failed ({type(e).__name__}). Keeping the previously published file.")
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    notams = build(features, now)
    if not notams:
        print(
            f"{len(features)} feature(s) came back but none is a usable Israeli "
            "NOTAM - keeping the previously published file.",
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
    fresh_enough = prev_fetched is not None and now - prev_fetched < REFRESH_AFTER
    environment = environment_of(client.host)
    unchanged = (
        previous.get("notams") == notams and previous.get("environment") == environment
    )
    if unchanged and fresh_enough:
        print(f"{len(notams)} NOTAMs, unchanged and fresh - nothing to publish.")
        return 0

    save_json(
        OUT_PATH,
        {
            "schemaVersion": SCHEMA_VERSION,
            "fetchedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": SOURCE,
            "environment": environment,
            "coverage": "series A only - not the domestic series C",
            "notams": notams,
        },
    )
    print(f"Published {len(notams)} NOTAMs ({'pulse' if unchanged else 'changed'}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

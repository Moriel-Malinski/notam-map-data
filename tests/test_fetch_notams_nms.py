"""fetch_notams_nms: conversion of real NMS GeoJSON answers (saved on
2026-11-12 from the staging environment) into the feed structure, and the
"never overwrite a good file with a bad run" rules."""
import copy
import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import fetch_notams_nms as fx  # noqa: E402
from nms_client import NmsAuthError, NmsHttpError  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "nms_geojson_sample.json")
NOW = dt.datetime(2026, 11, 12, 12, 0, tzinfo=dt.timezone.utc)

# What the app's feed reader (NotamService._readFeed) takes from an entry.
APP_KEYS = {"id", "location", "eLine", "validFrom", "validTo", "dLine",
            "lowerLimit", "upperLimit"}


@pytest.fixture
def features():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def by_id(entries):
    return {e["id"]: e for e in entries}


def test_every_entry_has_the_fields_the_app_reads(features):
    entries = fx.build(features, NOW)
    assert entries
    for e in entries:
        assert APP_KEYS <= set(e)
        assert e["id"] and e["eLine"] and e["location"].startswith("LL")
        assert e["rawText"].startswith(e["id"])  # the original text is always kept


def test_plain_notam_fields(features):
    e = by_id(fx.build(features, NOW))["A0890/26"]
    assert e["location"] == "LLLL"
    assert e["eLine"] == "VOR/DME NAT FREQ 112.400MHZ WITHDRAWN FOR MAINT."
    assert e["validFrom"] == "2026-11-19T08:00Z"
    assert e["validTo"] == "2026-11-19T10:00Z"
    assert e["created"] == "2026-11-11T10:32Z"
    assert e["qLine"] == "LLLL/QNMAS/IV/BO /E /000/999/3220N03458E025"
    assert (e["qCode"], e["minimumFl"], e["maximumFl"]) == ("QNMAS", "000", "999")
    assert (e["qCoordinates"], e["qRadiusNm"]) == ("3220N03458E", "25")
    assert e["geometry"]["type"] == "GeometryCollection"
    assert e["dLine"] == "" and e["lowerLimit"] == "" and e["upperLimit"] == ""


def test_permanent_notam_has_no_end_and_keeps_limits(features):
    e = by_id(fx.build(features, NOW))["A0616/26"]
    assert e["validTo"] is None
    assert e["lowerLimit"] == "7000FT AMSL"
    assert e["upperLimit"] == "40000FT AMSL"
    assert "\r" not in e["rawText"] and "F) 7000FT AMSL G) 40000FT AMSL" in e["rawText"]
    # Multi-line text is joined into one line, like the IAA feed's eLine.
    assert "\n" not in e["eLine"] and "  " not in e["eLine"]


def test_same_number_under_two_ids_is_published_once(features):
    numbers = [fx.notam_of(f)["number"] for f in features]
    assert numbers.count("A0616/26") == 2 and numbers.count("A0457/26") == 2
    ids = [e["id"] for e in fx.build(features, NOW)]
    assert len(ids) == len(set(ids)) == 7
    assert ids == sorted(ids)
    # The later update wins.
    assert by_id(fx.build(features, NOW))["A0616/26"]["nmsId"].endswith("0608")


def test_expired_notams_are_dropped(features):
    later = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
    ids = by_id(fx.build(features, later))
    assert "A0890/26" not in ids  # ended 19.11 10:00
    assert "A0616/26" in ids  # permanent


def _one(features, number="A0890/26"):
    return copy.deepcopy(next(f for f in features if fx.notam_of(f)["number"] == number))


def test_cancelled_and_cancel_notices_are_dropped(features):
    f = _one(features)
    fx.notam_of(f)["cancelationDate"] = "2026-11-12T09:00:00Z"
    assert fx.convert(f) is None
    f = _one(features)
    fx.notam_of(f)["type"] = "C"
    assert fx.convert(f) is None


def test_unreadable_end_is_not_published(features):
    f = _one(features)
    fx.notam_of(f)["effectiveEnd"] = "sometime"
    assert fx.convert(f) is None
    f = _one(features)
    del fx.notam_of(f)["effectiveEnd"]
    assert fx.convert(f) is None


def test_non_israeli_and_malformed_features_are_skipped(features):
    f = _one(features)
    n = fx.notam_of(f)
    n["icaoLocation"], n["location"], n["affectedFir"] = "OJAI", "OJAI", "OJAC"
    assert fx.convert(f) is None
    assert fx.convert({}) is None
    assert fx.convert({"properties": {"coreNOTAMData": {"notam": {"number": "A1/26"}}}}) is None
    assert fx.build([{}, {"properties": None}], NOW) == []


def test_schedule_and_estimated_end(features):
    f = _one(features)
    n = fx.notam_of(f)
    n["schedule"] = "DAILY 0500-1900"
    n["estimated"] = "True"
    e = fx.convert(f)
    assert e["dLine"] == "DAILY 0500-1900" and e["endEstimated"] is True


def test_d_line_is_read_from_the_original_text_when_schedule_is_missing(features):
    f = _one(features)
    t = f["properties"]["coreNOTAMData"]["notamTranslation"][0]
    t["formattedText"] = (
        "A0001/26 NOTAMN\r\nQ) LLLL/QRTCA/IV/BO /W /000/050/3200N03450E005\r\n"
        "A) LLLL B) 2611190800 C) 2611211000\r\nD) DAILY 0800-1000\r\n"
        "E) AREA ACT.\r\nF) SFC G) 5000FT AMSL"
    )
    assert fx.convert(f)["dLine"] == "DAILY 0800-1000"


# --- main(): what gets written -------------------------------------------

class FakeClient:
    host = "https://api-staging.cgifederal-aim.com"

    def __init__(self, result):
        self._result = result
        self.calls = []

    def notams(self, **params):
        self.calls.append(params)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.fixture
def out(tmp_path, monkeypatch):
    path = tmp_path / "notams_nms.json"
    monkeypatch.setattr(fx, "OUT_PATH", str(path))
    monkeypatch.delenv("NMS_ACCOUNTABILITY", raising=False)
    return path


def run(monkeypatch, result):
    client = FakeClient(result)
    monkeypatch.setattr(fx, "NmsClient", lambda: client)
    return fx.main(), client


def test_main_publishes_with_one_request(out, monkeypatch, features):
    code, client = run(monkeypatch, features)
    assert code == 0
    assert client.calls == [{"accountability": "LLADYNYX"}]
    body = json.loads(out.read_text(encoding="utf-8"))
    assert body["source"] == "NMS" and body["environment"] == "staging"
    assert body["schemaVersion"] == 1 and body["fetchedAt"].endswith("Z")
    # main() uses the real clock: only the three permanent NOTAMs of the
    # fixture are guaranteed to be still valid whenever this test runs.
    assert {"A0056/26", "A0457/26", "A0616/26"} <= {n["id"] for n in body["notams"]}
    text = out.read_text(encoding="utf-8")
    assert "FAA" not in text.upper().replace("FAA-", "")  # never labelled FAA data
    assert "Bearer" not in text and "access_token" not in text


def test_main_keeps_previous_file_when_the_fetch_fails(out, monkeypatch):
    out.write_text('{"keep": "me"}', encoding="utf-8")
    for failure in (NmsHttpError(500, "Internal Server Error"),
                    NmsHttpError(429, "Too Many Requests"),
                    fx.requests.ConnectionError("down")):
        code, _ = run(monkeypatch, failure)
        assert code == 0
        assert out.read_text(encoding="utf-8") == '{"keep": "me"}'


def test_main_fails_clearly_on_auth_and_keeps_the_file(out, monkeypatch, capsys):
    out.write_text('{"keep": "me"}', encoding="utf-8")
    code, _ = run(monkeypatch, NmsAuthError("token request rejected: HTTP 401 invalid_client"))
    assert code == 2
    assert "authentication failed" in capsys.readouterr().err
    code, _ = run(monkeypatch, NmsHttpError(401, "Unauthorized"))
    assert code == 2
    assert out.read_text(encoding="utf-8") == '{"keep": "me"}'


def test_main_never_overwrites_with_an_empty_list(out, monkeypatch):
    out.write_text('{"keep": "me"}', encoding="utf-8")
    code, _ = run(monkeypatch, [])
    assert code == 1
    assert out.read_text(encoding="utf-8") == '{"keep": "me"}'


def test_main_skips_the_write_when_unchanged_and_fresh(out, monkeypatch, features):
    assert run(monkeypatch, features)[0] == 0
    first = out.read_text(encoding="utf-8")
    assert run(monkeypatch, features)[0] == 0
    assert out.read_text(encoding="utf-8") == first

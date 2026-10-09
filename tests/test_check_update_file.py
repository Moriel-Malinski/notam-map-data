import os
import sys
from datetime import date

import requests

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
)

import check_update_file  # noqa: E402
from check_update_file import (  # noqa: E402
    candidate_urls,
    normalize,
    parse_effective_date,
    parse_issue_number,
    squash,
    uav_hits,
)
from fetch_a17 import change_is_suspicious  # noqa: E402

# Verbatim excerpts of what PyMuPDF extracts from the real קובץ עדכון 1-26 —
# RTL runs split mid-word, geresh displaced, month glued to the year.
COVER_SAMPLE = """רשות
 התעופה האזרחית– אגף תשתיות תעופתיות
 פמ"ת פנים ארצי- הוראות עדכון
 'עדכון מס1/26
  ת
אריך כניסה לתוקף
06

 אוגוסט2026
•

'א-
17
 – עדכו ,ן פרטי קשר
הטמע
ה ושינוי א זוריLLD
•

'ב-
09
 –
 הט מעת
טופס תכניות טיסה לכ
טמ"מ , עדכון הפרדות לטיסות
BVLOS, עדכ
ון טיסה מקומית לט
יסת רב ל
הב
"""

AIRPORT_ONLY_SAMPLE = """פמ"ת פנים ארצי- הוראות עדכון
 'עדכון מס2/30
 תאריך כניסה לתוקף 02 אוקטובר 2030
 חיפה: עדכון שעות פעילות מרכז תפעול, עדכון רחבה N
"""


def test_parse_issue_number_from_garbled_rtl():
    assert parse_issue_number(normalize(COVER_SAMPLE)) == "1/26"


def test_parse_effective_date_month_glued_to_year():
    assert parse_effective_date(normalize(COVER_SAMPLE)) == "2026-08-06"


def test_parse_effective_date_with_spaces():
    assert parse_effective_date(normalize(AIRPORT_ONLY_SAMPLE)) == "2030-10-02"


def test_uav_hits_finds_split_keywords():
    hits = uav_hits(COVER_SAMPLE)
    assert "א-17" in hits
    assert "ב-09" in hits
    assert 'כטמ"מ' in hits
    assert "BVLOS" in hits


def test_uav_hits_ignores_airport_only_amendment():
    assert uav_hits(AIRPORT_ONLY_SAMPLE) == []


def test_squash_removes_all_whitespace():
    assert squash("ב-\n09\n –") == 'ב-09-'


def test_uav_hits_match_any_dash_variant():
    # Hebrew maqaf, en dash, em dash — all look like "-" in the PDF.
    assert uav_hits("עדכון פרק א\u05be17") == ["א-17"]
    assert uav_hits("עדכון פרק ב\u201309") == ["ב-09"]
    assert uav_hits("עדכון פרק א\u201417") == ["א-17"]


def test_guard_allows_normal_amendment():
    # A real amendment: a handful of zones added/changed, nothing missing.
    assert change_is_suspicious(120, ["LLD34"], ["LLP23", "LLR45"], []) is None


def test_guard_trips_on_mass_disappearance():
    # 12 zones missing beyond the known hand-curated baseline.
    new_missing = [f"LLD{i}" for i in range(12)]
    assert change_is_suspicious(120, [], [], new_missing) is not None


def test_guard_trips_on_mass_rewrite():
    changed = [f"LLP{i}" for i in range(70)]
    assert change_is_suspicious(120, [], changed, []) is not None


def test_candidate_urls_cover_known_real_patterns():
    urls = [url for _, url in candidate_urls(date(2026, 7, 10))]
    # The two blob names the CAA actually used in 2025/2026.
    assert any("idcunim-2026" in u and "1-26" in u for u in urls)
    assert any("idcunim-2025" in u and "aip_" in u and "2-25" in u for u in urls)
    keys = {key for key, _ in candidate_urls(date(2026, 1, 5))}
    # Early in the year, last year's issues must still be probed.
    assert "4-25" in keys and "1-26" in keys


def test_failed_download_does_not_reopen_issues(tmp_path, monkeypatch):
    # 1-26 gets its issue, then the 2-26 download dies. State used to be saved
    # only at the very end, so every later run reopened the 1-26 issue.
    monkeypatch.setattr(check_update_file, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(sys, "argv", ["check_update_file.py"])
    issues = []
    monkeypatch.setattr(check_update_file, "open_github_issue",
                        lambda title, body: issues.append(title))
    monkeypatch.setattr(check_update_file, "discover", lambda session, today: {
        "1-26": "https://x/1-26.pdf", "2-26": "https://x/2-26.pdf"})
    monkeypatch.setattr(check_update_file, "analyze_pdf", lambda pdf: {
        "issue": "1/26", "effectiveDate": None, "uavRelevant": True,
        "keywords": ["א-17"], "coverText": "", "parseOk": True})

    class Response:
        content = b"%PDF-1.7"

        def raise_for_status(self):
            pass

    class Session:
        headers = {}

        def get(self, url, timeout=None, **kwargs):
            if "2-26" in url:
                raise requests.ConnectionError("connection reset")
            return Response()

    monkeypatch.setattr(check_update_file.requests, "Session", Session)
    check_update_file.main()
    check_update_file.main()
    assert len(issues) == 1

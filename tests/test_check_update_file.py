import os
import sys
from datetime import date

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
)

from check_update_file import (  # noqa: E402
    candidate_urls,
    normalize,
    parse_effective_date,
    parse_issue_number,
    squash,
    uav_hits,
)

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
    assert squash("ב-\n09\n –") == 'ב-09–'


def test_candidate_urls_cover_known_real_patterns():
    urls = [url for _, url in candidate_urls(date(2026, 7, 10))]
    # The two blob names the CAA actually used in 2025/2026.
    assert any("idcunim-2026" in u and "1-26" in u for u in urls)
    assert any("idcunim-2025" in u and "aip_" in u and "2-25" in u for u in urls)
    keys = {key for key, _ in candidate_urls(date(2026, 1, 5))}
    # Early in the year, last year's issues must still be probed.
    assert "4-25" in keys and "1-26" in keys

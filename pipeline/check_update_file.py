"""Safety-net watcher for the consolidated פמ"ת update file (קובץ עדכון N-YY).

The CAA publishes every AIP amendment as one "קובץ עדכון" PDF listed on
https://www.gov.il/he/pages/aip?chapterIndex=2. That page is bot-blocked
(403, like all gov.il HTML) but the underlying BlobFolder PDFs are not, so
this script probes the known blob URL patterns for the current and previous
year, detects new or changed issues, and checks whether the amendment
touches UAV-relevant content (chapters א-17 / ב-09, or drone keywords)
before opening a GitHub issue. Amendments that don't touch UAV content are
recorded in the state file without an issue.

This complements watch_aip.py: the chapter PDFs it watches can miss an
amendment (URL swap, republish without content diff), while the קובץ עדכון
always lists everything that changed plus the effective date.

State lives in data/.aip_update_state.json (committed by the workflow).
"""
import argparse
import json
import os
import re
import subprocess
import urllib.parse
from datetime import date

import fitz  # PyMuPDF
import requests

from common import REPO_ROOT, sha256_hex

STATE_PATH = os.path.join(REPO_ROOT, "data", ".aip_update_state.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

BLOB_BASE = "https://www.gov.il/BlobFolder"

# Observed blob-name patterns; the CAA is not consistent between years
# (2025: "aip_קובץ עדכון 2-25.pdf" in idcunim-2025; 2026: "קובץ עדכון - 1-26.pdf"
# in idcunim-2026), so every known variant is probed.
URL_PATTERNS = [
    "generalpage/idcunim-20{yy}/he/קובץ עדכון - {n}-{yy}.pdf",
    "generalpage/idcunim-20{yy}/he/קובץ עדכון {n}-{yy}.pdf",
    "generalpage/idcunim-20{yy}/he/aip_קובץ עדכון {n}-{yy}.pdf",
    "generalpage/idcunim-20{yy}/he/aip_קובץ עדכון - {n}-{yy}.pdf",
    "generalpage/updates-20{yy}/he/aip_קובץ עדכון {n}-{yy}.pdf",
]

MAX_ISSUE_PER_YEAR = 6  # the CAA publishes ~2-4 amendments a year

# A hit on any of these in the amendment's cover pages ("הוראות עדכון":
# replaced-pages list + עיקרי העדכון summary) marks it UAV-relevant.
# Matched against text with ALL whitespace removed, so multi-word keywords
# must be written squashed too (e.g. רב להב -> רבלהב).
UAV_KEYWORDS = [
    'א-17',    # restricted/danger areas chapter
    'ב-09',    # UAV & model aircraft chapter
    'כטב"ם',
    'כטב"מ',
    'כטמ"מ',
    'רחפן',
    'טיסן',
    'רבלהב',
    'BVLOS',
]

HEBREW_MONTHS = {
    "ינואר": 1, "פברואר": 2, "מרץ": 3, "מרס": 3, "אפריל": 4, "מאי": 5,
    "יוני": 6, "יולי": 7, "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10,
    "נובמבר": 11, "דצמבר": 12,
}


def normalize(text: str) -> str:
    """Collapses whitespace and unifies Hebrew gershayim/quote variants.
    PyMuPDF splits RTL runs mid-word, so downstream matching must tolerate
    arbitrary whitespace inside words."""
    text = re.sub(r'[״”“‟]', '"', text)
    return re.sub(r"\s+", " ", text)


def squash(text: str) -> str:
    """Removes ALL whitespace — the only reliable way to keyword-match text
    PyMuPDF extracted from RTL PDFs."""
    return re.sub(r"\s+", "", normalize(text))


def candidate_urls(today: date):
    """Yields (key, url) for every plausible amendment blob this year and
    last year (late-year amendments stay relevant across the rollover)."""
    for year in (today.year, today.year - 1):
        yy = f"{year % 100:02d}"
        for n in range(1, MAX_ISSUE_PER_YEAR + 1):
            for pattern in URL_PATTERNS:
                path = pattern.format(yy=yy, n=n)
                url = BLOB_BASE + "/" + urllib.parse.quote(path)
                yield f"{n}-{yy}", url


def parse_issue_number(norm_text: str):
    """'עדכון מס' 1/26' — the geresh may land before 'עדכון' after RTL
    extraction, so it is optional everywhere."""
    m = re.search(r"עדכון\s*מס\s*'?\s*(\d{1,2})\s*/\s*(\d{2})", norm_text)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def parse_effective_date(norm_text: str):
    """'תאריך כניסה לתוקף 06 אוגוסט2026' (month often glued to the year)."""
    m = re.search(
        r"כניסה\s*לתוקף\s*(\d{1,2})\s*([א-ת]+?)\s*(\d{4})", norm_text
    )
    if not m:
        return None
    day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = HEBREW_MONTHS.get(month_name)
    if not month:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def cover_pages_text(pdf_bytes: bytes) -> str:
    """Returns the raw text of the amendment's cover pages — the הוראות
    עדכון sheets that list every replaced page and the עיקרי העדכון summary.
    They always sit at the front, before the replacement pages themselves."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for i in range(min(10, doc.page_count)):
        text = doc[i].get_text()
        if "הוראות עדכון" in normalize(text):
            pages.append(text)
        elif pages:
            break  # cover block ended
    doc.close()
    return "\n".join(pages)


def uav_hits(cover_text: str):
    squashed = squash(cover_text)
    return [kw for kw in UAV_KEYWORDS if kw in squashed]


def keyword_snippets(cover_text: str, keywords, radius=90):
    """Context windows around each keyword hit, for the issue body. The text
    is RTL-garbled; the snippets only orient the human reviewer, who will
    open the PDF anyway."""
    norm = normalize(cover_text)
    spaced_ok = {kw: re.compile(r"\s*".join(map(re.escape, kw))) for kw in keywords}
    snippets = []
    for kw, rx in spaced_ok.items():
        m = rx.search(norm)
        if m:
            start = max(0, m.start() - radius)
            end = min(len(norm), m.end() + radius)
            snippets.append(f"**{kw}**: …{norm[start:end]}…")
    return snippets


def analyze_pdf(pdf_bytes: bytes):
    cover = cover_pages_text(pdf_bytes)
    norm = normalize(cover)
    hits = uav_hits(cover)
    return {
        "issue": parse_issue_number(norm),
        "effectiveDate": parse_effective_date(norm),
        "uavRelevant": bool(hits),
        "keywords": hits,
        "coverText": cover,
        "parseOk": bool(cover) and parse_issue_number(norm) is not None,
    }


def open_issue(title: str, body: str):
    try:
        subprocess.run(
            ["gh", "issue", "create", "--title", title, "--body", body],
            check=True,
        )
        print(f"Opened GitHub issue: {title}")
    except Exception as e:  # noqa: BLE001
        print(f"Could not open GitHub issue ({e}); report:\n{title}\n{body}")


def build_issue(key, url, info):
    title = f'פורסם קובץ עדכון פמ"ת {info["issue"] or key} — נדרש עדכון נתוני רחפנים'
    lines = [
        f'רת"א פרסמה קובץ עדכון פמ"ת חדש והוא נוגע לתוכן רלוונטי לרחפנים '
        f'(פרקים א-17 / ב-09 או מילות מפתח).',
        "",
        f"- **קובץ**: {url}",
        f"- **תאריך כניסה לתוקף**: {info['effectiveDate'] or 'לא זוהה'}",
        f"- **מילות מפתח שאותרו**: {', '.join(info['keywords'])}",
        "",
        "קטעים רלוונטיים מתוך עיקרי העדכון (טקסט גולמי מה-PDF, ייתכן שיבוש RTL):",
        "",
    ]
    lines += [f"- {s}" for s in keyword_snippets(info["coverText"], info["keywords"])]
    lines += [
        "",
        "יש לעבור על הקובץ, לעדכן את data/*.json בהתאם, ואז להריץ "
        "validate + build_manifest — האפליקציות יתעדכנו אוטומטית.",
    ]
    return title, "\n".join(lines)


def discover(session, today):
    """Probes candidate URLs; returns {key: url} for blobs that exist."""
    found = {}
    for key, url in candidate_urls(today):
        if key in found:
            continue
        try:
            # gov.il returns 404 to HEAD for some existing blobs (seen on the
            # 2026 files), so probe with a streamed GET and close unread.
            res = session.get(url, timeout=30, stream=True)
            if res.status_code == 200:
                found[key] = url
            res.close()
        except requests.RequestException as e:
            print(f"WARN: probe failed for {url}: {e}")
    return found


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def report(info):
    print(f"  issue: {info['issue']}, effective: {info['effectiveDate']}, "
          f"uavRelevant: {info['uavRelevant']} {info['keywords']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", help="analyze a local PDF and exit (no state, no issue)")
    parser.add_argument("--no-issue", action="store_true",
                        help="detect and update state but never open issues")
    args = parser.parse_args()

    if args.pdf:
        with open(args.pdf, "rb") as f:
            info = analyze_pdf(f.read())
        report(info)
        return

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    today = date.today()
    found = discover(session, today)
    if not found:
        print("No קובץ עדכון blobs found for this year or last — "
              "URL patterns may have changed; check the AIP page manually.")
        return

    state = load_state()
    for key, url in sorted(found.items()):
        res = session.get(url, timeout=120)
        res.raise_for_status()
        digest = sha256_hex(res.content)
        prev = state.get(key)
        if prev and prev.get("sha256") == digest:
            print(f"ok: קובץ עדכון {key} (unchanged)")
            continue

        info = analyze_pdf(res.content)
        print(f"{'CHANGED' if prev else 'NEW'}: קובץ עדכון {key} — {url}")
        report(info)

        state[key] = {
            "url": url,
            "sha256": digest,
            "issue": info["issue"],
            "effectiveDate": info["effectiveDate"],
            "uavRelevant": info["uavRelevant"],
            "keywords": info["keywords"],
            "firstSeen": state.get(key, {}).get("firstSeen") or today.isoformat(),
        }

        if args.no_issue:
            continue
        if not info["parseOk"]:
            open_issue(
                f'קובץ עדכון פמ"ת {key} — נכשל ניתוח אוטומטי, נדרשת בדיקה ידנית',
                f"לא הצלחתי לחלץ את עמודי הוראות העדכון מהקובץ:\n{url}\n\n"
                "יש לבדוק ידנית אם העדכון נוגע לפרקים א-17 / ב-09 (רחפנים).",
            )
        elif info["uavRelevant"]:
            title, body = build_issue(key, url, info)
            open_issue(title, body)
        else:
            print(f"  no UAV-relevant content in {key}; not opening an issue.")

    save_state(state)


if __name__ == "__main__":
    main()

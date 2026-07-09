"""Watches CAA/IAA source pages and PDFs for changes.

Fetches every source listed in pipeline/sources.json, hashes the (stabilized)
content, and compares against data/.source_hashes.json. On change it opens a
GitHub issue (when GH_TOKEN + gh CLI are available, i.e. inside Actions) so a
human reviews and updates data/*.json — the OTA channel then delivers the
update to every installed app within a day.

This script never edits the data files itself.
"""
import hashlib
import json
import os
import re
import subprocess

import requests

from common import REPO_ROOT

SOURCES_PATH = os.path.join(REPO_ROOT, "pipeline", "sources.json")
HASHES_PATH = os.path.join(REPO_ROOT, "data", ".source_hashes.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def stabilize_html(text: str) -> str:
    """Strips ASP.NET per-request tokens and whitespace noise so the hash only
    changes when the actual content changes."""
    text = re.sub(
        r'id="__(VIEWSTATE|VIEWSTATEGENERATOR|EVENTVALIDATION)"[^>]*value="[^"]*"',
        "",
        text,
    )
    text = re.sub(r"<input[^>]+type=\"hidden\"[^>]*>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def fetch_hash(source):
    res = requests.get(
        source["url"], headers={"User-Agent": USER_AGENT}, timeout=60
    )
    res.raise_for_status()
    if source.get("type") == "page":
        content = stabilize_html(res.text).encode("utf-8")
    else:
        content = res.content
    return hashlib.sha256(content).hexdigest()


def open_issue(changed):
    lines = [
        "זוהה שינוי במקורות רת\"א/פמ\"ת. יש לבדוק ולעדכן את קבצי data/ בהתאם:",
        "",
    ]
    for name, url in changed:
        lines.append(f"- **{name}**: {url}")
    lines += [
        "",
        "לאחר עדכון הקבצים והרצת validate + build_manifest, "
        "כל האפליקציות יתעדכנו אוטומטית.",
    ]
    body = "\n".join(lines)
    title = f"זוהה עדכון במקורות רת\"א ({len(changed)} מקורות)"
    try:
        subprocess.run(
            ["gh", "issue", "create", "--title", title, "--body", body],
            check=True,
        )
        print("Opened GitHub issue about source changes.")
    except Exception as e:  # noqa: BLE001
        print(f"Could not open GitHub issue ({e}); change report:\n{body}")


def main():
    if not os.path.exists(SOURCES_PATH):
        print("pipeline/sources.json missing; nothing to watch.")
        return
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        sources = json.load(f)

    old_hashes = {}
    if os.path.exists(HASHES_PATH):
        with open(HASHES_PATH, "r", encoding="utf-8") as f:
            old_hashes = json.load(f)

    new_hashes = dict(old_hashes)
    changed = []
    for source in sources:
        name, url = source["name"], source["url"]
        try:
            digest = fetch_hash(source)
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not fetch {name} ({url}): {e}")
            continue
        if old_hashes.get(name) and old_hashes[name] != digest:
            changed.append((name, url))
            print(f"CHANGED: {name}")
        else:
            print(f"ok: {name}")
        new_hashes[name] = digest

    with open(HASHES_PATH, "w", encoding="utf-8") as f:
        json.dump(new_hashes, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    if changed and old_hashes:
        open_issue(changed)


if __name__ == "__main__":
    main()

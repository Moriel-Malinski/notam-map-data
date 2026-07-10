"""Shared helpers for the notam-map-data pipeline."""
import gzip
import hashlib
import io
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")

# The four files the app knows how to consume OTA.
MANAGED_FILES = [
    "zones_data.json",
    "a17_additions.json",
    "ratag_output.json",
    "rtg_closures.json",
]

# Generous Israel bounding box (Eilat to Mount Hermon, Mediterranean to Jordan).
LAT_MIN, LAT_MAX = 29.0, 33.6
LON_MIN, LON_MAX = 33.5, 36.5

# Hosting. Override via env when the GitHub user/repo differs.
PAGES_BASE_URL = os.environ.get(
    "PAGES_BASE_URL", "https://moriel-malinski.github.io/notam-map-data"
)
RAW_BASE_URL = os.environ.get(
    "RAW_BASE_URL",
    "https://raw.githubusercontent.com/Moriel-Malinski/notam-map-data/main/docs",
)


def canonical_bytes(obj) -> bytes:
    """Deterministic JSON serialization — the exact bytes that get published
    (gzipped) and that the app verifies with sha256."""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gzip_deterministic(data: bytes) -> bytes:
    """gzip with a fixed mtime so identical content yields identical bytes."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as f:
        f.write(data)
    return buf.getvalue()


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj, indent=2):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        f.write("\n")


def in_israel_bbox(lat: float, lon: float) -> bool:
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


def open_github_issue(title: str, body: str):
    """Opens an issue via the gh CLI (available inside Actions); outside
    Actions it degrades to printing the report."""
    import subprocess

    try:
        subprocess.run(
            ["gh", "issue", "create", "--title", title, "--body", body],
            check=True,
        )
        print(f"Opened GitHub issue: {title}")
    except Exception as e:  # noqa: BLE001
        print(f"Could not open GitHub issue ({e}); report:\n{title}\n{body}")

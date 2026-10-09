"""Publishes data/ into docs/ (the GitHub Pages root).

For each managed file: serialize canonically, compare sha256 against the
committed manifest, and only when content actually changed — bump the file
version, write the gzipped payload to docs/v1/, and bump the global
dataVersion. A run with no data changes rewrites nothing, so the workflow's
"commit if changed" step stays silent.
"""
import os
import re
from datetime import datetime, timezone

from common import (
    DATA_DIR,
    DOCS_DIR,
    MANAGED_FILES,
    PAGES_BASE_URL,
    RAW_BASE_URL,
    canonical_bytes,
    gzip_deterministic,
    load_json,
    save_json,
    sha256_hex,
)

SOURCES = {
    "zones_data.json": "AIP Israel ENR / רשות התעופה האזרחית פמ\"ת",
    "a17_additions.json": "פמ\"ת א-17 נספחים / רשות התעופה האזרחית",
    "ratag_output.json": "רשות הטבע והגנים (KMZ)",
    "rtg_closures.json": "פמ\"ת א-17 נספח ה' / רשות הטבע והגנים",
}

SCHEMA_VERSION = 1
MIN_APP_VERSION = "1.4.0"

# The newest app release, published as the manifest's "app" section: apps
# older than latestVersion offer the download and list the notes. Edit this
# file when releasing a new app version.
APP_RELEASE_PATH = os.path.join(DATA_DIR, "app_release.json")


def load_app_release(path=APP_RELEASE_PATH):
    """data/app_release.json -> the manifest's "app" section, or None when
    the file doesn't exist. A malformed file raises ValueError (fails the
    run loudly rather than announcing a broken release)."""
    if not os.path.exists(path):
        return None
    release = load_json(path)
    version = release.get("latestVersion")
    url = release.get("downloadUrl")
    notes = release.get("notes", [])
    if not isinstance(version, str) or not re.fullmatch(r"\d+(\.\d+)*", version):
        raise ValueError(f"app_release.json: bad latestVersion {version!r}")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(f"app_release.json: downloadUrl must be https, got {url!r}")
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise ValueError("app_release.json: notes must be a list of strings")
    return {"latestVersion": version, "downloadUrl": url, "notes": notes}


def main():
    manifest_path = os.path.join(DOCS_DIR, "manifest.json")
    manifest = (
        load_json(manifest_path)
        if os.path.exists(manifest_path)
        else {"schemaVersion": SCHEMA_VERSION, "dataVersion": 0, "files": {}}
    )
    files = manifest.setdefault("files", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    v1_dir = os.path.join(DOCS_DIR, "v1")
    os.makedirs(v1_dir, exist_ok=True)

    changed = []
    for name in MANAGED_FILES:
        data = load_json(os.path.join(DATA_DIR, name))
        payload = canonical_bytes(data)
        digest = sha256_hex(payload)

        entry = files.get(name, {})
        gz_path = os.path.join(v1_dir, f"{name}.gz")
        if entry.get("sha256") == digest and os.path.exists(gz_path):
            continue

        gz = gzip_deterministic(payload)
        with open(gz_path, "wb") as f:
            f.write(gz)

        files[name] = {
            "version": int(entry.get("version", 0)) + 1,
            "url": f"{PAGES_BASE_URL}/v1/{name}.gz",
            "fallbackUrl": f"{RAW_BASE_URL}/v1/{name}.gz",
            "sha256": digest,
            "gzSha256": sha256_hex(gz),
            "sizeBytes": len(payload),
            "gzSizeBytes": len(gz),
            "count": len(data) if isinstance(data, list) else None,
            "updatedAt": now,
            "source": SOURCES.get(name, ""),
        }
        changed.append(name)
        print(
            f"{name}: v{files[name]['version']} "
            f"({len(payload):,} B -> {len(gz):,} B gz)"
        )

    # A new app release alone rewrites the manifest without bumping
    # dataVersion — apps have no data to download for it.
    app = load_app_release()
    app_changed = app is not None and manifest.get("app") != app
    if app is not None:
        manifest["app"] = app

    if changed:
        manifest["schemaVersion"] = SCHEMA_VERSION
        manifest["minAppVersion"] = MIN_APP_VERSION
        manifest["dataVersion"] = int(manifest.get("dataVersion", 0)) + 1
        manifest["generatedAt"] = now
        print(
            f"manifest.json: dataVersion {manifest['dataVersion']} "
            f"({', '.join(changed)})"
        )
    if app_changed:
        print(f"manifest.json: app release {app['latestVersion']}")
    if changed or app_changed:
        save_json(manifest_path, manifest)
    else:
        print("No content changes; manifest untouched.")


if __name__ == "__main__":
    main()

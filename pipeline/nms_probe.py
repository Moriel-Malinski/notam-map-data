"""Connection check for the NMS-API: token, /ping, and which query returns
the NOTAMs of Israel (LL*).

Prints counts, the fields seen and ONE sample NOTAM. Never prints the
credentials or the token. With --dump DIR the raw answers are also saved
there (pick a folder outside the repository) to build test fixtures from.

    python pipeline/nms_probe.py [--dump DIR] [--wide]

--wide adds the heavy queries (whole INTERNATIONAL checklist, all location
series, a 23 h delta of every NOTAM).
"""
import argparse
import collections
import datetime as dt
import json
import os
import sys

from nms_client import NmsAuthError, NmsClient, NmsHttpError, is_israeli, notam_of


def _dump(folder: str | None, name: str, body) -> None:
    if not folder:
        return
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)


def _summary(label: str, features: list[dict], pool: dict) -> None:
    notams = [notam_of(f) for f in features]
    israeli = [n for n in notams if is_israeli(n)]
    series = collections.Counter(n.get("series", "?") for n in israeli)
    print(f"{label}: {len(features)} returned, {len(israeli)} Israeli (LL*)"
          + (f", series {dict(series)}" if israeli else ""))
    for f, n in zip(features, notams):
        if is_israeli(n) and n.get("id"):
            pool.setdefault(n["id"], f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump")
    ap.add_argument("--wide", action="store_true")
    args = ap.parse_args()

    client = NmsClient()
    print(f"host: {client.host}")
    try:
        client.authenticate()
    except NmsAuthError as e:
        print(f"AUTH FAILED: {e}", file=sys.stderr)
        return 2
    print("token: OK")

    try:
        res = client.get("/ping")
        print(f"/ping: HTTP {res.status_code} {res.text[:120]!r}")
    except NmsHttpError as e:
        print(f"/ping FAILED: {e}", file=sys.stderr)
        return 1

    pool: dict[str, dict] = {}
    queries = [
        ("location=LLLL", {"location": "LLLL"}),
        ("location=LLBG", {"location": "LLBG"}),
        ("accountability=LLLLYNYX", {"accountability": "LLLLYNYX"}),
        ("INTERNATIONAL+location=LLLL",
         {"classification": "INTERNATIONAL", "location": "LLLL"}),
        ("circle north (32.3,35.0,100NM)",
         {"latitude": 32.3, "longitude": 35.0, "radius": 100}),
        ("circle south (30.3,34.9,100NM)",
         {"latitude": 30.3, "longitude": 34.9, "radius": 100}),
        ("freeText=LLLL", {"freeText": "LLLL"}),
    ]
    if args.wide:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=23)
        queries.append(("delta 23h (all NOTAMs)",
                        {"lastUpdatedDate": since.strftime("%Y-%m-%dT%H:%M:%SZ")}))
    for label, params in queries:
        try:
            features = client.notams(**params)
        except (NmsHttpError, OSError) as e:
            print(f"{label}: FAILED ({e})")
            continue
        _dump(args.dump, "notams_" + "".join(c if c.isalnum() else "_" for c in label),
              features)
        _summary(label, features, pool)

    # Checklist: light rows (id, number, location) - shows what exists.
    checklists = [("checklist location=LLLL", {"location": "LLLL"})]
    if args.wide:
        checklists.append(("checklist INTERNATIONAL", {"classification": "INTERNATIONAL"}))
    for label, params in checklists:
        try:
            body = client.get_json("/notams/checklist", params=params)
            rows = (body.get("data") or {}).get("checklist") or []
        except (NmsHttpError, OSError) as e:
            print(f"{label}: FAILED ({e})")
            continue
        _dump(args.dump, label.replace(" ", "_").replace("=", "_"), rows)
        ll = [r for r in rows if str(r.get("icaoLocation", "")).upper().startswith("LL")]
        locs = collections.Counter(r.get("icaoLocation") for r in ll)
        print(f"{label}: {len(rows)} rows, {len(ll)} Israeli; locations {dict(locs)}")

    if args.wide:
        try:
            body = client.get_json("/locationseries")
            rows = (body.get("data") or {}).get("locationSeries") or []
            ll = [r for r in rows if str(r.get("icaoId", "")).upper().startswith("LL")]
            _dump(args.dump, "locationseries_LL", ll)
            print(f"locationseries: {len(rows)} rows, {len(ll)} Israeli: "
                  f"{sorted(str(r.get('icaoId')) for r in ll)}")
        except (NmsHttpError, OSError) as e:
            print(f"locationseries: FAILED ({e})")

    print(f"\nDistinct Israeli NOTAMs over all queries: {len(pool)}")
    if pool:
        fields = collections.Counter()
        for f in pool.values():
            fields.update(k for k, v in notam_of(f).items() if v not in ("", None))
        print("notam fields (filled count):")
        for k, c in sorted(fields.items()):
            print(f"  {k}: {c}/{len(pool)}")
        geo = collections.Counter(
            (f.get("geometry") or {}).get("type", "none") for f in pool.values()
        )
        print(f"geometry types: {dict(geo)}")
        sample = next(iter(pool.values()))
        print("\nOne sample feature:")
        print(json.dumps(sample, ensure_ascii=False, indent=1)[:3000])
    else:
        print("No Israeli NOTAMs came back. Staging holds test data, so this "
              "alone does not mean the code is wrong.")
    print(f"\nrequests sent: {client.request_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

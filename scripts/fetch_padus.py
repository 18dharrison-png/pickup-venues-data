#!/usr/bin/env python3
"""Fetch PAD-US public-access land for one state into a GeoJSON file.

Why not the ScienceBase per-state zip? The 460 MB California package is the
"official" download, but every ScienceBase file URL answered this project's
runner with a 4 KB HTML app shell instead of the archive, so the build could not
depend on it.

The same PAD-US 4.1 data is served as an ArcGIS FeatureServer layer, and that
endpoint is reachable and queryable. It returns at most 2000 records per request
and sets `exceededTransferLimit`, so this script pages with resultOffset plus
orderByFields=OBJECTID (which the layer advertises), retries transient failures
with backoff, and pauses between pages to stay inside the service's limits.

It keeps only public-access land whose owner/manager is a federal, state, local
or regional agency, and writes that small extract as GeoJSON. Everything else
never leaves the process, so the builder's spatial index cannot see private land.

Progress is kept in a .progress file: a run that dies halfway resumes where it
stopped instead of re-paging the state, and the workflow caches the finished
GeoJSON between runs.

  python3 scripts/fetch_padus.py --state CA --out padus/CA.geojson
  python3 scripts/fetch_padus.py --bbox -118.65,33.85,-118.15,34.25 --out /tmp/la.geojson

PAD-US is public domain (USGS GAP). Credited in README.md.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import public_filter

SERVICE = (
    "https://services1.arcgis.com/4usxdjWKL0Lq9x6D/arcgis/rest/services/"
    "US_Protected_Areas_Database__PADUS/FeatureServer/1/query"
)
OUT_FIELDS = "OBJECTID,Unit_Nm,Pub_Access,Own_Type,Des_Tp,GAP_Sts,State_Nm"
PAGE_SIZE = 2000          # the layer's maxRecordCount
PAGE_PAUSE = 0.6          # seconds between pages: stay well inside the limit
ATTEMPTS = 5
USER_AGENT = "pickup-venues-data/1.0 (+https://github.com/18dharrison-png/pickup)"

# Geofabrik state names -> USPS codes, so the workflow can pass either.
STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district-of-columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new-hampshire": "NH", "new-jersey": "NJ", "new-mexico": "NM", "new-york": "NY",
    "north-carolina": "NC", "north-dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode-island": "RI",
    "south-carolina": "SC", "south-dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west-virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def state_code(value):
    key = (value or "").strip().lower()
    if key in STATE_CODES:
        return STATE_CODES[key]
    if len(key) == 2 and key.upper() in STATE_CODES.values():
        return key.upper()
    raise SystemExit(f"unknown state '{value}'")


def query_url(where, bbox, offset):
    params = {
        "where": where,
        "outFields": OUT_FIELDS,
        "returnGeometry": "true",
        "outSR": "4326",
        "geometryPrecision": "5",
        "resultRecordCount": str(PAGE_SIZE),
        "resultOffset": str(offset),
        "orderByFields": "OBJECTID",
        "f": "geojson",
    }
    if bbox:
        params.update({
            "geometry": ",".join(f"{value:.5f}" for value in bbox),
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
        })
    return SERVICE + "?" + urllib.parse.urlencode(params)


def fetch_page(url, attempts=ATTEMPTS):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = json.load(response)
            if "error" in payload:
                raise ValueError(f"service error: {payload['error']}")
            return payload.get("features") or [], bool(payload.get("exceededTransferLimit"))
        except Exception as error:  # throttle, timeout, torn response, HTML page
            last = error
            wait = min(60, 5 * 2 ** (attempt - 1))
            print(f"  page attempt {attempt}/{attempts} failed ({error}); retrying in {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise SystemExit(f"PAD-US query failed after {attempts} attempts: {last}")


def fetch(where, bbox, out_path, label):
    """Page every record matching `where`/`bbox` into a filtered GeoJSON."""
    progress = out_path + ".progress"
    pages_path = out_path + ".pages.jsonl"
    offset = 0
    if os.path.exists(progress) and os.path.exists(pages_path):
        with open(progress, encoding="utf-8") as handle:
            offset = int(handle.read().strip() or 0)
        print(f"  resuming {label} at offset {offset}")

    total = 0
    kept = 0
    while True:
        features, more = fetch_page(query_url(where, bbox, offset))
        if not features:
            break
        with open(pages_path, "a", encoding="utf-8") as handle:
            for feature in features:
                handle.write(json.dumps(feature, separators=(",", ":")) + "\n")
        total += len(features)
        offset += len(features)
        with open(progress, "w", encoding="utf-8") as handle:
            handle.write(str(offset))
        print(f"  {label}: {total} polygons fetched", flush=True)
        if not more and len(features) < PAGE_SIZE:
            break
        time.sleep(PAGE_PAUSE)

    with open(pages_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            feature = json.loads(line)
            if public_filter.is_public_padus(feature.get("properties") or {}):
                kept += 1
                yield feature

    os.remove(pages_path)
    os.remove(progress)
    print(f"  {label}: {kept} of {total} polygons are public-access public land")


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", help="state name or code, e.g. california or CA")
    parser.add_argument("--bbox", help="minx,miny,maxx,maxy instead of a whole state")
    parser.add_argument("--out", required=True, help="GeoJSON to write")
    parser.add_argument("--force", action="store_true", help="re-fetch even if cached")
    args = parser.parse_args(argv)

    if not args.state and not args.bbox:
        raise SystemExit("give --state or --bbox")

    if os.path.exists(args.out) and not args.force and os.path.getsize(args.out) > 1024:
        print(f"{args.out} already present ({os.path.getsize(args.out)/1e6:.1f} MB); "
              f"pass --force to refresh")
        return

    if args.bbox:
        bbox = tuple(float(value) for value in args.bbox.split(","))
        if len(bbox) != 4:
            raise SystemExit("--bbox wants minx,miny,maxx,maxy")
        where, label = "1=1", "bbox"
    else:
        bbox, label = None, state_code(args.state)
        where = f"State_Nm='{label}'"

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    features = list(fetch(where, bbox, args.out, label))
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "features": features}, handle,
                  separators=(",", ":"))
    print(f"{args.out}: {len(features)} public polygons, "
          f"{os.path.getsize(args.out)/1e6:.1f} MB")


if __name__ == "__main__":
    main(sys.argv[1:])

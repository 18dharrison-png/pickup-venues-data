#!/usr/bin/env python3
"""Fetch PAD-US public-access land for one state into a GeoJSON file.

Why not the ScienceBase per-state zip? The 460 MB California package is the
"official" download, but every ScienceBase file URL answered this project's
runner with a 4 KB HTML app shell instead of the archive, so the build could not
depend on it.

The same PAD-US 4.1 data is served as an ArcGIS FeatureServer layer, and that
endpoint is reachable and queryable. It returns at most 2000 records per request
and sets `exceededTransferLimit`, so this script pages with resultOffset plus
orderByFields=OBJECTID (which the layer advertises), pauses between pages, and
retries every request — including the very first one — with exponential backoff.
That last part matters: the service answers 504s and truncated bodies whenever
several state jobs start together, and to an un-retried request that looks
exactly like a permanent failure.

Use the *nationwide* layer. A previous endpoint
(services1.arcgis.com/4usxdjWKL0Lq9x6D, "US_Protected_Areas_Database__PADUS")
looked identical and answered `State_Nm='CA'` with 33,623 polygons, but it holds
only California: `distinct State_Nm` came back as exactly `['CA']`, so every
other state fetched an empty page. That is why a 51-state run failed in every
state except the cached one. PADUS4_1ManagerType is the national layer: 655,335
records, a State_Nm domain covering all 50 states plus DC, and 33,840 polygons
for California. `assert_nationwide` proves the source still has more than one
state before a build believes it.

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
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import public_filter

SERVICE = (
    "https://services.arcgis.com/v01gqwM5QqNysAAi/ArcGIS/rest/services/"
    "PADUS_Manager_Type/FeatureServer/0/query"
)
OUT_FIELDS = "OBJECTID,Unit_Nm,Pub_Access,Own_Type,Des_Tp,GAP_Sts,State_Nm"
# Half the layer's maxRecordCount: a 2000-record page of California is a 25 MB
# response, and the service truncates those (IncompleteRead) under load. Smaller
# pages cost a few more requests and stop losing a whole state to one cut body.
PAGE_SIZE = 1000
PAGE_PAUSE = 1.0          # base seconds between pages: stay inside the limits
PAGE_JITTER = 1.0         # plus up to this much, so parallel jobs drift apart
# Six attempts with backoff 5+10+20+40+60+60 s: about two minutes of patience
# before a state gives up, which is what the service's busy spells need.
ATTEMPTS = 6
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


def get_json(url, attempts=ATTEMPTS):
    """GET a JSON document, retrying the way page fetches do.

    Every request here must survive a busy service. The PAD-US layer answers
    504s, throttling and truncated bodies whenever several state jobs start
    together, and a single un-retried request is a coin flip: that is exactly
    how a nationwide run lost ten states inside one second each.
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = json.load(response)
            if isinstance(payload, dict) and "error" in payload:
                raise ValueError(f"service error: {payload['error']}")
            # A real ArcGIS response always carries a features list. Anything
            # else — a proxy page, a bare error object, a truncated body that
            # still parses — must be retried, not read as "this state is empty".
            if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
                raise ValueError(f"unexpected response shape: {str(payload)[:140]}")
            return payload
        except Exception as error:  # throttle, timeout, torn response, HTML page
            last = error
            wait = min(60, 5 * 2 ** (attempt - 1))
            print(f"  request attempt {attempt}/{attempts} failed ({error}); retrying in {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise SystemExit(f"PAD-US request failed after {attempts} attempts: {last}")


def assert_nationwide():
    """The set of states the source actually has, refusing a look-alike layer.

    A California-only layer answered `State_Nm='CA'` with tens of thousands of
    polygons, so nothing looked wrong until 50 other states each fetched an
    empty page. Counting distinct State_Nm values is one cheap request and makes
    that failure impossible to miss.
    """
    params = {"where": "1=1", "outFields": "State_Nm", "returnGeometry": "false",
              "returnDistinctValues": "true", "resultRecordCount": "200", "f": "json"}
    url = SERVICE + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(1, 4):
        payload = get_json(url)
        states = {str((feature.get("attributes") or {}).get("State_Nm") or "").strip()
                  for feature in payload.get("features") or []}
        states.discard("")
        if len(states) >= 10:
            return states
        # An empty or attribute-less answer is not proof that the layer lost 50
        # states: three state jobs hit the service at once and it answers short
        # or bare under that load. Retry before believing it.
        last = payload
        print(f"  source check {attempt}/3 saw {len(states)} state(s) in "
              f"{len(payload.get('features') or [])} row(s); retrying", file=sys.stderr)
        time.sleep(5 * attempt)
    raise SystemExit(
        f"PAD-US source is not nationwide: {len(states)} state(s) after 3 tries, out of "
        f"{len(last.get('features') or [])} row(s) ({str(last)[:200]}). "
        f"Refusing to write empty extracts."
    )


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
    payload = get_json(url, attempts=attempts)
    return payload.get("features") or [], bool(payload.get("exceededTransferLimit"))


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
        if not features and offset == 0:
            # A state with no polygons at all would silently drop almost every
            # venue in it, so an empty first page is retried before believed.
            for attempt in range(1, 4):
                print(f"  {label}: empty first page, retry {attempt}/3", file=sys.stderr)
                time.sleep(5 * attempt)
                features, more = fetch_page(query_url(where, bbox, offset))
                if features:
                    break
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
        # Jittered, not fixed: three state jobs paging in lockstep is what the
        # service chokes on, and a fixed pause keeps them in lockstep forever.
        time.sleep(PAGE_PAUSE + random.uniform(0, PAGE_JITTER))

    if total == 0:
        # Fail loudly instead of publishing a state with no public land: that
        # silently drops almost every venue in it. Opening a pages file that was
        # never written is what made a 51-state run die in one second per state.
        raise SystemExit(
            f"PAD-US returned no polygons for {label}. The service is probably "
            f"unavailable or the state filter matched nothing; not writing {out_path}."
        )

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

    states = assert_nationwide()
    if label != "bbox" and label not in states:
        raise SystemExit(f"PAD-US has no polygons for {label}: it is missing from the source")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    features = list(fetch(where, bbox, args.out, label))
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "features": features}, handle,
                  separators=(",", ":"))
    print(f"{args.out}: {len(features)} public polygons, "
          f"{os.path.getsize(args.out)/1e6:.1f} MB")


if __name__ == "__main__":
    main(sys.argv[1:])

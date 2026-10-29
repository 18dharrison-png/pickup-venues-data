#!/usr/bin/env python3
"""Check a built state against named real-world cases, and fail if one breaks.

The filter is only as good as its proof, so this runs in CI right after a build
and answers the questions that matter in plain language: is this specific court
gone, is that specific park still there, does this public high school appear,
does this private school stay out, and are the labelled schools really schools.

Usage:
  python3 spot_checks.py --venues out/california.ndjson.gz \
      --dropped out/california.dropped.jsonl --report out/california.report.json \
      [--state california]

Exit code is 1 when any case disagrees with what it should be, so a regression
in the rules shows up as a failed job rather than a quiet drop in coverage.
"""
import argparse
import gzip
import json
import math
import sys

# Cases people can verify by hand in a map app. "kept" means at least one venue
# matches; "none" means nothing at that spot may be kept.
CASES = {
    "california": [
        {"label": "way/1317683670 unverified 'Basketball courts' near Rancho Park",
         "id": "way/1317683670", "expect": "none"},
        {"label": "Reed Park, Santa Monica", "near": (34.022498, -118.49582), "expect": "kept"},
        {"label": "Penmar Recreation Center", "near": (34.006159, -118.457272), "expect": "kept"},
        {"label": "Venice Beach courts", "near": (34.038524, -118.543019), "expect": "kept"},
        {"label": "Pan Pacific Park", "near": (34.073141, -118.354812), "expect": "kept"},
        {"label": "Rancho Park site (OSM: Cheviot Hills Recreation Center)",
         "near": (34.045673, -118.409217), "expect": "kept"},
        {"label": "Venice High School (public, grades 9-12)",
         "near": (33.996932, -118.444048), "expect": "kept", "v": "school"},
        {"label": "Palisades Charter High School (public, grades 9-12)",
         "near": (34.047871, -118.531105), "expect": "kept", "v": "school"},
        {"label": "Fairfax High School (public, grades 9-12)",
         "near": (34.08284, -118.359927), "expect": "kept", "v": "school"},
        {"label": "Hamilton High School (public, grades 9-12)",
         "near": (34.034125, -118.390578), "expect": "kept", "v": "school"},
        # A radius around Loyola would also catch neighbouring public schools,
        # so this one is checked against the OSM school area itself: the build
        # reports how many courts inside each unproven school area were kept.
        {"label": "Loyola High School (private: must stay out)",
         "area": "loyola high school", "expect": "none"},
        {"label": "Wildwood School (private, Mar Vista: must stay out)",
         "area": "wildwood school", "expect": "none"},
        {"label": "Grand View Boulevard Elementary (must not be a school)",
         "area": "grand view boulevard", "expect": "none"},
        # The fields are inside the school site, so they must join the school
        # venue rather than becoming a separate "park" pin.
        {"label": "Fairfax Senior High football/baseball fields are the school",
         "name": "fairfax senior high", "expect": "kept", "v": "school",
         "sports": ["american_football", "baseball"]},
    ],
}


def metres(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    dlat = (lat1 - lat2) * 111_000
    dlon = (lon1 - lon2) * 111_000 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


def load_venues(path):
    out = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_dropped(path):
    out = []
    if not path:
        return out
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def match_area(case, report):
    """(kept, dropped, name) for an OSM school area the build could not prove public."""
    wanted = case["area"].lower()
    for area in (report or {}).get("unproven_school_areas", []):
        if wanted in (area.get("name") or "").lower():
            return area["kept"], area["dropped"], area.get("name") or ""
    return None


def matches(case, venues, dropped):
    if "id" in case:
        kept = [v for v in venues if v["id"] == case["id"]]
        gone = [d for d in dropped if d["id"] == case["id"]]
        return kept, gone
    lat, lon = case["near"]
    radius = case.get("radius", 400)
    kept = [v for v in venues if metres((lat, lon), (v["la"], v["lo"])) <= radius]
    if case.get("v"):
        kept = [v for v in kept if v["v"] == case["v"]]
    gone = [d for d in dropped if metres((lat, lon), (d["lat"], d["lon"])) <= radius]
    return kept, gone


def describe(item, key="n"):
    name = item.get(key) or "(unnamed)"
    return f"{name} ({item.get('v', 'dropped')}) {item['la']},{item['lo']} {item['id']}"


def check_named(case, venues):
    """A venue matched by name must exist with the expected verdict and sports."""
    wanted = case["name"].lower()
    found = [v for v in venues if wanted in (v.get("n") or "").lower()]
    if case.get("v"):
        found = [v for v in found if v["v"] == case["v"]]
    missing = [sport for sport in case.get("sports", [])
               if not any(sport in v.get("s", {}) for v in found)]
    ok = bool(found) and not missing
    detail = describe(found[0]) if found else "no venue with that name"
    if found and missing:
        detail += f" | missing sports: {', '.join(missing)}"
    elif found and case.get("sports"):
        detail += f" | sports: {', '.join(sorted(found[0]['s']))}"
    return ok, detail


PRIMARY_WORDS = ("elementary", "middle", "primary", "grade school")
SECONDARY_WORDS = ("high", "secondary", "senior", "junior", "7-12", "9-12")


def check_school_names(venues):
    """No "school" may be named only as an elementary/middle school.

    A combined name is legitimate — Hercules Middle/High, Bellflower Middle &
    High, Pescadero Middle/High all serve grades 9-12 — so a name is only wrong
    when it carries a primary word and no secondary one.
    """
    offenders = []
    for venue in venues:
        if venue["v"] != "school":
            continue
        name = (venue.get("n") or "").lower()
        if (any(word in name for word in PRIMARY_WORDS)
                and not any(word in name for word in SECONDARY_WORDS)):
            offenders.append(venue["n"])
    return not offenders, offenders


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="california", choices=sorted(CASES))
    parser.add_argument("--venues", required=True)
    parser.add_argument("--dropped")
    parser.add_argument("--report", help="build report JSON (needed by area cases)")
    args = parser.parse_args(argv)

    venues = load_venues(args.venues)
    dropped = load_dropped(args.dropped)
    report = None
    if args.report:
        with open(args.report, encoding="utf-8") as handle:
            report = json.load(handle)
    failures = 0
    print(f"{args.state}: {len(venues)} kept venues, {len(dropped)} dropped courts")
    for case in CASES[args.state]:
        if "area" in case:
            found = match_area(case, report)
            if found is None:
                failures += 1
                print(f"FAIL  {case['label']}: school area not found in the build at all")
                continue
            kept_count, gone_count, area_name = found
            ok = kept_count == 0 and gone_count > 0
            failures += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'}  {case['label']}: {area_name} kept "
                  f"{kept_count}, dropped {gone_count}")
            continue
        if "name" in case:
            ok, detail = check_named(case, venues)
            failures += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'}  {case['label']}: {detail}")
            continue
        kept, gone = matches(case, venues, dropped)
        if case["expect"] == "kept":
            ok = bool(kept)
            verdict = "PASS" if ok else "FAIL"
            detail = describe(kept[0]) if kept else "nothing kept here"
            if ok and case.get("v"):
                detail += f"  [{len(kept)} kept within {case.get('radius', 400)} m]"
        else:
            ok = not kept
            verdict = "PASS" if ok else "FAIL"
            if kept:
                detail = f"{len(kept)} wrongly kept, e.g. {describe(kept[0])}"
            else:
                detail = f"not kept ({len(gone)} courts dropped here)"
        failures += 0 if ok else 1
        print(f"{verdict}  {case['label']}: {detail}")

    ok, offenders = check_school_names(venues)
    failures += 0 if ok else 1
    if ok:
        print("PASS  no elementary/middle school is labelled a school")
    else:
        print(f"FAIL  {len(offenders)} elementary/middle schools labelled a school, "
              f"e.g. {', '.join(offenders[:5])}")

    schools = sum(1 for v in venues if v["v"] == "school")
    print(f"\nschool venues in {args.state}: {schools}")

    if failures:
        print(f"{failures} case(s) disagree with the expected result", file=sys.stderr)
        return 1
    print("all cases as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

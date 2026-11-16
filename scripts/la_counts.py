#!/usr/bin/env python3
"""Count Los Angeles venues before and after a build, for the build report.

The audit that started this work compared what the cells showed against the
city's own lists, so a rebuild has to answer the same question: how many venues
and courts does LA have now, and how many did it have before? This prints both
and writes them as JSON, which the dry-run workflow uploads as an artifact.

  python scripts/la_counts.py --before before.ndjson.gz --after after.ndjson.gz \\
      --out counts.json

`--before` may be missing (a first build); the counts are then null rather than
an error, and the report says so.
"""
import argparse
import collections
import gzip
import json
import os

# The audit's two boxes: the city of Los Angeles, and everything the state file
# keeps around it. (lon0, lat0, lon1, lat1)
CITY = (-118.65, 33.85, -118.15, 34.25)
COUNTY = (-118.75, 33.70, -117.55, 34.40)

# Sites the audit names, so a rebuild can say "this one is back" without a human
# squinting at pins. Each is (label, lat, lon, radius_m).
WATCHED = [
    ("Griffith Park", 34.1344, -118.3051, 3000),
    ("Vermont Square Park", 34.0003, -118.2972, 400),
    ("Exposition Park", 34.0183, -118.3002, 600),
    ("Hansen Dam sports complex", 34.2700, -118.3950, 1200),
    ("Ernest E. Debs Regional Park", 34.0947, -118.1946, 900),
    ("Balboa Sports Center", 34.1763, -118.5035, 500),
    ("Peck Park", 33.7500, -118.3042, 500),
    ("Westchester Park", 33.9582, -118.4153, 600),
    ("Westwood Park", 34.0532, -118.4481, 400),
    ("Algin Sutton Recreation Center", 33.9578, -118.2869, 400),
    ("Venice Beach Recreation Center", 33.9857, -118.4727, 500),
    ("Chatsworth Park South", 34.2610, -118.6174, 700),
    # The courts, not the 4730 Crystal Springs Dr address: that address sits at
    # Griffith Park Boys Camp, 1.5 km from the four tennis courts.
    ("Park Center, Griffith Park", 34.133228, -118.282872, 500),
]


def metres(lat1, lon1, lat2, lon2):
    """Approximate distance in metres (equirectangular; plenty for a box)."""
    import math
    dlon = (lon1 - lon2) * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlon, lat1 - lat2) * 111_320.0


def read(path):
    if not path or not os.path.exists(path):
        return None
    venues = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                venues.append(json.loads(line))
    return venues


def inside(venue, box):
    lon0, lat0, lon1, lat1 = box
    return lon0 <= venue.get("lo", 0) <= lon1 and lat0 <= venue.get("la", 0) <= lat1


def summary(venues, box):
    if venues is None:
        return None
    selected = [venue for venue in venues if inside(venue, box)]
    sports = collections.Counter()
    for venue in selected:
        for sport, count in (venue.get("s") or {}).items():
            sports[sport] += count
    return {
        "venues": len(selected),
        "courts_by_sport": dict(sports.most_common()),
        "venues_with_pickleball": sum(1 for v in selected if (v.get("s") or {}).get("pickleball")),
        "pickleball_courts": sports.get("pickleball", 0),
    }


def watched(venues):
    if venues is None:
        return None
    found = []
    for label, lat, lon, radius in WATCHED:
        near = [venue for venue in venues
                if metres(lat, lon, venue.get("la", 0), venue.get("lo", 0)) <= radius]
        found.append({
            "label": label,
            "venues": [
                {"id": venue.get("id"), "n": venue.get("n"),
                 "s": dict(sorted((venue.get("s") or {}).items(), key=lambda kv: (-kv[1], kv[0])))}
                for venue in near[:6]
            ],
        })
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", help="the state file published today")
    parser.add_argument("--after", required=True, help="the state file just built")
    parser.add_argument("--out", required=True, help="where to write the JSON")
    args = parser.parse_args(argv)

    before = read(args.before)
    after = read(args.after)
    if after is None:
        raise SystemExit(f"no built state file at {args.after}")

    report = {
        "before": {"la_city": summary(before, CITY), "la_county": summary(before, COUNTY)},
        "after": {"la_city": summary(after, CITY), "la_county": summary(after, COUNTY)},
        "watched": {"before": watched(before), "after": watched(after)},
        "state_venues": {"before": len(before) if before else None, "after": len(after)},
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)

    def line(label, name, key):
        old = (report["before"][name] or {}).get(key)
        new = (report["after"][name] or {}).get(key)
        if old is None:
            return f"| {label} | — | {new} |"
        return f"| {label} | {old} | {new} |"

    rows = [
        ("city of LA, venues", "la_city", "venues"),
        ("city of LA, venues with pickleball", "la_city", "venues_with_pickleball"),
        ("city of LA, pickleball courts", "la_city", "pickleball_courts"),
        ("LA county, venues", "la_county", "venues"),
        ("LA county, venues with pickleball", "la_county", "venues_with_pickleball"),
        ("LA county, pickleball courts", "la_county", "pickleball_courts"),
    ]
    print("### Los Angeles, before and after\n")
    print("| area | before | after |")
    print("| --- | --- | --- |")
    for label, area, key in rows:
        print(line(label, area, key))
    print()
    for entry in report["watched"]["after"]:
        names = ", ".join(
            f"{row['n'] or row['id']} ({', '.join(f'{k} {v}' for k, v in row['s'].items()) or 'no sports'})"
            for row in entry["venues"]
        )
        print(f"- **{entry['label']}**: {names or 'no venue within range'}")
    print()


if __name__ == "__main__":
    main()

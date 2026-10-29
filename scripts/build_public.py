#!/usr/bin/env python3
"""Build one state's venues, keeping only places the public can actually play.

This is the public-only builder: it reuses build_state.py's OSM reading and
grouping, decides each court with public_filter.py, and writes the same NDJSON
the app already reads plus the new "v" field:

  v: "park"    inside public-access PAD-US land (federal/state/local/regional)
     "school"  on a public high-school campus (OSM school area + NCES grades 9-12)
     "known"   named public rec/sports centre, beach court, or access=yes court

A park is judged as a whole, once, and every court inside it inherits that
verdict; a school's courts are grouped into one venue named after the school, the
same way a park's courts are grouped under the park, so a campus is one pin
rather than eight identical "Basketball courts" pins.

Usage:
  python3 build_public.py filtered.osm.pbf out/california.ndjson.gz \
      --padus-geojson padus/california.geojson \
      --schools nces/ccd_sch.zip --school-locations nces/edge_public.csv \
      --baseline --report out/california.report.json \
      --dropped out/california.dropped.jsonl --sample out/california.samples.txt
"""
import argparse
import gzip
import json
import os
import random
import sys
from collections import Counter, defaultdict

import build_state
import public_filter


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="filtered .osm.pbf")
    parser.add_argument("dest", help="output .ndjson.gz")
    parser.add_argument("--padus-geojson", required=True,
                        help="public PAD-US polygons from fetch_padus.py")
    parser.add_argument("--schools", help="NCES CCD school directory zip (grades)")
    parser.add_argument("--school-locations", help="NCES EDGE public school locations CSV")
    parser.add_argument("--baseline", action="store_true",
                        help="also build the old way, to report before vs after")
    parser.add_argument("--report", help="write counts here (JSON)")
    parser.add_argument("--dropped", help="write every dropped court here (JSONL)")
    parser.add_argument("--sample", help="write 10 kept + 10 dropped examples here")
    parser.add_argument("--sample-bbox", help="minx,miny,maxx,maxy to sample from, e.g. Los Angeles")
    parser.add_argument("--seed", type=int, default=7)
    return parser.parse_args(argv)


def items_of(areas):
    """OSM area dicts -> indexable items with a shapely geometry."""
    out = []
    for area in areas:
        geom = public_filter.polygons_of_rings(area["rings"])
        if geom is not None:
            out.append({"id": area["id"], "name": area["name"] or "", "geom": geom,
                        "centroid": area["centroid"]})
    return out


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if public_filter.Point is None:
        sys.exit("shapely is required: pip install shapely")

    padus = public_filter.build_index(public_filter.load_padus_geojson(args.padus_geojson))
    schools = (public_filter.load_schools(args.schools, args.school_locations)
               if args.schools and args.school_locations else [])

    # keep_private: a public school's courts are usually tagged access=private in
    # OSM, so dropping them at read time made the school rule unreachable.
    collector = build_state.read(args.source, keep_private=True)

    school_area_items = items_of([area for area in collector.restricted
                                  if (area.get("amenity") or "") == "school"])
    campuses = public_filter.resolve_campuses(school_area_items, schools)
    campuses_by_id = {campus["id"]: campus for campus in campuses}
    school_area_index = public_filter.build_index(school_area_items)
    park_items = items_of(collector.parks)

    # Parks and rec centres are judged as a whole, once, instead of court by
    # court: public land covering at least half of the area makes every court
    # inside it public ground. Testing each court against PAD-US on its own lost
    # real courts, because a park boundary and its PAD-US polygon never line up
    # exactly — Palms Park lost its basketball court and Sepulveda Basin 28 of
    # 36 baseball fields that way.
    public_areas = []
    for item in park_items:
        if public_filter.public_coverage(item["geom"], padus) >= public_filter.PARK_COVERAGE:
            public_areas.append({**item, "verdict": "park"})
    for item in items_of(collector.centres):
        if public_filter.is_public_rec_name(item["name"]):
            public_areas.append({**item, "verdict": "known"})
    public_area_index = public_filter.build_index(public_areas)
    park_index = public_filter.build_index(park_items)

    # School areas that are NOT proven public (private schools, elementary and
    # middle schools, anything NCES has no 9-12 record for). Courts inside them
    # must all be dropped, so their kept/dropped totals are reported: that is
    # what proves a private school like Loyola High School stays off the map.
    unproven_index = public_filter.build_index(
        [area for area in school_area_items if area["id"] not in campuses_by_id]
    )
    unproven = defaultdict(lambda: {"kept": 0, "dropped": 0, "name": ""})

    settled = {
        "padus": padus,
        "campuses_by_id": campuses_by_id,
        "school_areas": school_area_index,
        "osm_parks": park_index,
        "public_areas": public_area_index,
    }
    before = len(collector.pitches)
    kept, dropped = [], []
    school_members = defaultdict(list)
    school_details = {}

    for pitch in collector.pitches:
        point = (pitch["lon"], pitch["lat"])
        park = public_filter.inside(park_index, point)
        verdict, reason, campus = public_filter.classify(point, {
            **settled,
            "name": (park or {}).get("name") or pitch["name"],
            "sports": pitch["sports"],
            "access": pitch["access"],
            "leisure": pitch.get("leisure") or "",
        })

        # Count against every unproven school site the court sits in, so a
        # campus nested inside a district parcel is still reported.
        for area in public_filter.inside_all(unproven_index, point):
            entry = unproven[area["id"]]
            entry["name"] = area["name"]
            entry["kept" if verdict else "dropped"] += 1

        if verdict is None:
            dropped.append({
                "id": pitch["id"], "name": pitch["name"] or (park or {}).get("name"),
                "lat": round(pitch["lat"], 6), "lon": round(pitch["lon"], 6),
                "sports": sorted(pitch["sports"]), "access": pitch["access"],
                "leisure": pitch.get("leisure") or "", "reason": reason,
            })
            continue
        pitch["v"] = verdict
        if verdict == "school":
            school_members[campus["id"]].append(pitch)
            school_details[campus["id"]] = campus
        else:
            kept.append(pitch)

    collector.pitches = kept
    venues = build_state.group(collector, keep_restricted=True)

    # A grouped venue is named after its park or first court; give it that
    # verdict so the app can label it.
    labels = {}
    for pitch in kept:
        labels.setdefault(pitch["id"], pitch["v"])
    for park in collector.parks:
        if park["id"] in labels:
            continue
        # A park is judged on its own ground; a school site inside it doesn't
        # change what the park is.
        verdict, _, _ = public_filter.classify(park["centroid"], {
            **settled, "school_areas": None, "name": park["name"], "sports": (),
            "access": "", "leisure": "park",
        })
        if verdict:
            labels[park["id"]] = verdict
    for venue in venues:
        venue["v"] = labels.get(venue["id"], "known")

    for campus_id, members in school_members.items():
        campus = school_details[campus_id]
        lon, lat = campus["centroid"]
        name = campus["name"] or campus.get("school") or "Public high school"
        school_venue = build_state.venue(campus_id, name, lon, lat, members, None)
        if school_venue:
            school_venue["v"] = "school"
            venues.append(school_venue)

    venues.sort(key=lambda venue: venue["id"])

    os.makedirs(os.path.dirname(args.dest) or ".", exist_ok=True)
    with gzip.open(args.dest, "wt", encoding="utf-8") as handle:
        for venue in venues:
            handle.write(json.dumps(venue, ensure_ascii=False, separators=(",", ":")) + "\n")

    by_verdict = Counter(venue["v"] for venue in venues)
    by_reason = Counter(item["reason"] for item in dropped)
    print(f"{args.source}: {before} courts/fields read -> {len(venues)} venues "
          f"({dict(sorted(by_verdict.items()))}), {len(dropped)} courts dropped")

    baseline_venues = build_state.group(build_state.read(args.source)) if args.baseline else []
    baseline = len(baseline_venues) if args.baseline else None
    if args.baseline:
        print(f"  baseline (old rules, same extract): {baseline} venues")

    # The old builder kept every court standing in a park. This one may only lose
    # courts to rules that are meant to fire (private access, club names, a park
    # nobody could verify), so a park that comes out smaller is counted and
    # reported: the CI check fails when more than 2% of kept parks lose courts.
    old_courts = {venue["id"]: sum(venue["s"].values()) for venue in baseline_venues}
    park_venues = [venue for venue in venues if venue["v"] == "park"]
    regressed = []
    for venue in park_venues:
        was = old_courts.get(venue["id"])
        if was is None:
            continue
        now = sum(venue["s"].values())
        if now < was:
            regressed.append({"id": venue["id"], "name": venue["n"],
                              "old": was, "new": now, "lost": was - now})
    regressed.sort(key=lambda item: (-item["lost"], item["id"]))
    rate = len(regressed) / len(park_venues) if park_venues else 0.0
    park_courts = {
        "parks": len(park_venues),
        "regressed": len(regressed),
        "rate": round(rate, 4),
        "examples": regressed[:10],
    }
    if park_venues:
        print(f"  parks: {len(park_venues)}, losing courts vs the old builder: "
              f"{len(regressed)} ({rate:.2%})")

    if args.dropped:
        os.makedirs(os.path.dirname(args.dropped) or ".", exist_ok=True)
        with open(args.dropped, "w", encoding="utf-8") as handle:
            for item in dropped:
                handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")

    if args.report:
        os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump({
                "state": os.path.basename(args.dest).split(".")[0],
                "baseline_venues": baseline,
                "courts_before": before,
                "venues_after": len(venues),
                "by_verdict": dict(sorted(by_verdict.items())),
                "school_campuses": len(school_members),
                "unproven_school_areas": [
                    {"id": area_id, "name": entry["name"],
                     "kept": entry["kept"], "dropped": entry["dropped"]}
                    for area_id, entry in sorted(unproven.items())
                ],
                "padus_polygons": len(padus[1]) if padus else 0,
                "park_courts": park_courts,
                "schools_9_12": len(schools),
                "dropped": len(dropped),
                "dropped_by_reason": dict(by_reason.most_common()),
            }, handle, indent=2, sort_keys=True)

    if args.sample:
        random.seed(args.seed)
        box = None
        if args.sample_bbox:
            box = tuple(float(value) for value in args.sample_bbox.split(","))
        inside_box = (lambda lat, lon: box[0] <= lon <= box[2] and box[1] <= lat <= box[3])
        kept_pool = [v for v in venues if not box or inside_box(v["la"], v["lo"])]
        drop_pool = [d for d in dropped if not box or inside_box(d["lat"], d["lon"])]
        lines = [f"# 10 random KEPT ({len(kept_pool)} candidates)"]
        for venue in random.sample(kept_pool, min(10, len(kept_pool))):
            lines.append(f"{venue['n']}\t{venue['la']},{venue['lo']}\t{venue['v']}\t{venue['id']}")
        lines += ["", f"# 10 random DROPPED ({len(drop_pool)} candidates)"]
        for item in random.sample(drop_pool, min(10, len(drop_pool))):
            lines.append(f"{item['name'] or '(unnamed)'}\t{item['lat']},{item['lon']}\t"
                         f"{','.join(item['sports'])}\t{item['reason']}")
        text = "\n".join(lines) + "\n"
        os.makedirs(os.path.dirname(args.sample) or ".", exist_ok=True)
        with open(args.sample, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(text)


if __name__ == "__main__":
    main()

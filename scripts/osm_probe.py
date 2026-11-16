#!/usr/bin/env python3
"""Dump the OSM objects around the parks the LA audit named.

The audit's open questions — why Debs came out as baseball, why Griffith Park,
Vermont Square and Exposition Park have no venue, what the Hansen Dam sports
complex is tagged as — can only be answered from the OSM data itself. Overpass
is unreachable from some networks (and rate-limited from others), so this reads
the extract the build already downloaded: `filtered.osm.pbf`, after the
tags-filter step, is a few megabytes instead of a gigabyte.

  python scripts/osm_probe.py --pbf filtered.osm.pbf --out out/california.osm-probe.json

Everything it prints is evidence for a human: ids, tags, centroids, and whether
an object is a node, a way or a relation.
"""
import argparse
import json
import math
import os

import osmium

# (label, lat0, lon0, lat1, lon1) — generous enough to hold the whole park.
BOXES = [
    ("debs", 34.080, -118.215, 34.112, -118.175),
    ("griffith", 34.115, -118.330, 34.165, -118.270),
    ("vermont_square", 33.9930, -118.3050, 34.0070, -118.2900),
    ("exposition", 34.0110, -118.3090, 34.0260, -118.2920),
    ("hansen_dam", 34.2550, -118.4150, 34.2900, -118.3750),
]

# Only these tags are printed: the full tag list of a school polygon is noise.
KEEP = ("leisure", "amenity", "sport", "name", "access", "lit", "surface",
        "operator", "landuse", "building", "ref", "description")


def centroid(points):
    """Area centroid of a closed ring, in (lon, lat)."""
    if len(points) < 3:
        return (sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points))
    area = cx = cy = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-14:
        return (sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points))
    area *= 0.5
    return (cx / (6 * area), cy / (6 * area))


def area_m2(points):
    """Planar ring area in square metres (longitude scaled by the latitude)."""
    if len(points) < 3:
        return 0.0
    lat = sum(p[1] for p in points) / len(points)
    total = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0 * 111_320.0 * 111_320.0 * math.cos(math.radians(lat))


def box_of(lat, lon):
    for label, lat0, lon0, lat1, lon1 in BOXES:
        if lat0 <= lat <= lat1 and lon0 <= lon <= lon1:
            return label
    return None


def wanted(tags):
    return any(key in tags for key in ("leisure", "amenity"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", required=True, help="the filtered OSM extract")
    parser.add_argument("--out", required=True, help="where to write the JSON")
    args = parser.parse_args(argv)

    found = {label: [] for label, *_ in BOXES}
    for obj in osmium.FileProcessor(args.pbf).with_locations():
        tags = {tag.k: tag.v for tag in obj.tags}
        if not wanted(tags):
            continue

        points = []
        if isinstance(obj, osmium.osm.Node):
            if not obj.location.valid():
                continue
            points = [(obj.location.lon, obj.location.lat)]
            kind = "node"
        else:
            try:
                points = [(node.lon, node.lat) for node in obj.nodes if node.location.valid()]
            except Exception:  # noqa: BLE001 - a broken geometry is not worth crashing on
                continue
            if not points:
                continue
            kind = "way" if isinstance(obj, osmium.osm.Way) else "relation"

        lon, lat = centroid(points)
        label = box_of(lat, lon)
        if not label:
            continue

        entry = {
            "id": f"{kind}/{obj.id}",
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "points": len(points),
            "tags": {key: tags[key] for key in KEEP if key in tags},
        }
        if kind != "node":
            entry["area_m2"] = round(area_m2(points))
        found[label].append(entry)

    for entries in found.values():
        entries.sort(key=lambda row: (row["tags"].get("leisure", ""),
                                      row["tags"].get("amenity", ""),
                                      -row.get("area_m2", 0)))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(found, handle, indent=2, sort_keys=True)

    for label, entries in found.items():
        print(f"\n=== {label}: {len(entries)} objects")
        for row in entries:
            tags = " ".join(f"{key}={value}" for key, value in row["tags"].items())
            size = f" {row['area_m2']} m²" if "area_m2" in row else ""
            print(f"  {row['id']:<16} {row['lat']:.5f},{row['lon']:.5f}{size} {tags}")


if __name__ == "__main__":
    main()

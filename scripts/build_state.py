#!/usr/bin/env python3
"""Turn one state's OpenStreetMap extract into a list of places to play.

Input:  an .osm.pbf (or .osm) file, already filtered down to leisure=* objects
Output: a gzipped NDJSON file, one venue per line, in the compact format
        described in README.md

Courts and fields that sit inside a park are grouped under that park's name,
with a count per sport ("Tennis - 4 courts"). Courts that are not in a park are
kept on their own; unnamed neighbouring courts are merged into one venue.

Usage: python3 build_state.py <input.osm.pbf> <output.ndjson.gz>
"""
import gzip
import json
import math
import os
import sys
from collections import Counter, defaultdict

import osmium

# OpenStreetMap "sport" values -> the keys the PickUp app uses.
ALIASES = {
    "ultimate": "ultimate_frisbee",
    "frisbee": "ultimate_frisbee",
    "disc_golf": "disc_golf",
    "football": "american_football",  # in the US, "football" on a pitch is gridiron
    "american_football": "american_football",
    "beachvolleyball": "beach_volleyball",
    "beach_volleyball": "beach_volleyball",
    "paddle_tennis": "paddle_tennis",
    "padel": "padel",
    "athletics": "running",
    "running": "running",
    "fitness": "fitness",
    "skateboard": "skateboard",
    "skating": "skateboard",
}
SPORTS = {
    "basketball", "soccer", "tennis", "pickleball", "volleyball", "beach_volleyball",
    "baseball", "softball", "american_football", "ultimate_frisbee", "running",
    "fitness", "skateboard", "handball", "table_tennis", "bocce", "cricket",
    "rugby_union", "rugby_league", "lacrosse", "field_hockey", "hockey",
    "ice_hockey", "disc_golf", "padel", "paddle_tennis", "racquetball", "squash",
    "badminton", "netball", "golf", "multi",
}
# Running only counts at a real track (leisure=track). Gym / lifting style
# sports never put a pin on the map by themselves: players can still host a
# game for them, but a park with an outdoor gym isn't a "place to play".
MAP_HIDDEN_SPORTS = {"fitness"}
RUNNING_TRACK_SPORTS = {"", "running", "athletics", "jogging", "multi"}

PARK_CELL = 0.01      # spatial index cell for parks, in degrees (~1 km)
MERGE_SNAP = 0.0008   # unnamed courts closer than ~80 m become one venue


def sports_of(tags):
    leisure = tags.get("leisure", "")
    raw = tags.get("sport", "")
    parts = [p.strip().lower().replace(" ", "_") for p in raw.replace(",", ";").split(";")]
    parts = [p for p in parts if p]
    found = []
    for part in parts:
        key = ALIASES.get(part, part)
        if key not in SPORTS or key in MAP_HIDDEN_SPORTS:
            continue
        if key == "running" and leisure != "track":
            continue  # a "running" tag on a field or park isn't a track
        if key not in found:
            found.append(key)
    # An untagged (or plainly running) track is a running track. Horse and
    # motor racing tracks are not.
    if leisure == "track" and "running" not in found:
        if not parts or all(p in RUNNING_TRACK_SPORTS for p in parts):
            found.append("running")
    return found


def address_of(tags):
    street = tags.get("addr:street")
    if not street:
        return None
    number = tags.get("addr:housenumber")
    line = f"{number} {street}" if number else street
    city = tags.get("addr:city")
    return f"{line}, {city}" if city else line


def lit_of(tags):
    value = tags.get("lit", "").lower()
    if value in ("yes", "24/7", "automatic", "limited"):
        return True
    if value == "no":
        return False
    return None


def point_in_ring(lon, lat, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def ring_centroid(ring):
    """Area-weighted centroid of a ring; falls back to the vertex mean."""
    area = cx = cy = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-14:
        return (sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n)
    area *= 0.5
    return (cx / (6 * area), cy / (6 * area))


# Courts here are almost never open to the public, so they are dropped
# unless the court itself is tagged access=yes/permissive/public.
RESTRICTED_AMENITY = {
    "school", "college", "university", "kindergarten", "prison",
    "place_of_worship",
}
RESTRICTED_LEISURE = {"golf_course"}
CLOSED_ACCESS = {"private", "no", "members", "customers", "permit", "students"}
OPEN_ACCESS = {"yes", "permissive", "public", "designated"}


def access_of(tags):
    return tags.get("access", "").strip().lower()


def is_restricted_area(tags):
    if tags.get("amenity") in RESTRICTED_AMENITY:
        return True
    if tags.get("leisure") in RESTRICTED_LEISURE:
        return True
    if "club" in tags and tags.get("leisure") != "park":
        return True  # country clubs, tennis clubs, etc.
    return False


def polygon(kind, osm_id, tags, rings):
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    biggest = max(rings, key=len)
    return {
        "id": f"{kind}/{osm_id}",
        "name": tags.get("name"),
        "rings": rings,
        "bbox": (min(xs), min(ys), max(xs), max(ys)),
        "centroid": ring_centroid(biggest),
        "address": address_of(tags),
        # Kept for the public-only builder, which has to tell a school area from
        # a club or a golf course. Not written to the published files.
        "amenity": tags.get("amenity"),
        "leisure": tags.get("leisure"),
    }


class Collector:
    def __init__(self, keep_private=False):
        self.keep_private = keep_private
        self.parks = []       # public parks: courts inside are grouped under them
        self.restricted = []  # schools, clubs, private parks: courts inside are dropped
        self.pitches = []     # dict(id, name, lon, lat, sports, surface, lit, address, access)
        self.dropped_private = 0

    def add(self, kind, osm_id, tags, rings=None, point=None):
        leisure = tags.get("leisure")
        access = access_of(tags)
        if rings and (is_restricted_area(tags) or (leisure == "park" and access in CLOSED_ACCESS)):
            self.restricted.append(polygon(kind, osm_id, tags, rings))
            return
        if leisure == "park":
            if rings:
                self.parks.append(polygon(kind, osm_id, tags, rings))
            return
        if leisure not in ("pitch", "sports_centre", "track", "fitness_station"):
            return
        sports = sports_of(tags)
        if not sports:
            return
        if access in CLOSED_ACCESS and not self.keep_private:
            self.dropped_private += 1
            return
        # Indoor gyms (sport=fitness at a sports centre) aren't pickup spots.
        if leisure == "sports_centre" and set(sports) <= {"fitness"}:
            return
        if point is None:
            point = ring_centroid(max(rings, key=len))
        self.pitches.append({
            "id": f"{kind}/{osm_id}",
            "name": tags.get("name"),
            "lon": point[0],
            "lat": point[1],
            "sports": sports,
            "surface": tags.get("surface"),
            "lit": lit_of(tags),
            "address": address_of(tags),
            "access": access,
            "leisure": leisure,
        })


def read(path, keep_private=False):
    """Read the extract into a Collector.

    keep_private=True keeps pitches tagged access=private/no, which the map
    normally drops. The public-only builder needs them: a public high school's
    courts are usually tagged private in OSM, and the school area plus the NCES
    record is what proves they count.
    """
    col = Collector(keep_private=keep_private)
    fp = osmium.FileProcessor(path).with_locations().with_areas()
    for obj in fp:
        tags = {t.k: t.v for t in obj.tags}
        if "leisure" not in tags and tags.get("amenity") not in RESTRICTED_AMENITY:
            continue
        if isinstance(obj, osmium.osm.Node):
            if obj.location.valid():
                col.add("node", obj.id, tags, point=(obj.location.lon, obj.location.lat))
        elif isinstance(obj, osmium.osm.Way):
            # Closed ways also arrive as areas below; only keep open ways here.
            if obj.is_closed():
                continue
            pts = [(n.lon, n.lat) for n in obj.nodes if n.location.valid()]
            if pts:
                mid = pts[len(pts) // 2]
                col.add("way", obj.id, tags, point=mid)
        elif isinstance(obj, osmium.osm.Area):
            rings = []
            for outer in obj.outer_rings():
                ring = [(n.lon, n.lat) for n in outer if n.location.valid()]
                if len(ring) >= 3:
                    rings.append(ring)
            if rings:
                kind = "way" if obj.from_way() else "relation"
                col.add(kind, obj.orig_id(), tags, rings=rings)
    return col


def park_for(pitch, index):
    cx = math.floor(pitch["lon"] / PARK_CELL)
    cy = math.floor(pitch["lat"] / PARK_CELL)
    best = None
    for park in index.get((cx, cy), ()):
        x0, y0, x1, y1 = park["bbox"]
        if not (x0 <= pitch["lon"] <= x1 and y0 <= pitch["lat"] <= y1):
            continue
        if any(point_in_ring(pitch["lon"], pitch["lat"], r) for r in park["rings"]):
            # Prefer the smallest containing park (a named field inside a big regional park).
            size = (x1 - x0) * (y1 - y0)
            if best is None or size < best[0]:
                best = (size, park)
    return best[1] if best else None


def summarise(members):
    counts = Counter()
    surfaces = Counter()
    lit_values = []
    for m in members:
        for s in m["sports"]:
            counts[s] += 1
        if m["surface"]:
            surfaces[m["surface"]] += 1
        lit_values.append(m["lit"])
    if any(v is True for v in lit_values):
        lit = True
    elif lit_values and all(v is False for v in lit_values):
        lit = False
    else:
        lit = None
    surface = surfaces.most_common(1)[0][0] if surfaces else None
    ordered = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return ordered, surface, lit


def label_for(counts):
    top = next(iter(counts))
    return top.replace("_", " ").title() + (" courts" if top in COURT_SPORTS else " field")


COURT_SPORTS = {
    "basketball", "tennis", "pickleball", "volleyball", "beach_volleyball", "handball",
    "racquetball", "squash", "badminton", "padel", "paddle_tennis", "netball",
    "table_tennis", "bocce", "multi",
}


def venue(vid, name, lon, lat, members, address):
    counts, surface, lit = summarise(members)
    if not counts:
        return None
    out = {
        "id": vid,
        "n": (name or label_for(counts))[:120],
        "la": round(lat, 6),
        "lo": round(lon, 6),
        "s": counts,
    }
    if surface:
        out["sf"] = surface[:40]
    if lit is not None:
        out["lt"] = lit
    if address:
        out["a"] = address[:160]
    return out


def cluster(points):
    """Group points that are within MERGE_SNAP of each other (chains included)."""
    parent = list(range(len(points)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    grid = defaultdict(list)
    for i, p in enumerate(points):
        grid[(math.floor(p["lon"] / MERGE_SNAP), math.floor(p["lat"] / MERGE_SNAP))].append(i)
    lim = MERGE_SNAP * MERGE_SNAP
    for (gx, gy), members in grid.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    for i in members:
                        if i < j:
                            a, b = points[i], points[j]
                            dlon = (a["lon"] - b["lon"]) * math.cos(math.radians(a["lat"]))
                            if dlon * dlon + (a["lat"] - b["lat"]) ** 2 <= lim:
                                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i, p in enumerate(points):
        groups[find(i)].append(p)
    return list(groups.values())


def build_index(polys):
    index = defaultdict(list)
    for poly in polys:
        x0, y0, x1, y1 = poly["bbox"]
        for cx in range(math.floor(x0 / PARK_CELL), math.floor(x1 / PARK_CELL) + 1):
            for cy in range(math.floor(y0 / PARK_CELL), math.floor(y1 / PARK_CELL) + 1):
                index[(cx, cy)].append(poly)
    return index


def group(col, keep_restricted=False):
    """Group pitches into venues.

    keep_restricted=True keeps courts that sit inside a school/club area even
    when the court is tagged private. The public-only builder decides those
    cases itself (a public school campus is a legitimate place to play).
    """
    index = build_index(col.parks)
    restricted_index = build_index(col.restricted)

    in_park = defaultdict(list)
    loose_named = []
    unnamed = []
    for p in col.pitches:
        park = park_for(p, index)
        if (not keep_restricted and park is None
                and p["access"] not in OPEN_ACCESS and park_for(p, restricted_index)):
            col.dropped_private += 1
            continue
        if park is not None:
            in_park[park["id"]].append(p)
        elif p["name"]:
            loose_named.append(p)
        else:
            unnamed.append(p)
    loose_unnamed = cluster(unnamed)

    parks_by_id = {p["id"]: p for p in col.parks}
    venues = []
    for pid, members in in_park.items():
        park = parks_by_id[pid]
        lon, lat = park["centroid"]
        name = park["name"] or next((m["name"] for m in members if m["name"]), None)
        address = park["address"] or next((m["address"] for m in members if m["address"]), None)
        v = venue(pid, name, lon, lat, members, address)
        if v:
            venues.append(v)
    for p in loose_named:
        v = venue(p["id"], p["name"], p["lon"], p["lat"], [p], p["address"])
        if v:
            venues.append(v)
    for members in loose_unnamed:
        lon = sum(m["lon"] for m in members) / len(members)
        lat = sum(m["lat"] for m in members) / len(members)
        first = min(members, key=lambda m: m["id"])
        address = next((m["address"] for m in members if m["address"]), None)
        v = venue(first["id"], None, lon, lat, members, address)
        if v:
            venues.append(v)
    venues.sort(key=lambda v: v["id"])
    return venues


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    col = read(src)
    venues = group(col)
    with gzip.open(dst, "wt", encoding="utf-8") as f:
        for v in venues:
            f.write(json.dumps(v, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{src}: {len(col.parks)} parks, {len(col.pitches)} courts/fields, "
          f"{col.dropped_private} private skipped -> {len(venues)} venues")


if __name__ == "__main__":
    main()

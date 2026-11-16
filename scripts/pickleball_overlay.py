#!/usr/bin/env python3
"""Merge a maintained list of city-run pickleball sites into a state's venues.

Pickleball is the one sport where OpenStreetMap is a bad source. A hybrid court
is a tennis court with painted lines, and mappers almost never add
`sport=pickleball` to it, so every LA park the city lists as pickleball came out
of the build with none. Patch ing them by hand in the app (curated venues) does
not scale past one city.

So each state can carry a data file of documented sites — facility name,
coordinates, court count, hybrid or dedicated, lit or not, and the source it was
read from — and this module merges it after the OSM pass:

  * a documented site within MATCH_RADIUS_M of an existing venue adds
    `pickleball` to that venue (no second pin for the same park);
  * a documented site with no venue nearby becomes its own venue, id
    `pickleball/<slug>`, so a city-run site on ground nothing else maps still
    appears;
  * **OSM wins when it has an explicit pickleball pitch**: a venue that already
    carries a pickleball count is left alone, because someone mapped the court
    instead of the city listing it.

The overlay is data, not code: `data/pickleball/<state>.json`. Adding a city is
adding entries, and the file records where every count came from.
"""
import json
import math
import os
import re

# How far a documented site may sit from a venue's pin and still be that venue.
# The pins are park centroids, not courts, so a list coordinate for the courts
# inside a big park can be a few hundred metres away; 600 m still keeps two
# separate sites apart in a dense grid of small parks.
MATCH_RADIUS_M = 600

# File format version, so a change here can be seen in the data files.
FORMAT = 1


def metres_between(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres (haversine, spherical earth)."""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def slug(value):
    """A stable id fragment from a facility name."""
    words = re.split(r"[^a-z0-9]+", str(value or "").lower())
    return "-".join(word for word in words if word)[:80] or "pickleball-site"


def load(path):
    """Read a pickleball data file -> (entries, source note), or ([], None)."""
    if not path or not os.path.exists(path):
        return [], None
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    entries = data.get("sites") or []
    cleaned = []
    for entry in entries:
        try:
            lat = float(entry["lat"])
            lon = float(entry["lon"])
            courts = int(entry.get("courts") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if not courts or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        cleaned.append({
            "name": str(entry.get("name") or "").strip(),
            "address": str(entry.get("address") or "").strip() or None,
            "lat": lat,
            "lon": lon,
            "courts": courts,
            # "hybrid" courts are tennis courts with pickleball lines; the app
            # does not distinguish them yet, but the file keeps the fact.
            "kind": str(entry.get("kind") or "hybrid").strip().lower(),
            "lit": entry.get("lit"),
            "source": str(entry.get("source") or data.get("source") or "").strip() or None,
        })
    return cleaned, (data.get("source") or None)


def apply(venues, entries, radius_m=MATCH_RADIUS_M):
    """Merge documented pickleball sites into `venues` -> (venues, stats).

    `venues` are the dicts written to the state file; they are copied, so the
    caller's list is untouched. Every entry is reported as one of:

      "osm"      the venue already had an explicit OSM pickleball count, kept
      "merged"   pickleball was added to an existing venue
      "added"    a new venue for a documented site nothing else mapped
      "skipped"  no site matched and the entry could not stand on its own
    """
    stats = {"osm": 0, "merged": 0, "added": 0, "skipped": 0, "osm_wins": [],
             "merged_into": [], "added_names": []}
    if not entries:
        return venues, stats

    out = []
    for venue in venues:
        copy = dict(venue)
        copy["s"] = dict(venue.get("s") or {})
        out.append(copy)

    matched = set()
    for entry in entries:
        best = None
        for index, venue in enumerate(out):
            if index in matched:
                continue
            distance = metres_between(entry["lat"], entry["lon"],
                                      venue.get("la", 0.0), venue.get("lo", 0.0))
            if distance > radius_m:
                continue
            # Prefer a venue that already lists a racquet sport: a hybrid site
            # belongs to the tennis courts, not to the basketball court next to
            # them in the same park.
            racquet = bool(set(venue.get("s") or {}) & {"tennis", "paddle_tennis", "padel"})
            key = (0 if racquet else 1, distance)
            if best is None or key < best[0]:
                best = (key, index, venue)
        if best is None:
            slug_id = f"pickleball/{slug(entry['name'])}"
            venue = {
                "id": slug_id,
                "n": (entry["name"] or "Pickleball courts")[:120],
                "la": round(entry["lat"], 6),
                "lo": round(entry["lon"], 6),
                "s": {"pickleball": entry["courts"]},
                # A city documenting its own courts is the same proof a named
                # rec centre gets, so the app can label it honestly.
                "v": "known",
            }
            if entry["lit"] is True:
                venue["lt"] = True
            elif entry["lit"] is False:
                venue["lt"] = False
            if entry["address"]:
                venue["a"] = entry["address"][:160]
            out.append(venue)
            matched.add(len(out) - 1)
            stats["added"] += 1
            stats["added_names"].append(venue["n"])
            continue

        _, index, venue = best
        matched.add(index)
        if (venue.get("s") or {}).get("pickleball"):
            # OSM mapped the courts; a list is never better than a mapped court.
            stats["osm"] += 1
            stats["osm_wins"].append(venue.get("n"))
            continue
        venue.setdefault("s", {})["pickleball"] = entry["courts"]
        if entry["lit"] is True and "lt" not in venue:
            venue["lt"] = True
        if entry["kind"] == "dedicated":
            venue["sf"] = venue.get("sf") or "concrete"
        stats["merged"] += 1
        stats["merged_into"].append(venue.get("n"))

    out.sort(key=lambda venue: venue["id"])
    return out, stats


def describe(stats, source=None):
    """One line for the build log."""
    where = f" ({source})" if source else ""
    return (f"pickleball overlay{where}: {stats['merged']} merged into a venue, "
            f"{stats['added']} new site(s), {stats['osm']} left to OSM")

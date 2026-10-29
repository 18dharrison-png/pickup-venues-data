#!/usr/bin/env python3
"""Decide which courts and fields are really open to the public.

PickUp's map is a list of places a stranger can turn up and play, so a court is
kept only when a public source says the ground under it is public:

  "park"   inside a PAD-US protected-area polygon that is public-access land
           owned or managed by a federal, state, local or regional agency
  "school" inside an OpenStreetMap school area that also contains a *public*
           high school (NCES, grades 9-12), so the pitch is the school's
  "known"  a named public rec/sports centre, a public beach court, or a named
           court OpenStreetMap itself marks access=yes/public

Everything else goes: apartment and hotel courts, HOAs, clubs, private schools,
unnamed courts nobody has verified. Each venue carries the decision in its "v"
field so the app can label it.

Getting the school rule right took two fixes, both worth remembering:

  * the school point and the court are different objects, so the court can be
    200-400 m from the school's single NCES point on a big campus. Matching the
    court to a nearby point therefore failed for every real high school. The
    campus polygon is what connects them: an OSM school area is a public high
    school when an NCES 9-12 point falls inside it.
  * shapely's Point.contains(Point) is False even for identical points, so a
    containment test against point geometries silently never matched. Items are
    matched with covers(), which is true for a polygon covering a point and for
    two equal points.

PAD-US is public domain (USGS GAP); school places are public domain (NCES).
Both are credited in README.md.
"""
import csv
import io
import zipfile

try:  # shapely 2.x
    from shapely.geometry import MultiPolygon, Point, Polygon, shape
    from shapely.strtree import STRtree
except ImportError:  # pragma: no cover - reported by the caller
    Point = Polygon = MultiPolygon = shape = None
    STRtree = None

# PAD-US: public access codes are OA (open) and RA (restricted); XA (closed) and
# UK (unknown) are not public places. Owner/manager types are prefixed with the
# agency class, so a prefix test is robust to the exact domain list.
PUBLIC_ACCESS = {"OA", "RA"}
PUBLIC_OWNER_PREFIXES = ("FED", "STAT", "LOC", "REG", "DIST", "JNT")
NOT_PUBLIC_OWNER_PREFIXES = ("PVT", "NGO", "TRIB", "UNK")

# OSM access values that mean "anyone may play here", and the ones that don't.
OPEN_ACCESS = {"yes", "permissive", "public", "designated"}
CLOSED_ACCESS = {"private", "no", "members", "customers", "permit", "students"}

# A rec centre counts only when its name says it is one.
REC_NAME_WORDS = (
    "recreation", "rec center", "rec centre", "regional park", "sports complex",
    "sports centre", "sports center", "ballfield", "ball park", "ballpark",
    "athletic complex", "aquatic center", "community center", "community centre",
    "park district", "youth sports", "playfield",
)

# Named private/commercial operations that must never be labelled public.
PRIVATE_NAME_WORDS = (
    "country club", "tennis club", "golf club", "racquet club", "swim club",
    "athletic club", "fitness club", "sport club", "sports club", "ymca",
    "life time", "lifetime fitness", "equinox", "apartments", "apartment",
    "residences", "condominium", "hotel", "resort", "hoa", "homeowners",
    "rv park", "mobile home",
)

HIGH_SCHOOL_GRADES = {"09", "10", "11", "12"}

# How far a public high school's point may sit outside its OSM campus polygon
# and still prove the campus is a public school. NCES locates the building,
# OSM draws the boundary, and they disagree by a few tens of metres.
CAMPUS_TOLERANCE_M = 165


def shape_of(geojson):
    """A shapely geometry from a GeoJSON geometry dict, or None."""
    if not geojson or shape is None:
        return None
    try:
        geom = shape(geojson)
    except Exception:  # malformed geometry: treat as no geometry
        return None
    if geom.is_empty or not isinstance(geom, (Polygon, MultiPolygon)):
        return None
    return geom


def polygons_of_rings(rings):
    """One OSM area (maybe several outer rings) as a single shapely geometry."""
    parts = [Polygon(ring) for ring in rings if len(ring) >= 3]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return MultiPolygon(parts)


def normalise(value):
    return " ".join(str(value or "").strip().lower().split())


def is_public_padus(props):
    access = str(props.get("Pub_Access") or "").strip().upper()
    owner = str(props.get("Own_Type") or "").strip().upper()
    if access not in PUBLIC_ACCESS:
        return False
    if owner.startswith(NOT_PUBLIC_OWNER_PREFIXES):
        return False
    return owner.startswith(PUBLIC_OWNER_PREFIXES)


def load_padus_geojson(path):
    """Read the paged PAD-US extract -> [{"id", "name", "geom"}] of public land.

    fetch_padus.py has already dropped non-public units, but this re-checks
    every feature so a change to the fetcher can never quietly widen the rule.
    """
    import json

    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    items = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        if not is_public_padus(props):
            continue
        geom = shape_of(feature.get("geometry"))
        if geom is None:
            continue
        items.append({
            "id": f"padus/{props.get('OBJECTID')}",
            "name": str(props.get("Unit_Nm") or ""),
            "geom": geom,
        })
    return items


def load_schools(directory_path, locations_path):
    """Read NCES data -> public high-school points [(lon, lat, name)].

    Two files are needed and they are joined on NCESSCH:

      * the CCD school directory (ccd_sch_029_*.csv, inside its zip) has the
        grade span, the school type and the open/closed status but **no
        coordinates**;
      * the EDGE public-school locations file has NAME/LAT/LON.

    Using only the directory silently put every school at (0, 0), which is why
    an earlier build found no schools at all. Private schools are absent from
    both files, so "public" is structural; status and grades are checked here.
    """
    if not locations_path:
        return []

    places = {}
    with open(locations_path, encoding="utf-8-sig", errors="replace") as stream:
        for row in csv.DictReader(stream):
            try:
                lat = float(row.get("LAT") or "")
                lon = float(row.get("LON") or "")
            except ValueError:
                continue
            # The 50 states, DC, and the Atlantic/Caribbean territories. The
            # box also drops the (0, 0) that a missing fix produces. Pacific
            # territories are outside every state build, so they're excluded.
            if not (15 <= lat <= 72 and -180 <= lon <= -64):
                continue
            places[str(row.get("NCESSCH") or "").strip()] = (lon, lat, row.get("NAME") or "")

    archive = zipfile.ZipFile(directory_path)
    member = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
    points = []
    seen = set()
    with io.TextIOWrapper(archive.open(member), encoding="utf-8", errors="replace") as stream:
        for row in csv.DictReader(stream):
            if str(row.get("SY_STATUS", "1")).strip() not in ("", "1"):
                continue  # closed / future / inactive
            grades = {
                str(row.get("GSLO") or "").strip().upper().zfill(2),
                str(row.get("GSHI") or "").strip().upper().zfill(2),
            }
            if not (grades & HIGH_SCHOOL_GRADES):
                continue
            key = str(row.get("NCESSCH") or "").strip()
            place = places.get(key)
            if place is None or key in seen:
                continue
            seen.add(key)
            points.append((place[0], place[1], place[2] or str(row.get("SCH_NAME") or "")))
    return points


def build_index(items):
    """(STRtree, items) for fast containment lookups; items keep their order."""
    items = [item for item in items if item.get("geom") is not None]
    if not items:
        return None
    return (STRtree([item["geom"] for item in items]), items)


def inside(index, point):
    """The first indexed item whose geometry covers this point, or None."""
    if not index:
        return None
    tree, items = index
    probe = Point(point)
    for position in tree.query(probe):
        item = items[position]
        # covers(), not contains(): a point never contains an equal point.
        if item["geom"].covers(probe):
            return item
    return None


def resolve_campuses(areas, schools, tolerance_m=CAMPUS_TOLERANCE_M):
    """OSM school areas that provably hold a public 9-12 school.

    `areas` are OSM `amenity=school` polygons as {"id", "name", "geom"};
    `schools` are NCES points [(lon, lat, name)]. Only the areas that pass come
    back, so a private school (which NCES does not list at all) can never be
    labelled public.
    """
    if not areas or not schools:
        return []
    points = [Point(lon, lat) for lon, lat, _ in schools]
    tree = STRtree(points)
    tolerance = tolerance_m / 111_000.0
    campuses = []
    for area in areas:
        geom = area.get("geom")
        if geom is None:
            continue
        proof = None
        # A generous envelope keeps the candidate set small but never excludes a
        # school building just outside a campus edge.
        for position in tree.query(geom.buffer(tolerance)):
            point = points[position]
            if geom.covers(point) or geom.distance(point) <= tolerance:
                proof = schools[position]
                break
        if proof is None:
            continue
        campuses.append({**area, "school": proof[2]})
    return campuses


def classify(point, context):
    """Return (verdict, reason, campus) for one court/field location.

    `context` carries the prepared indexes (padus, campuses) plus the OSM facts
    about the venue itself (name, sports, access, leisure). `campus` is the
    public high school the court belongs to, so the builder can group a school's
    courts into one venue named after the school.
    """
    name = normalise(context.get("name"))
    sports = set(context.get("sports") or ())
    access = str(context.get("access") or "").strip().lower()
    leisure = context.get("leisure") or ""

    if name and any(word in name for word in PRIVATE_NAME_WORDS):
        return None, "named private or commercial", None

    park = inside(context.get("padus"), point)
    if park is not None:
        return "park", f"inside PAD-US public land: {park['name'] or 'unnamed unit'}", None

    campus = inside(context.get("campuses"), point)
    if campus is not None:
        return "school", f"inside {campus['name'] or 'a public high school'} (NCES grades 9-12)", campus

    if access in CLOSED_ACCESS:
        return None, f"tagged access={access} and not in a public park or school", None

    if leisure == "sports_centre" and name and any(word in name for word in REC_NAME_WORDS):
        return "known", "named public recreation or sports centre", None
    if "beach_volleyball" in sports:
        return "known", "beach volleyball court", None
    if name and access in OPEN_ACCESS:
        return "known", f"named court tagged access={access}", None

    return None, "no public source places it in a park, public school or rec centre", None


LABELS = {
    "park": "Public park",
    "school": "Public high school - check public hours",
    "known": "Rec center",
}

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

Getting the school rule right took three fixes, all worth remembering:

  * the school point and the court are different objects, so the court can be
    200-400 m from the school's single NCES point on a big campus. Matching the
    court to a nearby point therefore failed for every real high school. The
    campus polygon is what connects them: an OSM school area is a public high
    school when an NCES 9-12 point falls inside it.
  * shapely's Point.contains(Point) is False even for identical points, so a
    containment test against point geometries silently never matched. Items are
    matched with covers(), which is true for a polygon covering a point and for
    two equal points.
  * a loose tolerance reaches across a fence. At 165 m, Wildwood School (private,
    Mar Vista) borrowed a neighbouring high school's NCES record, and Elementary
    and Middle schools inherited high school records the same way. A campus now
    needs the NCES point inside *that* polygon (30 m for boundary disagreement,
    never a neighbouring campus) AND the two names to identify the same school.

A court inside an OSM school site is decided by the school rule before PAD-US,
because a public school's fields are public land too and were being labelled
"park". Inside a verified campus it is "school"; inside any other school site it
is dropped unless an OSM park covers it, which is a joint-use park.

Parks and rec centres are judged as a whole, once, instead of court by court. A
park boundary and its PAD-US polygon never line up exactly, so testing each
court on its own dropped real courts that sat a few metres outside the public
land while standing inside a public park: Palms Park lost its basketball court
and Sepulveda Basin 28 of 36 baseball fields that way. A park whose public
coverage reaches PARK_COVERAGE now keeps every court inside it, and the
court-by-court PAD-US test is left for loose courts that are inside no park,
school or rec centre.

Coverage is *sampled*, not intersected. PAD-US units are huge (a national
forest is one polygon with thousands of vertices) and thousands of parks query
the same ones, so repairing each unit per park, unioning the hits and
intersecting them took a California build from about seven minutes to over
forty-five. Now every PAD-US polygon is repaired exactly once when it is
loaded, one STRtree is built over the results, and a park's coverage is the
share of a fixed grid of points inside it that fall on public land: one bulk
tree query plus cheap point-in-polygon tests. A polygon that cannot be repaired
is dropped and counted in the report instead of crashing the build.

A park that PAD-US does not cover is not lost either: a name that says public
recreation area, or an operator/owner tag naming a city, county, park district
or recreation department, is proof enough to judge the park as a whole. Clubs
and private owners are refused in both cases.

PAD-US is public domain (USGS GAP); school places are public domain (NCES).
Both are credited in README.md.
"""
import csv
import io
import math
import re
import zipfile

try:  # shapely 2.x
    from shapely.geometry import MultiPolygon, Point, Polygon, shape
    from shapely import make_valid
    from shapely.ops import unary_union
    from shapely.prepared import prep
    from shapely.strtree import STRtree
except ImportError:  # pragma: no cover - reported by the caller
    Point = Polygon = MultiPolygon = shape = None
    STRtree = None
    unary_union = None

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

# A park whose operator/owner tags name a public agency is public ground even
# where PAD-US has no polygon for it. These are the words those tags use, and
# the tokens that mark a private operation instead.
PUBLIC_OWNERSHIP_VALUES = {"municipal", "county", "state", "public", "government",
                           "federal", "city", "town", "township", "parish"}
PUBLIC_AGENCY_PHRASES = (
    "city of", "city parks", "city park", "parks and recreation",
    "park and recreation", "parks & recreation", "parks & rec",
    "recreation and parks", "recreation and park", "recreation department",
    "parks department", "park district", "recreation district",
    "community services district", "community service district",
    "department of parks", "county of", "county parks", "county park",
    "town of", "village of", "municipality", "state parks",
    "department of natural resources", "school district",
)
# Whole words that name a public agency by themselves: "Los Angeles County".
# "city" is deliberately not one of them — a bare "city" is also how private
# developments name themselves ("Sun City"), while a city that runs a park
# writes "City of ...", which the phrase list above already covers.
PUBLIC_AGENCY_TOKENS = {"county", "parish", "borough", "township", "municipality"}
PRIVATE_OWNER_TOKENS = {"private", "club", "hoa", "homeowners", "llc", "inc",
                        "ltd", "corp", "company"}

HIGH_SCHOOL_GRADES = {"09", "10", "11", "12"}

# How far a public high school's point may sit outside its OSM campus polygon
# and still prove the campus is a public school. NCES locates the building and
# OSM draws the boundary, which disagree by a few metres — 165 m was wide enough
# to reach a *different* school and label a private campus public.
CAMPUS_TOLERANCE_M = 30

# A court is often drawn a few metres outside the park it belongs to: the fence,
# the road, a land parcel that stops short of the courts. Those courts used to be
# dropped outright, or kept as a "Tennis courts" pin standing beside the park's
# own pin. A pitch within this distance of a verified public park or public
# high-school campus now joins that place.
NEAR_PUBLIC_M = 150

# Golf is not a pickup court: a driving range or a golf course is a business
# with a tee sheet, not a place a stranger turns up to play. Excluded from the
# near-public rule and from having a pin of its own. Disc golf is a different
# sport and stays.
GOLF_SPORTS = {"golf"}

# Words that identify a school. Generic ones can't prove two records are the
# same place ("... High School" and "... Elementary" share only generic words),
# and neither can a school *type* — "Thomas Russell Middle School" and "Milpitas
# Middle College High" share only "middle".
GENERIC_NAME_WORDS = {
    "school", "schools", "high", "senior", "junior", "charter", "academy",
    "college", "campus", "center", "centre", "public", "prep", "preparatory",
    "international", "magnet", "learning", "education", "educational",
    "community", "technical", "technology", "leadership", "sciences", "arts",
    "middle", "elementary", "primary", "grades", "the", "and", "for", "of", "at",
}
# An OSM school named for a primary school is only believed when the NCES record
# it matched is itself a secondary school (a "Junior-Senior High" is fine).
PRIMARY_NAME_WORDS = ("elementary", "middle", "primary", "grade school", "k-8", "k8")
SECONDARY_NAME_WORDS = (
    "high", "secondary", "senior", "junior-senior", "junior senior",
    "jr/sr", "jr-sr", "jr sr", "7-12", "9-12",
)


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


def name_tokens(value):
    words = re.split(r"[^a-z0-9]+", normalise(value))
    return {word for word in words if len(word) >= 4 and word not in GENERIC_NAME_WORDS}


def names_agree(osm_name, nces_name):
    """True when both names carry a word that identifies the same school.

    An OSM area with no name can't be compared, so strict containment decides it.
    """
    if not normalise(osm_name):
        return True
    osm = name_tokens(osm_name)
    nces = name_tokens(nces_name)
    return bool(osm and nces and osm & nces)


def is_primary_only(osm_name, nces_name):
    """An OSM primary/middle school that is not the same school as the NCES record.

    A campus called "... Elementary" or "... Middle School" is not a public high
    school just because a high school's point falls inside it — Edgewood Middle
    School contains Edgewood High, and Thomas Russell Middle School contains
    Milpitas Middle College High. It only counts when the record is the *combined*
    school, carrying a primary and a secondary word ("Hercules Middle/High"),
    which is the one case where both names describe the same institution.
    """
    osm = normalise(osm_name)
    if not any(word in osm for word in PRIMARY_NAME_WORDS):
        return False
    if any(word in osm for word in SECONDARY_NAME_WORDS):
        return False  # the name already says secondary or combined
    nces = normalise(nces_name)
    return not (any(word in nces for word in PRIMARY_NAME_WORDS)
                and any(word in nces for word in SECONDARY_NAME_WORDS))


def is_public_padus(props):
    access = str(props.get("Pub_Access") or "").strip().upper()
    owner = str(props.get("Own_Type") or "").strip().upper()
    if access not in PUBLIC_ACCESS:
        return False
    if owner.startswith(NOT_PUBLIC_OWNER_PREFIXES):
        return False
    return owner.startswith(PUBLIC_OWNER_PREFIXES)


def load_padus_geojson(path, stats=None):
    """Read the paged PAD-US extract -> [{"id", "name", "geom"}] of public land.

    fetch_padus.py has already dropped non-public units, but this re-checks
    every feature so a change to the fetcher can never quietly widen the rule.

    Every polygon is repaired here, exactly once, so the park lookups never touch
    GEOS's validity machinery again. `stats`, when given, collects how many
    polygons needed repairing and how many had to be dropped: a broken ring is
    skipped and counted in the build report instead of failing the state.
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
            if stats is not None:
                stats["unrepaired"] = stats.get("unrepaired", 0) + 1
            continue
        fixed = repaired(geom)
        if fixed is None:
            # One bad polygon is dropped and counted: never fatal.
            if stats is not None:
                stats["unrepaired"] = stats.get("unrepaired", 0) + 1
            continue
        if fixed is not geom and stats is not None:
            stats["repaired"] = stats.get("repaired", 0) + 1
        items.append({
            "id": f"padus/{props.get('OBJECTID')}",
            "name": str(props.get("Unit_Nm") or ""),
            "geom": fixed,
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


def inside_all(index, point):
    """Every indexed item whose geometry covers this point, in index order."""
    if not index:
        return []
    tree, items = index
    probe = Point(point)
    return [items[position] for position in tree.query(probe)
            # covers(), not contains(): a point never contains an equal point.
            if items[position]["geom"].covers(probe)]


def inside(index, point):
    """The first indexed item whose geometry covers this point, or None."""
    found = inside_all(index, point)
    return found[0] if found else None


def nearby(index, point, limit_m=NEAR_PUBLIC_M):
    """The nearest indexed area within `limit_m` of `point`, or None.

    Distance is measured from the polygon, not its centroid, so a court beside a
    long thin park is near the park it is beside. A point inside an area counts
    as distance zero, so an inside hit always wins.

    One STRtree query over the point's own neighbourhood, then a real distance
    test only for the candidates it returns: the same cheap pattern the coverage
    sampling uses. The longitude tolerance is widened by 1/cos(lat) because a
    degree of longitude shrinks away from the equator; clamping the cosine keeps
    a degenerate point from producing an unbounded box.
    """
    if not index or Point is None:
        return None
    tree, items = index
    probe = Point(point)
    latitude = abs(probe.y)
    metres_per_degree = 111_000.0 * max(math.cos(math.radians(latitude)), 0.2)
    tolerance = limit_m / min(111_000.0, metres_per_degree)
    best = None
    try:
        candidates = tree.query(probe.buffer(tolerance))
    except Exception:
        candidates = range(len(items))
    for position in candidates:
        item = items[position]
        try:
            if item["geom"].covers(probe):
                return item
            distance = item["geom"].distance(probe)
        except Exception:  # a geometry GEOS still refuses: not this one
            continue
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, item)
    return best[1] if best else None


# A park is judged as a whole: public land under at least this share of the
# park's ground makes every court inside it public ground. Half is deliberately
# generous to boundary disagreement while still refusing a park that only clips
# a public parcel at one corner.
PARK_COVERAGE = 0.5

# Coverage is estimated from a PARK_SAMPLES x PARK_SAMPLES grid across the park's
# bounding box, keeping the points that fall inside the park. Fixed, so a park's
# verdict never changes between runs, and cheap enough to run for every park in
# a state: an exact intersection is what made California take an hour.
PARK_SAMPLES = 6


def polygon_only(geom):
    """The polygonal part of `geom`, or None.

    make_valid() returns a GeometryCollection for some broken rings (the repaired
    polygon plus the line where the ring crossed itself), and a line has no area
    to measure, so only the polygons come back.
    """
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    parts = [part for part in getattr(geom, "geoms", ())
             if isinstance(part, Polygon) and not part.is_empty]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def repaired(geom):
    """A valid polygonal version of `geom`, or None when it cannot be repaired.

    PAD-US ships self-intersecting rings, and GEOS refuses to reason about them
    ("TopologyException: side location conflict at ..."), which took down a
    whole state build. make_valid fixes almost all of them, buffer(0) is tried
    next, and anything still broken comes back as None so the caller can drop it
    and count it — one bad ring never crashes the build.

    This runs once per polygon when PAD-US is loaded, never per lookup: repairing
    the same ring again for every park in the state is what made a build take an
    hour instead of minutes.
    """
    if geom is None:
        return None
    if geom.is_valid and not geom.is_empty:
        return geom
    for fix in (make_valid, lambda broken: broken.buffer(0)):
        try:
            fixed = polygon_only(fix(geom))
        except Exception:
            continue
        if fixed is not None and fixed.is_valid and not fixed.is_empty:
            return fixed
    return None


def sample_ground(geom, cells=PARK_SAMPLES):
    """Fixed grid points spread across `geom`, keeping only the ones inside it.

    Points outside the park are dropped, so the survivors are an even sample of
    the park's ground even when it is L-shaped or long and thin. A park too thin
    to catch a grid point returns nothing; the caller falls back to its centre.
    """
    minx, miny, maxx, maxy = geom.bounds
    width, height = maxx - minx, maxy - miny
    if width <= 0 or height <= 0:
        return []
    prepared = prep(geom)
    points = []
    for column in range(cells):
        x = minx + width * (column + 0.5) / cells
        for row in range(cells):
            y = miny + height * (row + 0.5) / cells
            probe = Point(x, y)
            if prepared.covers(probe):
                points.append(probe)
    return points


# Which row of a bulk STRtree query holds the tree indices. Shapely releases
# disagree, so it is measured once with a probe tree (see tree_matches) rather
# than assumed, because guessing wrong silently reads the wrong geometry.
_QUERY_ORDER = None


def _probe_query_order():
    """"tree_first" or "input_first": the row order of a bulk STRtree query."""
    probe = STRtree([Polygon(((0, 0), (0, 1), (1, 1), (1, 0))),
                     Polygon(((5, 5), (5, 6), (6, 6), (6, 5))),
                     Polygon(((10, 10), (10, 11), (11, 11), (11, 10)))])
    hits = probe.query([Point(0.5, 0.5), Point(10.5, 10.5)])
    first, second = list(hits[0]), list(hits[1])
    # The two points sit in the first and third geometries, so only the tree row
    # can contain a 2; the input row holds 0 and 1.
    if first and max(first) > 1:
        return "tree_first"
    if second and max(second) > 1:
        return "input_first"
    return "tree_first"


def tree_matches(tree, points):
    """(tree indices, point indices) for every candidate pair, in one query."""
    global _QUERY_ORDER
    hits = tree.query(points)
    rows = (list(hits[0]), list(hits[1]))
    if _QUERY_ORDER is None:
        _QUERY_ORDER = _probe_query_order()
    if _QUERY_ORDER == "tree_first":
        return rows[0], rows[1]
    return rows[1], rows[0]


def covered_points(tree, items, points):
    """For each point, whether any indexed geometry covers it.

    One bulk STRtree query answers every point at once, then a point-in-polygon
    test is run only for the candidate pairs it returns. A geometry that GEOS
    still chokes on counts as "not covering" instead of failing the build.
    """
    covered = [False] * len(points)
    try:
        tree_ids, point_ids = tree_matches(tree, points)
    except Exception:  # older shapely: query one point at a time
        tree_ids, point_ids = [], []
        for position, probe in enumerate(points):
            for tree_id in tree.query(probe):
                tree_ids.append(tree_id)
                point_ids.append(position)
    for tree_id, point_id in zip(tree_ids, point_ids):
        tree_id, point_id = int(tree_id), int(point_id)
        if not (0 <= tree_id < len(items) and 0 <= point_id < len(points)):
            continue
        if covered[point_id]:
            continue
        try:
            if items[tree_id]["geom"].covers(points[point_id]):
                covered[point_id] = True
        except Exception:
            continue
    return covered


def public_coverage(geom, index):
    """Share of `geom` (0-1) sitting on public land in `index`.

    Sampled, not intersected. The old version repaired every candidate PAD-US
    polygon for every park, unioned the hits and intersected them, so a state
    with thousands of parks took an hour. Here the polygons were repaired once
    at load time, one STRtree holds them, and a park's coverage is the share of
    its sample points that fall inside public land: cheap point-in-polygon tests,
    no union, no intersection. A park that cannot be repaired or sampled scores
    0.0 rather than raising.
    """
    if geom is None or index is None:
        return 0.0
    target = repaired(geom)
    if target is None:
        return 0.0
    try:
        points = sample_ground(target)
    except Exception:
        points = []
    if not points:
        try:
            points = [target.representative_point()]
        except Exception:
            return 0.0
    tree, items = index
    return sum(covered_points(tree, items, points)) / len(points)


def is_private_name(name):
    """True when a name says club, HOA, apartments, hotel, a chain gym...

    Used to index the areas a court just outside a park must not hide in: a
    tennis club beside a public park is still a tennis club.
    """
    lowered = normalise(name)
    return bool(lowered) and any(word in lowered for word in PRIVATE_NAME_WORDS)


def is_public_rec_name(name):
    """A rec centre counts only when its name says it is one, and not a club."""
    lowered = normalise(name)
    if not lowered or any(word in lowered for word in PRIVATE_NAME_WORDS):
        return False
    return any(word in lowered for word in REC_NAME_WORDS)


def is_public_operator(value):
    """True when an operator/owner value names a public agency.

    The same proof the park rule uses, applied to a single field: OSM records who
    runs a court far more often than any dataset draws it — "City of Pasadena",
    "Los Angeles County", "LAUSD". A club or a company is refused first, so
    "Sunset Tennis Club" can never pass as a city operation, and "Lincoln" is not
    read as "inc".
    """
    text = normalise(value)
    if not text:
        return False
    if any(word in text for word in PRIVATE_NAME_WORDS):
        return False
    words = {word for word in re.split(r"[^a-z0-9]+", text) if word}
    if words & PRIVATE_OWNER_TOKENS:
        return False
    if words & PUBLIC_AGENCY_TOKENS:
        return True
    if text in PUBLIC_OWNERSHIP_VALUES:
        return True
    if any(phrase in text for phrase in PUBLIC_AGENCY_PHRASES):
        return True
    # School districts write themselves as one word: LAUSD, ABCUSD, ...ISD.
    return any(len(word) >= 5 and word.endswith(("usd", "isd")) for word in words)


def is_public_agency_park(item):
    """An OSM park whose operator/owner tags name a public agency.

    PAD-US never covers all public ground: a city park can sit on land PAD-US
    files under another owner, or not at all, and then the park-level rule never
    verifies it and its courts are judged one by one. OSM usually records who
    runs the place, so an operator or owner such as "City of Westlake Village",
    "Los Angeles County" or a recreation and park district, or an
    `ownership=municipal/county/state` tag, is proof that the park is public
    ground.

    Every one of the name/operator/owner/ownership values is checked for a club
    or private operation first, and agency words are matched as whole words, so
    a private park can't borrow a city's name and "Lincoln" is not read as "inc".
    """
    values = [normalise(item.get(key)) for key in
              ("name", "operator", "owner", "ownership")]
    for value in values:
        if not value:
            continue
        if any(word in value for word in PRIVATE_NAME_WORDS):
            return False
        if set(re.split(r"[^a-z0-9]+", value)) & PRIVATE_OWNER_TOKENS:
            return False
    if normalise(item.get("ownership")) in PUBLIC_OWNERSHIP_VALUES:
        return True
    for key in ("operator", "owner"):
        value = normalise(item.get(key))
        if not value:
            continue
        if any(phrase in value for phrase in PUBLIC_AGENCY_PHRASES):
            return True
        if set(re.split(r"[^a-z0-9]+", value)) & PUBLIC_AGENCY_TOKENS:
            return True
    return False


def resolve_campuses(areas, schools, tolerance_m=CAMPUS_TOLERANCE_M):
    """OSM school areas that provably hold a public 9-12 school.

    `areas` are OSM `amenity=school` polygons as {"id", "name", "geom"};
    `schools` are NCES points [(lon, lat, name)]. Only the areas that pass come
    back, so a private school (which NCES does not list at all) can never be
    labelled public.

    The NCES point must be inside *this* polygon (within `tolerance_m`) and the
    names must agree, so a private campus beside a public high school, and an
    elementary school beside one, can't borrow the high school's record.
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
        for position in tree.query(geom):
            point = points[position]
            if not (geom.covers(point) or geom.distance(point) <= tolerance):
                continue  # a different campus: never match across polygons
            nces_name = schools[position][2]
            if not names_agree(area.get("name"), nces_name):
                continue  # "Wildwood School" vs a high school over the fence
            if is_primary_only(area.get("name"), nces_name):
                continue  # "... Elementary"/"... Middle" unless the record is combined
            proof = schools[position]
            break
        if proof is None:
            continue
        campuses.append({**area, "school": proof[2]})
    return campuses


def decision(verdict, reason, campus=None, attach=None, near=False):
    """One court's verdict, with what the builder needs to place it.

    `attach` is the id of the park whose pin the court joins (see
    build_state.group); `near` marks a court kept only by the near-public rule,
    so the build report can list every court it added.
    """
    return {"verdict": verdict, "reason": reason, "campus": campus,
            "attach": attach, "near": near}


def classify(point, context):
    """Return the decision for one court/field location.

    `context` carries the prepared indexes (padus, school_areas, osm_parks,
    campuses_by_id) plus the OSM facts about the venue itself (name, sports,
    access, leisure). `campus` is the public high school the court belongs to, so
    the builder can group a school's courts into one venue named after the
    school.

    Order matters. A court inside an OSM school site is decided by the school
    rule first: on a verified public grades 9-12 campus it is "school",
    otherwise it is dropped — unless it is also inside an OSM park, which is a
    joint-use park. PAD-US is only consulted after that, because a public
    school's fields are usually public land too and were being labelled "park".

    A park or rec centre that was verified as a whole comes next: its courts
    inherit that verdict, which is why the individual PAD-US test is last. Park
    boundaries and PAD-US polygons never line up exactly, and testing each court
    on its own quietly dropped real courts from public parks.

    A court drawn just outside all of that is the last case before a court is
    thrown away: standing within NEAR_PUBLIC_M of a verified public park, or of a
    public high-school campus, it is kept and attached to that place's pin, so
    "Live Oak Park" and the "Tennis courts" 105 m outside its fence become one
    pin counting both. Two things stop that becoming a licence: the court must
    not stand in an excluded area (a club, private school, golf course, a
    commercial name) and it must not be tagged access=private/customers/members.
    Golf is never rescued at all — a driving range is not a pickup court.

    After that, a field is a public place on its own proof: access=yes (or the
    other open values), or an operator/owner naming a city, county, park district
    or school district.
    """
    name = normalise(context.get("name"))
    sports = set(context.get("sports") or ())
    access = str(context.get("access") or "").strip().lower()
    leisure = context.get("leisure") or ""

    if name and any(word in name for word in PRIVATE_NAME_WORDS):
        return decision(None, "named private or commercial")

    school_areas = inside_all(context.get("school_areas"), point)
    if school_areas:
        campuses = context.get("campuses_by_id") or {}
        # Overlapping areas are common (a campus inside a district parcel), so a
        # verified campus anywhere in the stack wins.
        for area in school_areas:
            campus = campuses.get(area["id"])
            if campus is not None:
                return decision(
                    "school",
                    f"inside {campus['name'] or 'a public high school'} (NCES grades 9-12)",
                    campus)
        if inside(context.get("osm_parks"), point) is not None:
            return decision("park", "inside a park on a school site (joint use)")
        return decision(None, "inside a school site that is not a verified public high school")

    # The area has already been judged as a whole, so every court standing on it
    # is public ground — including the ones a few metres outside the PAD-US
    # polygon. Only a court tagged closed is still refused.
    area = inside(context.get("public_areas"), point)
    if area is not None and access not in CLOSED_ACCESS:
        return decision(area["verdict"], f"inside {area['name'] or 'a verified public area'}")

    if access in CLOSED_ACCESS:
        return decision(None, f"tagged access={access} and not in a public park or school")

    # Golf is not a pickup court: no near-park rescue, and no pin of its own.
    if sports and sports <= GOLF_SPORTS:
        return decision(None, "golf or a driving range, not a pickup court")

    # Before PAD-US, on purpose: a court on public land but outside the park
    # polygon was kept as its own pin ("Tennis courts" beside "Live Oak Park"),
    # and the point of this rule is that it is the park's court. PAD-US still
    # decides a court that is near no park at all.
    if context.get("allow_near", True) and inside(context.get("excluded"), point) is None:
        target = nearby(context.get("public_areas"), point)
        if target is not None:
            return decision(
                "park",
                f"just outside {target['name'] or 'a public park'} (within {NEAR_PUBLIC_M} m)",
                attach=target["id"], near=True)
        campus = nearby(context.get("campus_index"), point)
        if campus is not None:
            return decision(
                "school",
                f"just outside {campus['name'] or 'a public high school'} (within {NEAR_PUBLIC_M} m)",
                campus, near=True)

    park = inside(context.get("padus"), point)
    if park is not None:
        return decision("park", f"inside PAD-US public land: {park['name'] or 'unnamed unit'}")

    # A field OSM says is open, or that a city, county or school district runs,
    # is a public place even when no park polygon covers it.
    if access in OPEN_ACCESS:
        return decision("known", f"tagged access={access}", near=True)
    operator = context.get("operator") or context.get("owner")
    if is_public_operator(operator):
        return decision("known", f"run by {normalise(operator)}", near=True)

    if leisure == "sports_centre" and name and any(word in name for word in REC_NAME_WORDS):
        return decision("known", "named public recreation or sports centre")
    if "beach_volleyball" in sports:
        return decision("known", "beach volleyball court")

    return decision(None, "no public source places it in a park, public school or rec centre")


LABELS = {
    "park": "Public park",
    "school": "Public high school - check public hours",
    "known": "Rec center",
}

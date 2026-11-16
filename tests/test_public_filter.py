"""Unit tests for the near-public rule in public_filter.py.

Synthetic geometry only: small squares for a park, a club, a campus, and points
80-400 m apart on paper. shapely is needed to import public_filter; without it
the module skips with a reason, and a skip is not a pass.

The rule under test: a pitch drawn outside every park, school and PAD-US polygon
is kept when it stands within NEAR_PUBLIC_M of a verified public park or public
high-school campus, is not tagged access=private/customers/members, and is not
inside a club, private school, golf course or commercial facility. A court kept
that way joins the park's pin (build_state.group reads the attach id), and golf
is never rescued.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

try:
    import shapely  # noqa: F401  the filter needs it at import time
except ImportError:  # pragma: no cover - the workflow installs it before this runs
    raise unittest.SkipTest("shapely is needed to import public_filter; pip install shapely")

import build_state  # noqa: E402
import public_filter  # noqa: E402


def square(lat, lon, side_deg):
    half = side_deg / 2.0
    return [[(lon - half, lat - half), (lon + half, lat - half),
             (lon + half, lat + half), (lon - half, lat + half),
             (lon - half, lat - half)]]


def area(osm_id, name, lat, lon, side_deg):
    return {"id": osm_id, "name": name,
            "geom": public_filter.polygons_of_rings(square(lat, lon, side_deg))}


def north(lat, metres):
    """A latitude `metres` north of `lat`."""
    return lat + metres / 111_000.0


def context(parks=(), excluded=(), campuses=()):
    """The indexes classify() needs, built from synthetic areas."""
    return {
        "padus": None,
        "campuses_by_id": {campus["id"]: campus for campus in campuses},
        "campus_index": public_filter.build_index(list(campuses)),
        "school_areas": None,
        "osm_parks": None,
        "public_areas": public_filter.build_index(
            [{**park, "verdict": "park"} for park in parks]),
        "excluded": public_filter.build_index(list(excluded)),
    }


def facts(**overrides):
    base = {"name": "", "sports": ("basketball",), "access": "", "leisure": "pitch",
            "operator": "", "owner": ""}
    base.update(overrides)
    return base


class NearParkTest(unittest.TestCase):
    """A court outside the park fence is still the park's court."""

    def park(self):
        # 0.002 degrees on a side is ~222 m: half of it (111 m) is the fence.
        return area("way/1", "Live Oak Park", 34.0, -118.0, 0.002)

    def test_a_public_court_eighty_metres_outside_the_fence_joins_the_park(self):
        point = (-118.0, north(34.0, 191))  # 111 m to the fence, 80 m past it
        decided = public_filter.classify(point, {**context(parks=[self.park()]), **facts()})

        self.assertEqual(decided["verdict"], "park")
        self.assertEqual(decided["attach"], "way/1", "it joins that park's pin")
        self.assertTrue(decided["near"])
        self.assertIn("Live Oak Park", decided["reason"])

    def test_a_court_further_than_the_limit_is_not_rescued(self):
        point = (-118.0, north(34.0, 111 + 400))  # 400 m past the fence
        decided = public_filter.classify(point, {**context(parks=[self.park()]), **facts()})

        self.assertIsNone(decided["verdict"])
        self.assertIsNone(decided["attach"])

    def test_the_court_lands_on_the_parks_pin_not_beside_it(self):
        col = build_state.Collector(keep_private=True)
        col.add("way", 1, {"leisure": "park", "name": "Live Oak Park"},
                rings=square(34.0, -118.0, 0.002))
        col.add("way", 2, {"leisure": "pitch", "sport": "tennis"},
                rings=square(north(34.0, 191), -118.0, 0.0002))
        # What the builder does with the decision: hand the pitch to the park.
        col.pitches[-1]["attach_park"] = "way/1"

        venues = build_state.group(col, keep_restricted=True)

        self.assertEqual(len(venues), 1, "one park, one pin")
        self.assertEqual(venues[0]["id"], "way/1")
        self.assertEqual(venues[0]["n"], "Live Oak Park")
        self.assertEqual(venues[0]["s"], {"tennis": 1})

    def test_a_tennis_centre_beside_the_park_joins_it(self):
        col = build_state.Collector(keep_private=True)
        col.add("way", 1, {"leisure": "park", "name": "Orange Grove Park"},
                rings=square(34.0, -118.0, 0.002))
        # OSM often maps a park's tennis courts as a sports centre a few metres
        # outside the park polygon with no pitches drawn inside it.
        col.add("way", 2, {"leisure": "sports_centre", "sport": "tennis"},
                rings=square(north(34.0, 191), -118.0, 0.0004))
        col.pitches[-1]["attach_park"] = "way/1"

        venues = build_state.group(col, keep_restricted=True)

        self.assertEqual(len(venues), 1, "the centre's pin folds into the park")
        self.assertEqual(venues[0]["id"], "way/1")
        self.assertEqual(venues[0]["s"], {"tennis": 1})

    def test_a_private_club_beside_a_park_is_not_rescued(self):
        point = (-118.0, north(34.0, 191))
        club = area("way/9", "Sunset Tennis Club", point[1], point[0], 0.0004)
        decided = public_filter.classify(
            point, {**context(parks=[self.park()], excluded=[club]), **facts(sports=("tennis",))})

        self.assertIsNone(decided["verdict"], "a club next door stays a club")

    def test_a_court_tagged_private_is_not_rescued(self):
        point = (-118.0, north(34.0, 191))
        decided = public_filter.classify(
            point, {**context(parks=[self.park()]), **facts(access="private")})

        self.assertIsNone(decided["verdict"])
        self.assertIn("access=private", decided["reason"])

    def test_golf_beside_a_park_is_not_a_court(self):
        point = (-118.0, north(34.0, 150))
        decided = public_filter.classify(
            point, {**context(parks=[self.park()]), **facts(sports=("golf",))})

        self.assertIsNone(decided["verdict"], "a driving range is not a pickup court")
        self.assertIn("golf", decided["reason"])

    def test_a_standalone_court_with_a_public_operator_is_kept(self):
        decided = public_filter.classify(
            (34.05, -118.0), {**context(), **facts(operator="City of Pasadena")})

        self.assertEqual(decided["verdict"], "known")
        self.assertTrue(decided["near"])

    def test_access_yes_keeps_a_standalone_court(self):
        decided = public_filter.classify(
            (34.05, -118.0), {**context(), **facts(access="yes")})

        self.assertEqual(decided["verdict"], "known")
        self.assertIn("access=yes", decided["reason"])

    def test_a_named_club_court_is_refused_before_anything_else(self):
        decided = public_filter.classify(
            (-118.0, north(34.0, 191)),
            {**context(parks=[self.park()]), **facts(name="Sunset Tennis Club")})

        self.assertIsNone(decided["verdict"])


class NearSchoolTest(unittest.TestCase):
    """A school's field is often outside the school building's polygon."""

    def campus(self):
        return {"id": "way/20", "name": "Fairfax Senior High School",
                "geom": public_filter.polygons_of_rings(square(34.0, -118.0, 0.001))}

    def test_a_school_baseball_field_joins_the_school(self):
        point = (-118.0, north(34.0, 55 + 100))  # 100 m past the campus fence
        decided = public_filter.classify(
            point, {**context(campuses=[self.campus()]), **facts(sports=("baseball",))})

        self.assertEqual(decided["verdict"], "school")
        self.assertEqual(decided["campus"]["id"], "way/20")
        self.assertTrue(decided["near"])

    def test_a_field_far_from_the_campus_is_not_the_schools(self):
        point = (-118.0, north(34.0, 500))
        decided = public_filter.classify(
            point, {**context(campuses=[self.campus()]), **facts(sports=("baseball",))})

        self.assertIsNone(decided["verdict"])


class PublicOperatorTest(unittest.TestCase):
    def test_a_city_a_county_and_a_school_district_are_public(self):
        for value in ("City of Los Angeles", "Los Angeles County", "LAUSD",
                      "Pasadena Unified School District", "City of Long Beach Parks, Recreation and Marine"):
            self.assertTrue(public_filter.is_public_operator(value), value)

    def test_a_club_or_a_company_is_not(self):
        for value in ("Sunset Tennis Club", "LA Fitness", "Wilson & Sons LLC",
                      "Sun City Homeowners Association", ""):
            self.assertFalse(public_filter.is_public_operator(value), value)

    def test_a_private_named_area_is_recognised(self):
        self.assertTrue(public_filter.is_private_name("Sunset Tennis Club"))
        self.assertTrue(public_filter.is_private_name("Marina City Apartments"))
        self.assertFalse(public_filter.is_private_name("Live Oak Park"))
        self.assertFalse(public_filter.is_private_name(""))


if __name__ == "__main__":
    unittest.main()

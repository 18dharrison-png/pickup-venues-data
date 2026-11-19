"""Unit tests for the near-public rule in public_filter.py.

Synthetic geometry only: small squares for a park, a club, a campus, and points
80-400 m apart on paper. shapely is needed to import public_filter; without it
the module skips with a reason, and a skip is not a pass.

The rule under test: a pitch drawn outside every park, school and PAD-US polygon
is kept when it stands within NEAR_PUBLIC_M of a verified public park or public
high-school campus, is not tagged access=private/customers/members, and is not
inside a club, private school, golf course or commercial facility. A court kept
that way joins the park's pin (build_state.group reads the attach id), and golf
is never rescued. A public area that is not a place to play — golf course,
cemetery, nature preserve, garden, estate, open space, conservation land — never
takes it. An untagged court only joins within NEAR_PUBLIC_UNTAGGED_M, and a court
with a name of its own keeps its own pin.

build_public.public_shortcut is tested too: recreation ground and a city or
county operator keep a field beyond the 50 m cap, while golf, closed access and
private ground stay out.
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

import build_public  # noqa: E402
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


def context(parks=(), excluded=(), campuses=(), recreation=()):
    """The indexes classify() needs, built from synthetic areas."""
    return {
        "padus": None,
        "campuses_by_id": {campus["id"]: campus for campus in campuses},
        "campus_index": public_filter.build_index(list(campuses)),
        "school_areas": None,
        "osm_parks": None,
        "public_areas": public_filter.build_index(
            [{**park, "verdict": "park"} for park in parks]),
        "recreation": public_filter.build_index(list(recreation)),
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
        decided = public_filter.classify(
            point, {**context(parks=[self.park()]), **facts(access="yes")})

        self.assertEqual(decided["verdict"], "park")
        self.assertEqual(decided["attach"], "way/1", "it joins that park's pin")
        self.assertTrue(decided["near"])
        self.assertIn("Live Oak Park", decided["reason"])

    def test_an_untagged_court_has_to_be_close_to_the_fence(self):
        # 80 m past the fence: too far for a court nobody has tagged. These are
        # the condo and estate courts that were joining parks.
        point = (-118.0, north(34.0, 191))
        decided = public_filter.classify(
            point, {**context(parks=[self.park()]), **facts(access="")})

        self.assertIsNone(decided["attach"], "untagged and 80 m out: not the park's")
        self.assertIsNone(decided["verdict"])

    def test_a_court_tagged_open_may_be_the_full_distance_out(self):
        point = (-118.0, north(34.0, 191))
        decided = public_filter.classify(
            point, {**context(parks=[self.park()]), **facts(access="yes")})

        self.assertEqual(decided["attach"], "way/1")
        self.assertIn(str(public_filter.NEAR_PUBLIC_M), decided["reason"])

    def test_an_untagged_court_just_past_the_fence_still_joins(self):
        # 30 m past the fence: close enough that it is plainly the park's court.
        point = (-118.0, north(34.0, 111 + 30))
        decided = public_filter.classify(
            point, {**context(parks=[self.park()]), **facts(access="")})

        self.assertEqual(decided["attach"], "way/1")

    def test_a_court_on_residential_ground_is_not_attached(self):
        point = (-118.0, north(34.0, 111 + 30))
        houses = area("way/40", "", point[1], point[0], 0.002)
        decided = public_filter.classify(
            point, {**context(parks=[self.park()], excluded=[houses]), **facts(access="")})

        self.assertIsNone(decided["attach"], "inside the subdivision: not the park's")

    def test_a_garden_or_estate_never_takes_the_court(self):
        for name in ("Virginia Robinson Gardens", "Greystone Estate",
                     "Huntington Botanical Garden", "Rancho Los Alamitos Historic Site"):
            with self.subTest(name=name):
                garden = area("way/41", name, 34.0, -118.0, 0.002)
                decided = public_filter.classify(
                    (-118.0, north(34.0, 191)), {**context(parks=[garden]), **facts(access="yes")})

                self.assertIsNone(decided["attach"], name)

    def test_a_named_facility_is_not_folded_into_the_park(self):
        # Venice Skate Park is its own place, 145 m from the boardwalk polygon.
        decided = public_filter.classify(
            (-118.0, north(34.0, 111 + 30)),
            {**context(parks=[self.park()]), **facts(name="Venice Skate Park", access="yes")})

        self.assertIsNone(decided["attach"], "a named facility keeps its own pin")

    def test_a_generic_court_name_still_folds_in(self):
        decided = public_filter.classify(
            (-118.0, north(34.0, 111 + 30)),
            {**context(parks=[self.park()]), **facts(name="Tennis courts")})

        self.assertEqual(decided["attach"], "way/1")

    def test_a_facility_shared_name_is_one_pin_outside_the_park(self):
        # Santa Barbara: the tennis centre's pitches are named, half inside the
        # refuge polygon and half outside. All of it is one facility, so the
        # refuge must not count any of it.
        col = build_state.Collector(keep_private=True)
        col.add("way", 1, {"leisure": "park", "name": "Andrée Clark Bird Refuge"},
                rings=square(34.0, -118.0, 0.002))
        col.add("way", 2, {"leisure": "pitch", "sport": "pickleball",
                           "name": "Municipal Tennis and Pickleball Center"},
                rings=square(34.0, -118.0, 0.0002))
        col.add("way", 3, {"leisure": "pitch", "sport": "pickleball",
                           "name": "Municipal Tennis and Pickleball Center"},
                rings=square(north(34.0, 60), -118.0, 0.0002))
        for pitch in col.pitches:
            pitch["own_facility"] = True

        venues = build_state.group(col, keep_restricted=True)

        self.assertEqual([venue["n"] for venue in venues],
                         ["Municipal Tennis and Pickleball Center"])
        self.assertEqual(venues[0]["s"], {"pickleball": 2})

    def test_a_field_on_recreation_ground_is_kept(self):
        # A city's sports complex is drawn as recreation ground, and its diamonds
        # sit far inside it: no park polygon, no access tag, still public.
        ground = area("way/50", "Sepulveda Basin Sports Complex", 34.0, -118.0, 0.004)
        settled = {"recreation": public_filter.build_index([ground]),
                   "public_areas": None, "excluded": None}
        decided = build_public.public_shortcut(
            (-118.0, 34.0),
            {"sports": ("baseball",), "access": "", "operator": ""}, settled)

        self.assertEqual(decided["verdict"], "known")
        self.assertIn("recreation ground", decided["reason"])

    def test_a_city_run_field_beyond_the_fence_is_kept(self):
        # 400 m outside the park fence, untagged, but the city runs it.
        settled = {"recreation": None, "public_areas": None, "excluded": None}
        decided = build_public.public_shortcut(
            (-118.0, north(34.0, 111 + 400)),
            {"sports": ("baseball",), "access": "",
             "operator": "City of Los Angeles Department of Recreation and Parks"},
            settled)

        self.assertEqual(decided["verdict"], "known")
        self.assertIn("run by", decided["reason"])

    def test_a_city_run_golf_course_is_still_not_a_court(self):
        settled = {"recreation": None, "public_areas": None, "excluded": None}
        decided = build_public.public_shortcut(
            (-118.0, north(34.0, 200)),
            {"sports": ("golf",), "access": "", "operator": "City of Pasadena"},
            settled)

        self.assertIsNone(decided, "a city golf course is not a pickup court")

    def test_an_untagged_court_on_private_ground_stays_out(self):
        # The public-operator shortcut must not open the condo courts: no
        # operator tag, inside a residential block, 30 m past the park fence.
        point = (-118.0, north(34.0, 111 + 30))
        houses = area("way/51", "", point[1], point[0], 0.004)
        settled = {"recreation": None, "public_areas": None,
                   "excluded": public_filter.build_index([houses])}
        decided = build_public.public_shortcut(
            point, {"sports": ("tennis",), "access": "", "operator": ""}, settled)

        self.assertIsNone(decided, "no proof of its own: falls through to the rule")
        self.assertIsNone(public_filter.classify(
            point, {**context(parks=[self.park()], excluded=[houses]),
                    **facts(sports=("tennis",))})["verdict"])

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
            point, {**context(parks=[self.park()], excluded=[club]),
                    **facts(sports=("tennis",), access="yes")})

        # The club's ground is excluded, so the court does not join the park; it
        # is only kept at all because the court itself is tagged open.
        self.assertIsNone(decided["attach"], "a club next door stays a club")
        self.assertNotIn("Live Oak Park", decided["reason"])

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

    def test_a_court_beside_a_nature_refuge_is_not_folded_into_it(self):
        refuge = area("way/30", "Andrée Clark Bird Refuge", 34.0, -118.0, 0.002)
        decided = public_filter.classify(
            (-118.0, north(34.0, 191)),
            {**context(parks=[refuge]), **facts(sports=("tennis",), access="yes")})

        self.assertIsNone(decided["attach"], "a refuge is public ground, not a place to play")
        self.assertNotIn("Refuge", decided["reason"])

    def test_golf_cemetery_preserve_and_open_space_refuse_the_fold(self):
        for name in ("Elings Park Golf Course", "Sunset Cemetery",
                     "San Elijo Lagoon Preserve", "Laguna Coast Open Space",
                     "Yolo Bypass Wildlife Area", "Greenbelt Conservation Land"):
            with self.subTest(name=name):
                parcel = area("way/31", name, 34.0, -118.0, 0.002)
                decided = public_filter.classify(
                    (-118.0, north(34.0, 191)), {**context(parks=[parcel]), **facts(access="yes")})

                self.assertIsNone(decided["attach"], name)

    def test_ordinary_parks_still_take_their_court(self):
        # The exclusion list must not swallow a park that merely shares a word.
        for name in ("Reservoir Park", "Memorial Park", "Refugio Park",
                     "Live Oak Park"):
            with self.subTest(name=name):
                park = area("way/32", name, 34.0, -118.0, 0.002)
                decided = public_filter.classify(
                    (-118.0, north(34.0, 191)), {**context(parks=[park]), **facts(access="yes")})

                self.assertEqual(decided["attach"], "way/32", name)

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
            {**context(parks=[self.park()]), **facts(name="Sunset Tennis Club", access="yes")})

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

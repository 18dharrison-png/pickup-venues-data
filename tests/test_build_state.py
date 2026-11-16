"""Unit tests for the pin and dedupe helpers in build_state.py.

Synthetic venues and rings only — no OSM extract, no PBF, no network. The
functions under test are the ones `group()` calls: `ring_area_m2` decides
whether a park is big, `pins_centroid` decides where its pin goes, and
`merge_same_name` collapses duplicate pins for one place.
"""
import json
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

try:
    import osmium  # noqa: F401  build_state reads OSM with it, at import time
except ImportError:  # pragma: no cover - the workflow installs it before this runs
    raise unittest.SkipTest("osmium is needed to import build_state; pip install 'osmium>=4,<5'")

import build_state  # noqa: E402


def venue(vid, name, lat, lon, sports, **extra):
    out = {"id": vid, "n": name, "la": lat, "lo": lon, "s": dict(sports)}
    out.update(extra)
    return out


def square(lat, lon, side_deg):
    """A closed ring, `side_deg` on a side, as osmium hands it over."""
    half = side_deg / 2.0
    return [[(lon - half, lat - half), (lon + half, lat - half),
             (lon + half, lat + half), (lon - half, lat + half),
             (lon - half, lat - half)]]


class DistanceTest(unittest.TestCase):
    def test_a_degree_of_latitude_is_about_111_km(self):
        self.assertAlmostEqual(build_state.metres(34.0, -118.0, 35.0, -118.0), 111_320, delta=50)

    def test_distance_is_symmetric_and_zero_at_a_point(self):
        a = build_state.metres(34.0, -118.0, 34.01, -118.02)
        b = build_state.metres(34.01, -118.02, 34.0, -118.0)
        self.assertAlmostEqual(a, b, places=6)
        self.assertEqual(build_state.metres(34.0, -118.0, 34.0, -118.0), 0.0)


class AreaTest(unittest.TestCase):
    def test_a_500_metre_square_is_about_200_000_square_metres(self):
        # 0.0045 degrees of latitude is ~501 m, but a degree of longitude at
        # 34°N is only ~0.83 of that, so the square is ~501 x 415 m ≈ 208 000 m².
        area = build_state.ring_area_m2(square(34.0, -118.0, 0.0045))
        self.assertAlmostEqual(area, 208_000, delta=6_000)

    def test_no_rings_is_zero(self):
        self.assertEqual(build_state.ring_area_m2(None), 0.0)
        self.assertEqual(build_state.ring_area_m2([]), 0.0)

    def test_the_threshold_sits_between_a_neighbourhood_park_and_griffith(self):
        small = build_state.ring_area_m2(square(34.0, -118.0, 0.003))   # ~330 x 280 m
        large = build_state.ring_area_m2(square(34.0, -118.0, 0.02))    # ~2.2 km wide
        self.assertLess(small, build_state.LARGE_PARK_M2)
        self.assertGreater(large, build_state.LARGE_PARK_M2)


class PinsCentroidTest(unittest.TestCase):
    def test_the_pin_moves_to_the_pitches(self):
        # A park polygon whose centroid sits 300 m north of its courts.
        members = [{"lat": 34.0000, "lon": -118.0000},
                   {"lat": 34.0000, "lon": -118.0020},
                   {"lat": 34.0000, "lon": -118.0040}]
        lon, lat = build_state.pins_centroid(members)
        self.assertAlmostEqual(lat, 34.0, places=6)
        self.assertAlmostEqual(lon, -118.002, places=6)

    def test_each_pitch_counts_once_not_once_per_sport(self):
        # One soccer field and one five-sport court: the pin is the midpoint,
        # not pulled five times towards the busy court.
        members = [{"lat": 34.0, "lon": -118.0}, {"lat": 34.0, "lon": -118.002}]
        lon, _ = build_state.pins_centroid(members)
        self.assertAlmostEqual(lon, -118.001, places=6)

    def test_no_members_has_no_pin(self):
        self.assertIsNone(build_state.pins_centroid([]))


class MergeSameNameTest(unittest.TestCase):
    def test_four_pins_for_one_stretch_of_sand_become_one(self):
        # Del Rey Lagoon: the same name, the same sport, metres apart.
        venues = [venue("way/1", "Beach Volleyball courts", 33.9770, -118.4610, {"beach_volleyball": 1}),
                  venue("way/2", "Beach Volleyball courts", 33.9771, -118.4610, {"beach_volleyball": 1}),
                  venue("way/3", "Beach Volleyball courts", 33.9772, -118.4610, {"beach_volleyball": 1}),
                  venue("way/4", "Beach Volleyball courts", 33.9773, -118.4610, {"beach_volleyball": 1})]
        out = build_state.merge_same_name(venues)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "way/1", "the lowest id wins, so the result is stable")
        self.assertEqual(out[0]["s"], {"beach_volleyball": 4})

    def test_a_lit_pitch_keeps_the_merged_pin_lit(self):
        venues = [venue("way/1", "Tennis courts", 34.0, -118.0, {"tennis": 2}, lt=False),
                  venue("way/2", "Tennis courts", 34.0001, -118.0, {"tennis": 2}, lt=True)]
        out = build_state.merge_same_name(venues)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["lt"])

    def test_a_different_name_is_a_different_place(self):
        venues = [venue("way/1", "Reed Park", 34.0, -118.0, {"tennis": 2}),
                  venue("way/2", "Reed Park Tennis Center", 34.0001, -118.0, {"tennis": 2})]
        self.assertEqual(len(build_state.merge_same_name(venues)), 2)

    def test_the_same_name_far_apart_stays_two_pins(self):
        # ~1.1 km: two parks can share a name across a city.
        venues = [venue("way/1", "Veterans Park", 34.0, -118.0, {"basketball": 2}),
                  venue("way/2", "Veterans Park", 34.01, -118.0, {"basketball": 2})]
        self.assertEqual(len(build_state.merge_same_name(venues)), 2)

    def test_no_shared_sport_means_no_merge(self):
        # A park with a tennis pin and a separate basketball pin is not a
        # duplicate: one place, two lists, and the app shows both.
        venues = [venue("way/1", "Reed Park", 34.0, -118.0, {"tennis": 2}),
                  venue("way/2", "Reed Park", 34.0001, -118.0, {"basketball": 2})]
        self.assertEqual(len(build_state.merge_same_name(venues)), 2)

    def test_names_are_compared_ignoring_case_and_spacing(self):
        venues = [venue("way/1", "Beach  Volleyball Courts", 34.0, -118.0, {"beach_volleyball": 1}),
                  venue("way/2", "beach volleyball courts", 34.0001, -118.0, {"beach_volleyball": 1})]
        self.assertEqual(len(build_state.merge_same_name(venues)), 1)

    def test_unnamed_courts_are_never_merged_by_name(self):
        venues = [venue("way/1", None, 34.0, -118.0, {"tennis": 1}),
                  venue("way/2", None, 34.0001, -118.0, {"tennis": 1})]
        self.assertEqual(len(build_state.merge_same_name(venues)), 2)

    def test_sports_are_written_biggest_first(self):
        venues = [venue("way/1", "Reed Park", 34.0, -118.0, {"tennis": 1}),
                  venue("way/2", "Reed Park", 34.0001, -118.0, {"tennis": 3, "basketball": 2})]
        out = build_state.merge_same_name(venues)
        self.assertEqual(list(out[0]["s"]), ["tennis", "basketball"])

    def test_the_input_list_is_untouched(self):
        venues = [venue("way/1", "Beach Volleyball courts", 34.0, -118.0, {"beach_volleyball": 1}),
                  venue("way/2", "Beach Volleyball courts", 34.0001, -118.0, {"beach_volleyball": 1})]
        snapshot = json.dumps(venues, sort_keys=True)
        build_state.merge_same_name(venues)
        self.assertEqual(json.dumps(venues, sort_keys=True), snapshot)

    def test_an_empty_list_is_an_empty_list(self):
        self.assertEqual(build_state.merge_same_name([]), [])


class SportsCentreTest(unittest.TestCase):
    """A sports centre is a place: its drawn courts are the count, not its tag.

    North Venice Little League published twice — the centre as baseball 1 +
    softball 1, and a separate "Baseball field" pin holding the diamonds drawn
    inside it. Same for Eastview, Bishop Canyon, Bad News Bears and Glendale
    Sports Complex.
    """

    def test_a_pitch_inside_a_sports_centre_is_that_centres_court(self):
        col = build_state.Collector(keep_private=True)
        col.add("way", 907145142,
                {"leisure": "sports_centre", "name": "North Venice Little League",
                 "sport": "baseball;softball"},
                rings=square(34.0, -118.0, 0.002))
        col.add("way", 81339277, {"leisure": "pitch", "sport": "baseball"},
                rings=square(34.0002, -118.0002, 0.0003))

        venues = build_state.group(col, keep_restricted=True)

        self.assertEqual(len(venues), 1, "one facility is one pin")
        self.assertEqual(venues[0]["id"], "way/907145142")
        self.assertEqual(venues[0]["n"], "North Venice Little League")
        self.assertEqual(venues[0]["s"], {"baseball": 1},
                         "the drawn diamond counts, the centre's tag does not")

    def test_two_diamonds_inside_one_centre_count_two(self):
        col = build_state.Collector(keep_private=True)
        col.add("way", 20304799,
                {"leisure": "sports_centre", "name": "Eastview Little League",
                 "sport": "baseball"},
                rings=square(34.0, -118.0, 0.003))
        col.add("way", 666826138, {"leisure": "pitch", "sport": "baseball"},
                rings=square(34.0003, -118.0003, 0.0002))
        col.add("way", 666826139, {"leisure": "pitch", "sport": "baseball"},
                rings=square(33.9997, -118.0003, 0.0002))

        venues = build_state.group(col, keep_restricted=True)

        self.assertEqual(len(venues), 1)
        self.assertEqual(venues[0]["s"], {"baseball": 2})

    def test_a_court_just_outside_the_fence_is_still_the_centres(self):
        # Venues draw the fence, not every diamond: North Venice's other two
        # diamonds are 40-70 m outside the centre polygon but part of it.
        col = build_state.Collector(keep_private=True)
        col.add("way", 907145142,
                {"leisure": "sports_centre", "name": "North Venice Little League",
                 "sport": "baseball;softball"},
                rings=square(34.0, -118.0, 0.001))
        col.add("way", 1, {"leisure": "pitch", "sport": "baseball"},
                rings=square(34.0002, -118.0, 0.0002))
        col.add("way", 2, {"leisure": "pitch", "sport": "baseball"},
                rings=square(34.0009, -118.0, 0.0002))   # ~80 m north, outside the fence

        venues = build_state.group(col, keep_restricted=True)

        self.assertEqual(len(venues), 1, "the complex is one pin")
        self.assertEqual(venues[0]["n"], "North Venice Little League")
        self.assertEqual(venues[0]["s"], {"baseball": 2})

    def test_a_court_of_another_sport_outside_the_fence_stays_its_own_pin(self):
        col = build_state.Collector(keep_private=True)
        col.add("way", 907145142,
                {"leisure": "sports_centre", "name": "North Venice Little League",
                 "sport": "baseball"},
                rings=square(34.0, -118.0, 0.001))
        col.add("way", 1, {"leisure": "pitch", "sport": "baseball"},
                rings=square(34.0002, -118.0, 0.0002))
        col.add("way", 2, {"leisure": "pitch", "sport": "tennis"},
                rings=square(34.0009, -118.0, 0.0002))

        venues = build_state.group(col, keep_restricted=True)

        self.assertEqual(len(venues), 2, "tennis courts next door are not the little league")

    def test_a_centre_with_no_drawn_courts_falls_back_to_its_own_tag(self):
        col = build_state.Collector(keep_private=True)
        col.add("way", 5,
                {"leisure": "sports_centre", "name": "Pan Pacific Recreation Center",
                 "sport": "pickleball"},
                rings=square(34.0, -118.0, 0.001))

        venues = build_state.group(col, keep_restricted=True)

        self.assertEqual(len(venues), 1)
        self.assertEqual(venues[0]["n"], "Pan Pacific Recreation Center")
        self.assertEqual(venues[0]["s"], {"pickleball": 1})

    def test_a_park_next_to_a_centre_keeps_its_own_courts(self):
        col = build_state.Collector(keep_private=True)
        col.add("way", 1, {"leisure": "park", "name": "Mar Vista Park"},
                rings=square(34.0, -118.0, 0.006))
        col.add("way", 2, {"leisure": "sports_centre", "name": "Mar Vista Recreation Center",
                            "sport": "basketball"},
                rings=square(34.0015, -118.0, 0.001))
        col.add("way", 10, {"leisure": "pitch", "sport": "tennis"},
                rings=square(33.9985, -118.0, 0.0003))
        col.add("way", 11, {"leisure": "pitch", "sport": "basketball"},
                rings=square(34.0015, -117.9998, 0.0002))

        venues = build_state.group(col, keep_restricted=True)
        by_name = {venue["n"]: venue["s"] for venue in venues}

        self.assertEqual(by_name.get("Mar Vista Park"), {"tennis": 1},
                         "the park keeps the court that is in the park")
        self.assertEqual(by_name.get("Mar Vista Recreation Center"), {"basketball": 1},
                         "the centre counts its own drawn court once")


class SplitParkTest(unittest.TestCase):
    """One park drawn as two polygons is one park.

    Heartwell Park is two ways with the same name and the same GNIS feature id,
    split where a path crosses it 29 m apart. It published as two pins
    ("Heartwell Park" baseball 4 and "Heartwell Park" baseball 11).
    """

    def parks(self):
        tags = {"leisure": "park", "name": "Heartwell Park", "gnis:feature_id": "243327"}
        return [build_state.polygon("way", 27207229, tags, square(34.0, -118.0, 0.003)),
                build_state.polygon("way", 27207478, tags, square(34.0, -117.995, 0.003))]

    def test_two_polygons_with_one_feature_id_become_one_park(self):
        merged = build_state.merge_park_polygons(self.parks())

        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]["rings"]), 2, "both halves are kept")
        self.assertEqual(merged[0]["id"], "way/27207229", "the first id stays, so output is stable")

    def test_the_merged_park_holds_the_courts_in_both_halves(self):
        col = build_state.Collector(keep_private=True)
        tags = {"leisure": "park", "name": "Heartwell Park", "gnis:feature_id": "243327"}
        col.add("way", 1, tags, rings=square(34.0, -118.0, 0.003))
        col.add("way", 2, tags, rings=square(34.0, -117.995, 0.003))
        col.add("way", 10, {"leisure": "pitch", "sport": "baseball"},
                rings=square(34.0, -118.001, 0.0003))
        col.add("way", 11, {"leisure": "pitch", "sport": "baseball"},
                rings=square(34.0, -117.994, 0.0003))
        col.parks = build_state.merge_park_polygons(col.parks)

        venues = build_state.group(col, keep_restricted=True)

        self.assertEqual(len(venues), 1, "one park is one pin")
        self.assertEqual(venues[0]["s"], {"baseball": 2})

    def test_two_parks_sharing_a_name_without_a_shared_id_stay_two(self):
        # Paxton Park: two multipolygons with the same name and no shared
        # identifier, 249 m apart at their nearest boundary. Nothing in OSM says
        # they are one place, so they are left as they are.
        a = build_state.polygon("relation", 16695368,
                                {"leisure": "park", "name": "Paxton Park"},
                                square(34.2688, -118.4381, 0.002))
        b = build_state.polygon("relation", 20107501,
                                {"leisure": "park", "name": "Paxton Park"},
                                square(34.2649, -118.4398, 0.002))

        self.assertEqual(len(build_state.merge_park_polygons([a, b])), 2)


class SportsVocabularyTest(unittest.TestCase):
    def test_softball_is_its_own_sport_and_is_never_rewritten_to_baseball(self):
        # The audit saw Debs come out as baseball 2 while OSM uses soccer. This
        # guards the vocabulary side of that: softball stays softball.
        self.assertIn("softball", build_state.SPORTS)
        self.assertIn("baseball", build_state.SPORTS)
        self.assertNotEqual(
            build_state.sports_of({"leisure": "pitch", "sport": "softball"}),
            {"baseball": 1},
        )


if __name__ == "__main__":
    unittest.main()

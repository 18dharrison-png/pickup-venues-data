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

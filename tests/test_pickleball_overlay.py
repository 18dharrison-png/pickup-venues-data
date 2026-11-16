"""Unit tests for the pickleball overlay. Small synthetic venues only.

No state file, no network, no OSM extract: every venue here is a dict built in
the test, shaped exactly like the rows build_state.py writes (`id`, `n`, `la`,
`lo`, `s`, and optionally `lt`, `sf`, `a`, `v`).
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import pickleball_overlay as overlay  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data", "pickleball", "california.json")


def venue(vid, name, lat, lon, sports, **extra):
    out = {"id": vid, "n": name, "la": lat, "lo": lon, "s": dict(sports)}
    out.update(extra)
    return out


def site(name, lat, lon, courts=4, **extra):
    return {"name": name, "lat": lat, "lon": lon, "courts": courts,
            "kind": extra.pop("kind", "hybrid"), "lit": extra.pop("lit", None),
            "address": extra.pop("address", None), "source": "test"}


# Santa Monica-ish coordinates; 0.001 degrees of latitude is about 111 m.
LAT, LON = 34.0200, -118.4900


class MergeTest(unittest.TestCase):
    def test_pickleball_is_added_to_the_park_it_belongs_to(self):
        venues = [venue("way/1", "Reed Park", LAT, LON, {"tennis": 4})]
        out, stats = overlay.apply(venues, [site("Reed Courts", LAT + 0.001, LON, 3)])

        self.assertEqual(len(out), 1, "a documented site must not add a second pin")
        self.assertEqual(out[0]["s"], {"tennis": 4, "pickleball": 3})
        self.assertEqual(stats["merged"], 1)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["merged_into"], ["Reed Park"])

    def test_the_racquet_venue_wins_over_a_closer_basketball_court(self):
        venues = [
            venue("way/basket", "Reed Park", LAT, LON, {"basketball": 2}),
            venue("way/tennis", "Reed Park tennis", LAT + 0.003, LON, {"tennis": 4}),
        ]
        out, stats = overlay.apply(venues, [site("Reed Courts", LAT + 0.0005, LON, 3)])

        by_id = {row["id"]: row for row in out}
        self.assertEqual(by_id["way/tennis"]["s"], {"tennis": 4, "pickleball": 3})
        self.assertEqual(by_id["way/basket"]["s"], {"basketball": 2})
        self.assertEqual(stats["merged_into"], ["Reed Park tennis"])

    def test_a_site_with_no_venue_nearby_becomes_its_own_venue(self):
        venues = [venue("way/1", "Some Other Park", LAT + 0.05, LON, {"tennis": 2})]
        out, stats = overlay.apply(venues, [site("Brand New Courts", LAT, LON, 8,
                                                 kind="dedicated", lit=True,
                                                 address="1 Main St")])

        new = [row for row in out if row["id"] == "pickleball/brand-new-courts"]
        self.assertEqual(len(new), 1)
        row = new[0]
        self.assertEqual(row["n"], "Brand New Courts")
        self.assertEqual(row["s"], {"pickleball": 8})
        self.assertEqual(row["v"], "known", "a city's own list is as good as a named site")
        self.assertTrue(row["lt"])
        self.assertEqual(row["a"], "1 Main St")
        self.assertEqual(row["la"], LAT)
        self.assertEqual(stats["added"], 1)
        self.assertEqual(stats["added_names"], ["Brand New Courts"])

    def test_osm_wins_when_it_has_an_explicit_pickleball_count(self):
        venues = [venue("way/1", "Balboa Sports Center", LAT, LON,
                        {"tennis": 16, "basketball": 2, "pickleball": 1})]
        before = json.dumps(venues, sort_keys=True)
        out, stats = overlay.apply(venues, [site("Balboa Courts", LAT, LON, 8)])

        self.assertEqual(json.dumps(out, sort_keys=True), before,
                         "a mapped pickleball court is never overwritten by a list")
        self.assertEqual(stats["osm"], 1)
        self.assertEqual(stats["merged"], 0)
        self.assertEqual(stats["osm_wins"], ["Balboa Sports Center"])

    def test_the_radius_is_a_hard_cut_off(self):
        venues = [venue("way/1", "Far Park", LAT + 0.01, LON, {"tennis": 2})]  # ~1.1 km
        out, stats = overlay.apply(venues, [site("Far Courts", LAT, LON, 4)])
        self.assertEqual(stats["merged"], 0)
        self.assertEqual(stats["added"], 1, "too far to be the same place, so it stands alone")

    def test_the_callers_list_is_not_mutated(self):
        venues = [venue("way/1", "Reed Park", LAT, LON, {"tennis": 4})]
        snapshot = json.dumps(venues, sort_keys=True)
        overlay.apply(venues, [site("Reed Courts", LAT, LON, 3)])
        self.assertEqual(json.dumps(venues, sort_keys=True), snapshot,
                         "venue dicts are copied, so a dry run and a build cannot interfere")

    def test_no_entries_is_a_no_op(self):
        venues = [venue("way/1", "Reed Park", LAT, LON, {"tennis": 4})]
        out, stats = overlay.apply(venues, [])
        self.assertEqual([row["id"] for row in out], ["way/1"])
        self.assertEqual(stats["merged"] + stats["added"] + stats["osm"], 0)

    def test_the_output_is_sorted_by_id(self):
        venues = [venue("way/9", "Zed Park", LAT, LON, {"tennis": 1}),
                  venue("way/2", "Abe Park", LAT + 0.02, LON, {"tennis": 1})]
        out, _ = overlay.apply(venues, [site("New Site", LAT + 0.1, LON, 2)])
        ids = [row["id"] for row in out]
        self.assertEqual(ids, sorted(ids))


class LoadTest(unittest.TestCase):
    def test_malformed_rows_are_dropped_and_the_good_ones_kept(self):
        import tempfile

        payload = {
            "source": "test list",
            "sites": [
                {"name": "Good", "lat": 34.0, "lon": -118.0, "courts": 2},
                {"name": "No courts", "lat": 34.0, "lon": -118.0},
                {"name": "Bad lat", "lat": "north", "lon": -118.0, "courts": 2},
                {"name": "Off the earth", "lat": 91, "lon": 0, "courts": 2},
                {"name": "Zero courts", "lat": 34.0, "lon": -118.0, "courts": 0},
            ],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            path = handle.name
        try:
            entries, source = overlay.load(path)
        finally:
            os.unlink(path)

        self.assertEqual([entry["name"] for entry in entries], ["Good"])
        self.assertEqual(source, "test list")
        self.assertEqual(entries[0]["kind"], "hybrid", "the default kind is the common case")

    def test_a_missing_file_is_empty_not_an_error(self):
        self.assertEqual(overlay.load("/nonexistent/file.json"), ([], None))

    def test_the_describe_line_says_what_happened(self):
        line = overlay.describe({"merged": 4, "added": 2, "osm": 1}, "laparks.org")
        self.assertIn("laparks.org", line)
        self.assertIn("4 merged", line)
        self.assertIn("2 new site(s)", line)
        self.assertIn("1 left to OSM", line)


class ShippedDataTest(unittest.TestCase):
    """The real data file, not a fixture: it must stay loadable and complete."""

    def setUp(self):
        self.entries, self.source = overlay.load(DATA)

    def test_the_documented_la_sites_are_all_there(self):
        self.assertEqual(len(self.entries), 9)
        names = {entry["name"] for entry in self.entries}
        self.assertIn("Peck Park Community Center", names)
        self.assertIn("Park Center, Griffith Park", names)
        self.assertEqual(sum(entry["courts"] for entry in self.entries), 48)
        self.assertIn("laparks.org", self.source or "")

    def test_every_site_says_where_it_came_from(self):
        for entry in self.entries:
            self.assertTrue(entry["source"], entry["name"])
            self.assertIn(entry["kind"], {"hybrid", "dedicated"}, entry["name"])

    def test_slugs_are_unique_so_two_sites_cannot_share_a_pin(self):
        slugs = [overlay.slug(entry["name"]) for entry in self.entries]
        self.assertEqual(len(slugs), len(set(slugs)), slugs)

    def test_the_corrected_coordinates_stay_corrected(self):
        # The list's street addresses geocode 1.5-4 km from the courts, which
        # put a pin on the wrong hillside (Park Center) or in another district
        # (Westchester, Peck).
        by_name = {entry["name"]: entry for entry in self.entries}
        self.assertAlmostEqual(by_name["Westchester Courts"]["lat"], 33.95823, places=5)
        self.assertAlmostEqual(by_name["Peck Park Community Center"]["lon"], -118.30417, places=5)
        # Park Center: the four tennis courts at way/1491866543, not the address.
        self.assertAlmostEqual(by_name["Park Center, Griffith Park"]["lat"], 34.133228, places=5)
        self.assertAlmostEqual(by_name["Park Center, Griffith Park"]["lon"], -118.282872, places=5)


if __name__ == "__main__":
    unittest.main()

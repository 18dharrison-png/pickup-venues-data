"""Tests for build_site.py. Small synthetic states only: no real state file.

The case that matters is the comparison baseline. The workflow leaves the state
published *before* a build in the same folder as before.ndjson.gz so the
before/after counts can be printed, and that file must never reach the map:
it sorts first, and build_site keeps the first record for each venue id, so it
would win over the build that just ran.
"""
import gzip
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import build_site  # noqa: E402


def venue(vid, name, lat, lon, sports):
    return {"id": vid, "n": name, "la": lat, "lo": lon, "s": dict(sports)}


def write_state(path, venues):
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for item in venues:
            handle.write(json.dumps(item) + "\n")


def run(states_dir, out_dir):
    argv = sys.argv
    sys.argv = ["build_site.py", states_dir, out_dir]
    try:
        build_site.main()
    finally:
        sys.argv = argv


def read_cells(out_dir):
    cell_dir = os.path.join(out_dir, "v1", "cells")
    venues = []
    for name in sorted(os.listdir(cell_dir)):
        with open(os.path.join(cell_dir, name), encoding="utf-8") as handle:
            venues.extend(json.load(handle)["venues"])
    return {item["id"]: item for item in venues}


class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.states = os.path.join(self.tmp.name, "states")
        self.out = os.path.join(self.tmp.name, "site")
        os.makedirs(self.states)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_baseline_file_never_outranks_the_build(self):
        write_state(os.path.join(self.states, "before.ndjson.gz"), [
            # Yesterday's record for a venue this build changed...
            venue("way/1", "Westwood Park", 34.0537, -118.4466, {"tennis": 8}),
            # ...and a pin only the old build had.
            venue("way/9", "Gone Park", 34.0600, -118.4000, {"tennis": 1}),
        ])
        write_state(os.path.join(self.states, "california.ndjson.gz"), [
            venue("way/1", "Westwood Park", 34.0537, -118.4466,
                  {"tennis": 8, "pickleball": 8}),
        ])

        run(self.states, self.out)

        cells = read_cells(self.out)
        self.assertEqual(cells["way/1"]["s"], {"tennis": 8, "pickleball": 8},
                         "the build that just ran must win over the baseline")
        self.assertNotIn("way/9", cells, "a baseline-only pin must not be published")

        with open(os.path.join(self.out, "v1", "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["states"], {"california": 1},
                         "the baseline is a comparison file, not a state")
        self.assertEqual(manifest["venueCount"], 1)

    def test_a_border_venue_is_still_kept_once(self):
        shared = venue("way/7", "Border Park", 42.0000, -124.2000, {"basketball": 1})
        write_state(os.path.join(self.states, "california.ndjson.gz"), [shared])
        write_state(os.path.join(self.states, "oregon.ndjson.gz"), [shared])

        run(self.states, self.out)

        cells = read_cells(self.out)
        self.assertEqual(len(cells), 1, "a park on a state border is one pin")
        with open(os.path.join(self.out, "v1", "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(sorted(manifest["states"]), ["california", "oregon"])


if __name__ == "__main__":
    unittest.main()

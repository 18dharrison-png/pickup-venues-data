#!/usr/bin/env python3
"""Decide which states a run builds. Prints a GitHub Actions output line.

  "california"            -> just California
  "texas, new-york"       -> those two
  "all" (or empty)        -> every state + DC

Names follow Geofabrik's file names (lowercase, hyphens), e.g. "new-york".
"""
import json
import sys

ALL = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "district-of-columbia", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "new-hampshire",
    "new-jersey", "new-mexico", "new-york", "north-carolina", "north-dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode-island",
    "south-carolina", "south-dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west-virginia", "wisconsin", "wyoming",
]


def pick(requested):
    text = (requested or "").strip().lower()
    if text in ("", "all"):
        return ALL
    chosen = []
    for part in text.split(","):
        name = part.strip().replace(" ", "-")
        if not name:
            continue
        if name not in ALL:
            sys.exit(f"Unknown state '{name}'. Use names like: california, new-york, district-of-columbia")
        if name not in chosen:
            chosen.append(name)
    if not chosen:
        sys.exit("No states chosen")
    return chosen


if __name__ == "__main__":
    print("states=" + json.dumps(pick(sys.argv[1] if len(sys.argv) > 1 else "")))

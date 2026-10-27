#!/usr/bin/env python3
"""Combine every state's venues into small map files for GitHub Pages.

Input:  a folder of <state>.ndjson.gz files from build_state.py
Output: a folder ready to publish:

  v1/manifest.json        what's inside, when it was built, which states
  v1/overview.json        1-degree squares with a venue count (whole-US view)
  v1/regions.json         0.25-degree squares with a venue count (city view)
  v1/cells/<y>_<x>.json   the venues in one 0.25-degree square (street view)

A 0.25-degree square is about 28 km tall. Its file name is
floor(lat * 4) _ floor(lng * 4), e.g. Santa Monica is cells/136_-474.json.

Usage: python3 build_site.py <states_dir> <out_dir>
"""
import glob
import gzip
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

CELLS_PER_DEGREE = 4


def compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    states_dir, out_dir = sys.argv[1], sys.argv[2]
    files = sorted(glob.glob(os.path.join(states_dir, "*.ndjson.gz")))
    if not files:
        sys.exit(f"no state files in {states_dir}")

    venues = {}
    per_state = {}
    for path in files:
        state = os.path.basename(path).removesuffix(".ndjson.gz")
        count = 0
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                v = json.loads(line)
                # A park on a state border appears in both extracts; keep one.
                venues.setdefault(v["id"], v)
                count += 1
        per_state[state] = count

    cells = defaultdict(list)
    overview = defaultdict(lambda: [0, 0.0, 0.0])
    for v in venues.values():
        cy = math.floor(v["la"] * CELLS_PER_DEGREE)
        cx = math.floor(v["lo"] * CELLS_PER_DEGREE)
        cells[(cy, cx)].append(v)
        o = overview[(math.floor(v["la"]), math.floor(v["lo"]))]
        o[0] += 1
        o[1] += v["la"]
        o[2] += v["lo"]

    v1 = os.path.join(out_dir, "v1")
    cell_dir = os.path.join(v1, "cells")
    os.makedirs(cell_dir, exist_ok=True)

    regions = []
    for (cy, cx), items in sorted(cells.items()):
        items.sort(key=lambda v: v["id"])
        with open(os.path.join(cell_dir, f"{cy}_{cx}.json"), "w", encoding="utf-8") as f:
            f.write(compact({"cell": f"{cy}_{cx}", "venues": items}))
        regions.append({
            "k": f"{cy}_{cx}",
            "c": len(items),
            "la": round(sum(v["la"] for v in items) / len(items), 4),
            "lo": round(sum(v["lo"] for v in items) / len(items), 4),
        })

    squares = [
        {"k": f"{y}_{x}", "c": c, "la": round(sla / c, 3), "lo": round(slo / c, 3)}
        for (y, x), (c, sla, slo) in sorted(overview.items())
    ]

    with open(os.path.join(v1, "regions.json"), "w", encoding="utf-8") as f:
        f.write(compact({"cellsPerDegree": CELLS_PER_DEGREE, "regions": regions}))
    with open(os.path.join(v1, "overview.json"), "w", encoding="utf-8") as f:
        f.write(compact({"squares": squares}))

    manifest = {
        "version": 1,
        "builtAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "venueCount": len(venues),
        "cellCount": len(cells),
        "cellsPerDegree": CELLS_PER_DEGREE,
        "states": per_state,
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "files": {
            "overview": "v1/overview.json",
            "regions": "v1/regions.json",
            "cell": "v1/cells/{y}_{x}.json",
        },
    }
    with open(os.path.join(v1, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(manifest, indent=2, ensure_ascii=False))

    # GitHub Pages: serve files as-is, and give the site a plain home page.
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(
            "<!doctype html><meta charset=utf-8><title>PickUp venue data</title>"
            "<body style='font-family:system-ui;max-width:40em;margin:3em auto'>"
            "<h1>PickUp venue data</h1>"
            f"<p>{len(venues):,} places to play across {len(per_state)} states, "
            f"built {manifest['builtAt']}.</p>"
            "<p>Start at <a href='v1/manifest.json'>v1/manifest.json</a>.</p>"
            "<p>Data © OpenStreetMap contributors, available under the "
            "<a href='https://www.openstreetmap.org/copyright'>Open Database License</a>.</p>"
        )

    print(f"{len(venues)} venues in {len(cells)} cells from {len(files)} states -> {out_dir}")


if __name__ == "__main__":
    main()

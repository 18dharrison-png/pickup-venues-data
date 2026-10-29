# pickup-venues-data

Nationwide sports venue map data for PickUp, built from OpenStreetMap.

Every place in this dataset is a court or field a stranger can turn up at and
play. A court is kept only when a **public** source proves the ground under it is
public; everything else (apartment and hotel courts, HOAs, clubs, private
schools, unnamed courts nobody has verified) is dropped.

## What a venue looks like

One gzipped NDJSON file per state, one venue per line:

```json
{"id":"way/24655760","n":"Cheviot Hills Recreation Center","la":34.045673,"lo":-118.409217,"s":{"tennis":4},"v":"park"}
```

| field | meaning |
|-------|---------|
| `id`  | OpenStreetMap id of the venue (the park, school or court) |
| `n`   | display name |
| `la` / `lo` | latitude / longitude |
| `s`   | sports at the venue, as `{"tennis": 4, "basketball": 2}` |
| `v`   | **why this place is public** — see below |
| `sf`  | commonest surface, when tagged |
| `lt`  | `true` if any court is lit, `false` if all are unlit, otherwise absent |
| `a`   | address, when tagged |

### The `v` field

`v` records which public source justified keeping the venue, so the app can label
it honestly instead of implying a guarantee:

| `v` | label in the app | rule |
|-----|------------------|------|
| `park` | "Public park" | inside a public-access protected-area polygon from PAD-US, owned or managed by a federal, state, local or regional agency |
| `school` | "Public high school - check public hours" | inside an OpenStreetMap school area that also contains a public high school point from NCES serving grades 9-12. Private schools are never in the NCES public list, so they cannot qualify |
| `known` | "Rec center" | a named public recreation or sports centre, a beach volleyball court, or a named court OpenStreetMap itself marks `access=yes`/`public` |

Existing rules still apply on top of this:

* gym and fitness-station style sports are hidden unless they are at a real
  running track (`leisure=track`), because an outdoor gym is not a place to play;
* courts tagged `access=private`, `no`, `members`, `customers`, `permit` or
  `students` are dropped — unless they sit on a public school campus, where the
  school's own hours decide (that is what the "check public hours" label is for);
* named private and commercial operations (country clubs, tennis clubs, HOAs,
  hotels, apartments) are dropped by name whatever else says otherwise.

A school's courts are grouped into one venue named after the school, the same way
a park's courts are grouped under the park, so a campus is one pin rather than
eight identical "Basketball courts" pins.

## How a state is built

`.github/workflows/build-venues.yml` builds one state per matrix job:

1. download the state extract from Geofabrik and filter it with `osmium
   tags-filter` down to parks, courts, fields, tracks, schools and clubs;
2. download the NCES public school locations (EDGE) and the NCES school
   directory (CCD), join them on `NCESSCH`, and keep schools serving grades 9-12
   (`scripts/prepare_nces.py` validates the file layout and the join rate);
3. page the PAD-US FeatureServer for the state's public-access polygons
   (`scripts/fetch_padus.py`, retries with backoff, resumes mid-state, cached);
4. decide each court with `scripts/public_filter.py` and write the venues with
   `scripts/build_public.py`, which also reports counts, every dropped court and
   ten kept/dropped examples;
5. for California, run `scripts/spot_checks.py`, which fails the job if a named
   real-world case changes — the unverified courts near Rancho Park must be gone,
   Reed Park / Penmar / Venice Beach / Pan Pacific must stay, Venice, Palisades
   Charter, Fairfax and Hamilton high schools must appear as `school`, and Loyola
   (private) must stay out.

Locally, with a filtered extract:

```sh
pip install "osmium>=4,<5" "shapely>=2,<3"
python scripts/fetch_padus.py --state california --out padus/california.geojson
python scripts/build_public.py filtered.osm.pbf out/california.ndjson.gz \
  --padus-geojson padus/california.geojson \
  --schools nces/ccd_sch.zip --school-locations nces/edge_public.csv \
  --baseline --report out/california.report.json \
  --dropped out/california.dropped.jsonl --sample out/california.samples.txt
python scripts/spot_checks.py --venues out/california.ndjson.gz \
  --dropped out/california.dropped.jsonl --report out/california.report.json
```

## Data sources and credits

* **OpenStreetMap** — venue names, sports, geometry. © OpenStreetMap
  contributors, [ODbL](https://www.openstreetmap.org/copyright).
* **PAD-US** (Protected Areas Database of the United States), U.S. Geological
  Survey Gap Analysis Project — public-access land boundaries. Public domain.
  Annual release; accessed through the USGS PAD-US FeatureServer, state by state.
* **NCES EDGE Public School Locations** and the **NCES Common Core of Data
  school directory**, U.S. Department of Education, National Center for Education
  Statistics — which schools are public, and which serve grades 9-12. Public
  domain.

None of these sources guarantee that a court is open at a given hour. The labels
are evidence, not a promise, which is why the school label tells players to check
public hours.

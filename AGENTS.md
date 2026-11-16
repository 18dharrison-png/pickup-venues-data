# Working in the venue data repo

This repo builds the venue files the PickUp app maps: `scripts/` reads an OSM
extract, joins PAD-US public land and the NCES school directory, and writes
`states/<state>.ndjson.gz` plus a build report. GitHub Actions builds it; the
`data` branch is the published output and GitHub Pages serves the map from it.

Read this before touching anything. The rules below are the ones that have
already caused damage when ignored.

## The two branches

- **`main`** is the generator: `scripts/`, `data/` (inputs like the pickleball
  list), `tests/`, `.github/workflows/`.
- **`data`** is output only. The `publish` job builds it fresh each run and
  **force-pushes** it. Never commit to `data` by hand, never merge into it, and
  never treat it as a source of truth for code.

## Landing work

**A cloud chat cannot `git push`.** The GitHub connection here authorises API
calls, not git: there is no credential helper, no `~/.netrc`, no token, and
`git push` stops at `could not read Username for 'https://github.com'`. Land work
through the GitHub API (`GITHUB_COMMIT_MULTIPLE_FILES`), and **verify the bytes
you sent are the bytes that landed**:

- per file: `git hash-object <path>` against the `blob_sha` the API reports;
- for the change: `git rev-parse <commit>^{tree}` against `new_tree_sha`.

A matching commit message proves nothing. Keep one commit's file content under
about 50 KB — past that the API truncates silently, which produces a blob that
looks committed and is broken.

Work lands on a branch, never straight on `main`: push a named branch
(`la-fixes`, `pickleball-overlay`, …), let the dry run prove it, and only then
open a pull request or fast-forward `main`.

## Never publish without a dry run

`build-venues.yml` has a **`dry_run`** input. A dry run builds for real on
Actions (which can reach Geofabrik, the ArcGIS PAD-US service and NCES, where a
laptop often cannot), runs the spot checks, uploads
`state-<state>` (the `.ndjson.gz`, the report, the dropped courts, the spot-check
verdicts, the LA before/after counts and the OSM probe) as an artifact — and
**does not publish**: no `data` push, no Pages deploy.

The order is always: dry run → read the artifact → the owner approves → the
publish run. **Never force-push `data` without a dry run of the same commit.**
The `data` branch is force-pushed, so a wrong publish does not sit on top of the
old data, it replaces every state file for every state that run rebuilt.

## Verification

- `python3 -m unittest discover -s tests` must pass. The tests use small
  synthetic venues and rings on purpose: they prove the merge rules without an
  OSM extract, a PAD-US file or a network.
- `test_build_state.py` needs `osmium` (`pip install 'osmium>=4,<5'`);
  without it the module skips with a reason. **A skip is not a pass.**
- A build you did not run is not verified. Say "not built here" rather than
  implying the data is fine; the dry-run artifact is the evidence.
- `scripts/spot_checks.py` is the named real-world check (specific courts that
  must be gone, parks that must stay). It runs in the workflow; keep a case for
  every defect that was ever fixed.

## Data, not code

City-run facilities OSM cannot see belong in `data/<kind>/<state>.json`, merged
by an overlay module (`scripts/pickleball_overlay.py` is the first), never as a
special case in the builder. Every entry records its source and its coordinates;
OSM wins wherever OSM already has an explicit tag for the same thing.

## Never touch the live database

The app's Supabase project is not this repo's to write to. Read-only at most,
and nothing in the build needs it at all. "Test data" in the live project is
never allowed.

## Notes

If a change needs explaining, add it to the notes the app repo keeps
(`docs/notes/map-data.md`, `map-data-repo-prompt.md`) rather than starting a
parallel set here. Keep any notes file under 30 KB.

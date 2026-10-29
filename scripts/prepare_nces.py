#!/usr/bin/env python3
"""Turn the NCES downloads into the two files the builder needs, and check them.

Two NCES products are involved and neither is usable as-is:

  * EDGE "Public School Locations" (EDGE_GEOCODE_PUBLICSCH_<YY><YY>.zip) has the
    coordinates. The 2024-25 archive contains a **pipe-delimited .TXT with no
    header row** (plus an XLSX, a SAS file and a nested shapefile), so the columns
    have to be located by position. Getting that wrong is exactly the mistake
    that put every school at (0, 0) earlier, so the mapping below is validated
    against the data instead of trusted: every row must have 23 fields, a
    12-digit NCESSCH, a two-letter state and coordinates that parse.
  * the CCD school directory (ccd_sch_*.zip) has the grade span (GSLO/GSHI) and
    the open/closed status, but no coordinates.

They are joined on NCESSCH. This script writes one small CSV with a header, then
prints the join rate and fails when the two files don't line up — which catches
a year mismatch before a whole state is built from it.

  python3 scripts/prepare_nces.py --edge nces/edge.zip --ccd nces/ccd_sch.zip --out nces

Both sources are public domain (NCES, US Department of Education).
"""
import argparse
import csv
import io
import os
import sys
import zipfile

# EDGE 2024-25 .TXT layout, confirmed against real rows:
#  0 NCESSCH          1 LEAID            2 NAME             3 state FIPS
#  4 street           5 city             6 state            7 ZIP
#  8 county FIPS      9 county FIPS      10 county name     11 locale
# 12 latitude        13 longitude       14 CBSA            15 CBSA name
# 16 CBSA type       17 CSA              18 CSA name        19 CD
# 20 SLDL            21 SLDU             22 school year
EDGE_COLUMNS = [
    "NCESSCH", "LEAID", "NAME", "STFIP", "STREET", "CITY", "STATE", "ZIP",
    "CNTY", "CNTY2", "NMCNTY", "LOCALE", "LAT", "LON", "CBSA", "NMCBSA",
    "CBSATYPE", "CSA", "NMCSA", "CD", "SLDL", "SLDU", "SCHOOLYEAR",
]
KEEP = ("NCESSCH", "NAME", "STATE", "LAT", "LON", "SCHOOLYEAR")

# Every USPS state and territory code. Used to prove the state column really is
# a state column: a shifted layout puts a street or a county FIPS there instead.
USPS = set("""AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN
MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
PR VI GU AS MP""".split())

# Grades that make a school a high school (CCD encodes them as "09".."12").
HIGH_SCHOOL_GRADES = {"09", "10", "11", "12"}
MIN_EDGE_VALID = 0.99     # share of EDGE rows that must look like US schools
MIN_JOIN = 0.85           # share of open high schools that must have coordinates


def read_edge(path):
    """Return (rows, school_year, source) from an EDGE zip, CSV or TXT."""
    if path.endswith(".zip"):
        archive = zipfile.ZipFile(path)
        members = [n for n in archive.namelist()
                   if n.lower().endswith((".csv", ".txt"))]
        if not members:
            raise SystemExit(f"{path} has no CSV or TXT inside: {archive.namelist()}")
        raw = archive.read(members[0]).decode("utf-8-sig", errors="replace")
        source = f"{path}!{members[0]}"
    else:
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            raw = handle.read()
        source = path

    lines = [line for line in raw.splitlines() if line.strip()]
    delimiter = "|" if lines[0].count("|") > lines[0].count(",") else ","
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    table = list(reader)
    if not table:
        raise SystemExit(f"{source} is empty")

    header = table[0]
    if "NCESSCH" in [cell.strip().upper() for cell in header]:
        names = [cell.strip().upper() for cell in header]
        rows = [dict(zip(names, row)) for row in table[1:]]
    else:
        rows = []
        for row in table:
            if len(row) != len(EDGE_COLUMNS):
                raise SystemExit(
                    f"{source}: expected {len(EDGE_COLUMNS)} fields per row, found "
                    f"{len(row)} — the NCES layout changed, update EDGE_COLUMNS"
                )
            rows.append(dict(zip(EDGE_COLUMNS, row)))

    year = next((row.get("SCHOOLYEAR") for row in rows if row.get("SCHOOLYEAR")), "")
    print(f"{source}: {len(rows)} schools, {len(table[0])} columns, "
          f"school year {year or 'unknown'}")
    return rows, year, source


def validate(rows, source):
    """Prove the columns are where EDGE_COLUMNS says they are.

    The check is structural (ids, state codes, usable coordinates) rather than a
    mainland bounding box: Puerto Rico, Guam and American Samoa are real rows
    that no mainland box contains, and rejecting them would look like a layout
    bug. How many rows are in the 50 states + DC box is printed separately
    because the state builds only cover those.
    """
    good = 0
    mainland = 0
    for row in rows:
        ncessch = str(row.get("NCESSCH") or "").strip()
        state = str(row.get("STATE") or "").strip().upper()
        try:
            lat = float(row.get("LAT") or "")
            lon = float(row.get("LON") or "")
        except ValueError:
            continue
        if not (len(ncessch) == 12 and ncessch.isdigit() and state in USPS):
            continue
        if not (-180 <= lon <= 180 and -90 <= lat <= 90) or (lat == 0 and lon == 0):
            continue
        good += 1
        if 15 <= lat <= 72 and -180 <= lon <= -64:
            mainland += 1
    share = good / max(1, len(rows))
    print(f"  {good}/{len(rows)} rows are US public schools with usable coordinates "
          f"({share:.1%}); {mainland} of them in the 50 states + DC box")
    if share < MIN_EDGE_VALID:
        raise SystemExit(
            f"{source}: only {share:.1%} of rows validated; the column layout is "
            f"probably wrong (refusing to build schools from bad coordinates)"
        )


def ccd_high_schools(path):
    archive = zipfile.ZipFile(path)
    member = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
    with archive.open(member) as handle:
        reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", errors="replace"))
        for row in reader:
            if str(row.get("SY_STATUS", "1")).strip() not in ("", "1"):
                continue
            grades = {
                str(row.get("GSLO") or "").strip().upper().zfill(2),
                str(row.get("GSHI") or "").strip().upper().zfill(2),
            }
            if grades & HIGH_SCHOOL_GRADES:
                yield str(row.get("NCESSCH") or "").strip()


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edge", required=True, help="EDGE public school locations (zip/csv/txt)")
    parser.add_argument("--ccd", required=True, help="CCD school directory zip")
    parser.add_argument("--out", required=True, help="directory to write edge_public.csv into")
    args = parser.parse_args(argv)

    rows, year, source = read_edge(args.edge)
    validate(rows, source)

    os.makedirs(args.out, exist_ok=True)
    target = os.path.join(args.out, "edge_public.csv")
    with open(target, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(KEEP), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: (row.get(key) or "").strip() for key in KEEP})
    print(f"{target}: {len(rows)} rows")

    have = {str(row.get("NCESSCH") or "").strip() for row in rows}
    wanted = list(ccd_high_schools(args.ccd))
    joined = sum(1 for ncessch in wanted if ncessch in have)
    share = joined / max(1, len(wanted))
    print(f"CCD high schools with EDGE coordinates: {joined}/{len(wanted)} ({share:.1%})")
    if share < MIN_JOIN:
        raise SystemExit(
            f"only {share:.1%} of open CCD high schools have coordinates; the EDGE "
            f"and CCD years are too far apart — use matching school years"
        )
    print(f"ok: school year {year or 'unknown'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

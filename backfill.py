#!/usr/bin/env python3
"""One-time backfill: Meta Business Suite CSV exports → Airtable Snapshots.

Reads the Facebook CSV exports in ./csv (UTF-16, Hebrew headers, daily rows):

  Followers.csv  daily NEW followers  → reconstructed running total
  Visits.csv     daily profile visits → weekly sum, stored as Profile Views

Follower totals are reconstructed backwards from the CURRENT follower count
fetched live from the Graph API (the export only contains daily adds, not
totals, and does not include unfollows — so older totals are approximate).

Writes one Snapshots row per Monday per platform with Source=Backfill,
upserted on (Date, Platform) — safe to re-run.

    python3 backfill.py            # parse, reconstruct, write to Airtable
    python3 backfill.py --dry-run  # print what would be written
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys

# reuse the shared env/HTTP/Airtable helpers from the collector
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector import airtable_upsert, graph_get, load_env, log

CSV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csv")


def read_daily_csv(name):
    """Meta Business Suite export → {date: int}. Layout: 'sep=,' line,
    a title line, a header line, then '"YYYY-MM-DDT00:00:00","<n>"' rows."""
    path = os.path.join(CSV_DIR, name)
    lines = open(path, encoding="utf-16").read().splitlines()
    out = {}
    for row in csv.reader(lines[3:]):
        if len(row) == 2 and row[0][:4].isdigit():
            out[dt.date.fromisoformat(row[0][:10])] = int(row[1])
    if not out:
        raise SystemExit(f"ERROR: no data rows parsed from {name}")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print rows instead of writing to Airtable")
    args = parser.parse_args()

    load_env()

    daily_adds = read_daily_csv("Followers.csv")
    daily_visits = read_daily_csv("Visits.csv")
    first_day, last_day = min(daily_adds), max(daily_adds)
    log(f"Parsed {len(daily_adds)} days of data: {first_day} → {last_day}")

    log("Fetching current Facebook follower count (anchor for the totals)…")
    page = graph_get(os.environ["FB_PAGE_ID"], os.environ["META_ACCESS_TOKEN"],
                     fields="followers_count")
    anchor = page["followers_count"]
    log(f"  anchor: {anchor} followers as of today")

    # Walk backwards from today: total at end of day D = anchor - adds(D+1..last)
    totals = {}
    running = anchor
    for day in sorted(daily_adds, reverse=True):
        totals[day] = running
        running -= daily_adds[day]

    # One row per Monday: follower total at end of that Monday,
    # profile visits summed over the 7 days ending that Monday.
    rows = []
    day = first_day + dt.timedelta(days=(7 - first_day.weekday()) % 7)  # first Monday
    while day <= last_day:
        week = [day - dt.timedelta(days=i) for i in range(7)]
        rows.append({
            "Date": day.isoformat(),
            "Platform": "Facebook",
            "Followers": totals[day],
            "Profile Views": sum(daily_visits.get(d, 0) for d in week),
            "Source": "Backfill",
        })
        day += dt.timedelta(days=7)

    log(f"Built {len(rows)} weekly backfill rows "
        f"({rows[0]['Date']} → {rows[-1]['Date']}), "
        f"followers {rows[0]['Followers']} → {rows[-1]['Followers']}")

    if args.dry_run:
        log(json.dumps(rows[:5] + [{"…": "…"}] + rows[-2:], indent=2))
        return

    n = airtable_upsert("Snapshots", rows, ["Date", "Platform"])
    log(f"Done: upserted {n} Snapshots row(s) with Source=Backfill.")


if __name__ == "__main__":
    main()

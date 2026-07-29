#!/usr/bin/env python3
"""Guard rails. Run automatically after every collection.

This exists because the project has a few invariants that are invisible until
they break, and by then whoever broke them has moved on. Each check below
corresponds to a mistake that is easy to make and expensive to notice:

  1. The scoring rules live in TWO files, one JavaScript and one Python. If
     they drift, the summary page and the written takeaway quietly start
     disagreeing about the same week.
  2. docs/config.js is PUBLIC. Pasting the read-write Airtable token there
     would hand the world permission to edit the data.
  3. Airtable fields get renamed by hand in the UI; the collector then writes
     to a field that no longer exists and the value vanishes silently.

    python3 selfcheck.py           # all checks (needs .env / secrets)
    python3 selfcheck.py --offline # only the checks that need no network
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector import AIRTABLE, load_env, _http_json

HERE = os.path.dirname(os.path.abspath(__file__))
problems, notes = [], []


def fail(msg):
    problems.append(msg)


def ok(msg):
    notes.append(msg)


# 1 ─ the two rule files must agree ---------------------------------------
# name in shared.js -> name in takeaway.py
MIRRORED = {
    "TREND_THRESHOLD_PCT": "TREND_THRESHOLD_PCT",
    "POST_MATURITY_DAYS": "POST_MATURITY_DAYS",
    "MIN_POSTS_FOR_RATE": "MIN_POSTS_FOR_BLOCK",
    "BASELINE_PERIODS": "BASELINE_BLOCKS",
}


def check_constants():
    js = open(os.path.join(HERE, "docs", "shared.js")).read()
    py = open(os.path.join(HERE, "takeaway.py")).read()
    for jname, pname in MIRRORED.items():
        jm = re.search(rf"^const {jname}\s*=\s*([0-9.]+)", js, re.M)
        pm = re.search(rf"^{pname}\s*=\s*([0-9.]+)", py, re.M)
        if not jm or not pm:
            fail(f"constant {jname}/{pname} not found — the parity check can no "
                 "longer see it; update MIRRORED in selfcheck.py")
            continue
        if jm.group(1) != pm.group(1):
            fail(f"SCORING RULES DISAGREE: shared.js {jname}={jm.group(1)} but "
                 f"takeaway.py {pname}={pm.group(1)}. The summary page and the "
                 "weekly takeaway will contradict each other. Set them equal.")
    ok(f"scoring constants match across shared.js and takeaway.py "
       f"({len(MIRRORED)} checked)")


# 2 ─ the public config must not carry a writable token -------------------

def check_public_token():
    path = os.path.join(HERE, "docs", "config.js")
    cfg = open(path).read()
    m = re.search(r'AIRTABLE_TOKEN:\s*"([^"]+)"', cfg)
    if not m:
        fail("docs/config.js has no AIRTABLE_TOKEN — the dashboard cannot load")
        return
    public = m.group(1)
    if "XXXX" in public:
        fail("docs/config.js still holds the placeholder token — the dashboard "
             "will show 'not configured' to every visitor")
        return
    secret = os.environ.get("AIRTABLE_TOKEN")
    if secret and public == secret:
        fail("SECURITY: docs/config.js contains the same token as the "
             "AIRTABLE_TOKEN secret. That token can WRITE, and config.js is "
             "public. Replace it with a read-only token (data.records:read, "
             "this base only) — see the README security note.")
        return
    ok("public dashboard token is distinct from the collector's write token")


# 3 ─ Airtable still has the fields we write to ---------------------------

REQUIRED = {
    "Snapshots": ["Date", "Platform", "Followers", "Reach", "Impressions",
                  "Profile Views", "Source"],
    "Posts": ["Post ID", "Platform", "Published", "Type", "Permalink", "Caption",
              "Views", "Reach", "Likes", "Comments", "Shares", "Saves",
              "Last Synced"],
    "Takeaways": ["Week Of", "Weekly Takeaway", "Generated", "Window"],
}


def check_airtable_schema():
    base = os.environ.get("AIRTABLE_BASE_ID")
    token = os.environ.get("AIRTABLE_TOKEN")
    if not (base and token):
        ok("skipped Airtable schema check (no credentials in this environment)")
        return
    d = _http_json(f"{AIRTABLE.replace('/v0', '/v0/meta')}/bases/{base}/tables",
                   headers={"Authorization": f"Bearer {token}"})
    if "tables" not in d:
        # schema scope is optional; fall back to reading a row per table
        for table in REQUIRED:
            r = _http_json(
                f"{AIRTABLE}/{base}/{urllib.parse.quote(table)}?maxRecords=1",
                headers={"Authorization": f"Bearer {token}"})
            if "error" in r:
                fail(f"Airtable table '{table}' is unreadable: {r['error']}")
        ok("Airtable tables reachable (field-level check needs the schema scope)")
        return
    present = {t["name"]: {f["name"] for f in t["fields"]} for t in d["tables"]}
    for table, fields in REQUIRED.items():
        if table not in present:
            fail(f"Airtable table '{table}' is missing — the collector cannot "
                 "write to it")
            continue
        gone = [f for f in fields if f not in present[table]]
        if gone:
            fail(f"Airtable table '{table}' is missing field(s) {gone}. Anything "
                 "written to them is being silently discarded. Re-create them "
                 "with exactly these names.")
    ok(f"Airtable schema has every field the scripts write ({len(REQUIRED)} tables)")


# 4 ─ the Meta token deadline ---------------------------------------------
# The single most likely way this project dies: the Meta permission lapses and
# nobody notices, because a warning in a log nobody reads is not a warning.
# GitHub emails collaborators when a run FAILS, so once the deadline is close
# this fails the run on purpose — after the day's data is already saved.

def check_meta_deadline():
    from collector import check_meta_token, TOKEN_FAIL_DAYS
    token = os.environ.get("META_ACCESS_TOKEN")
    if not (token and os.environ.get("META_APP_ID") and os.environ.get("META_APP_SECRET")):
        ok("skipped Meta token deadline check (needs META_APP_ID/SECRET)")
        return
    info = check_meta_token(token)
    if not info:
        ok("Meta token has no expiry and no data-access deadline")
        return
    if info["days_left"] <= TOKEN_FAIL_DAYS:
        fail(f"META TOKEN DEADLINE — {info['reason']} on {info['date']}, in "
             f"{info['days_left']} day(s). Today's data was collected and saved "
             "normally; this run is failed on purpose so somebody sees this. "
             "Fix: follow 'Renewing the Meta token' in the README, put the new "
             "token in the META_ACCESS_TOKEN repository secret, re-run the "
             "workflow. ~10 minutes, no knowledge of this project required.")
    else:
        ok(f"Meta token good until {info['date']} ({info['days_left']} days)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true",
                    help="skip checks that need network or credentials")
    args = ap.parse_args()

    if not args.offline:
        load_env()

    check_constants()
    check_public_token()
    if not args.offline:
        check_airtable_schema()
        check_meta_deadline()

    for n in notes:
        print(f"  OK   {n}", flush=True)
    for p in problems:
        print(f"  FAIL {p}", flush=True)

    if problems:
        head = "::error::" if os.environ.get("GITHUB_ACTIONS") else "ERROR: "
        print(f"\n{head}{len(problems)} self-check problem(s) found — see above.",
              file=sys.stderr, flush=True)
        sys.exit(1)
    print(f"\nAll {len(notes)} self-checks passed.", flush=True)


if __name__ == "__main__":
    main()

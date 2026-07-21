#!/usr/bin/env python3
"""One-time Instagram history backfill, straight from the Graph API.

Unlike Facebook (whose history came from CSV exports), the IG insights API
answers historical queries, so this pulls everything it can:

  - weekly Snapshots rows (Mondays, matching the Facebook backfill): trailing
    28-day Reach / Views→Impressions / Profile Views per week, back to
    --start (default 2024-01-01). Metrics Meta has no data for in a given
    week come back as 0 and are stored as blank rather than a fake zero.
  - Followers for roughly the last month only — Meta's follower_count metric
    refuses to look back further than 30 days, so earlier weeks stay blank.
    Totals are reconstructed backwards from the current live count.
  - the FULL post archive with per-post insights (older media that predate
    business-account conversion may have no insights; those keep likes and
    comments from the media fields and leave the rest blank).

Upserts on (Date, Platform) / Post ID — safe to re-run.

    python3 backfill_instagram.py            # write to Airtable
    python3 backfill_instagram.py --dry-run  # print what would be written
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector import (GraphError, airtable_upsert, graph_get, ig_media_type,
                       load_env, log)


def week_snapshot(ig_id, token, monday):
    """Trailing-28-day totals for the week ending on `monday`."""
    until = int(dt.datetime.combine(monday, dt.time(23, 59)).timestamp())
    since = until - 27 * 86400
    d = graph_get(f"{ig_id}/insights", token,
                  metric="reach,views,profile_views", period="day",
                  metric_type="total_value", since=since, until=until)
    vals = {e["name"]: (e.get("total_value") or {}).get("value")
            for e in d.get("data") or []}
    return {
        "Date": monday.isoformat(),
        "Platform": "Instagram",
        # 0 here almost always means "Meta has no data for this era"
        # (e.g. the views metric didn't exist yet) — store blank, not 0
        "Reach": vals.get("reach") or None,
        "Impressions": vals.get("views") or None,
        "Profile Views": vals.get("profile_views") or None,
        "Source": "Backfill",
    }


def recent_follower_totals(ig_id, token):
    """{date: total} for the last ~4 weeks, anchored to the live count."""
    acct = graph_get(ig_id, token, fields="followers_count")
    anchor = acct["followers_count"]
    now = int(time.time())
    d = graph_get(f"{ig_id}/insights", token, metric="follower_count",
                  period="day", since=now - 29 * 86400, until=now)
    daily = {}
    for v in (d.get("data") or [{}])[0].get("values") or []:
        daily[dt.date.fromisoformat(v["end_time"][:10])] = v.get("value") or 0
    totals, running = {}, anchor
    for day in sorted(daily, reverse=True):
        totals[day] = running
        running -= daily[day]
    return totals


def all_media_posts(ig_id, token):
    posts, params = [], {
        "fields": "id,caption,media_type,media_product_type,permalink,"
                  "timestamp,like_count,comments_count",
        "limit": 100,
    }
    d = graph_get(f"{ig_id}/media", token, **params)
    media = list(d.get("data") or [])
    while (d.get("paging") or {}).get("next"):
        import urllib.request
        d = json.load(urllib.request.urlopen(d["paging"]["next"]))
        media.extend(d.get("data") or [])
    log(f"  {len(media)} media items total")

    for i, m in enumerate(media, 1):
        metrics = {}
        try:
            ins = graph_get(f"{m['id']}/insights", token,
                            metric="reach,shares,saved")
            metrics = {e["name"]: (e.get("values") or [{}])[-1].get("value")
                       for e in ins.get("data") or []}
        except GraphError as e:
            log(f"  [{i}/{len(media)}] no insights for {m['id']} "
                f"({e.message[:60]}) — keeping likes/comments only")
        posts.append({
            "Post ID": m["id"],
            "Platform": "Instagram",
            "Published": (m.get("timestamp") or "")[:10],
            "Type": ig_media_type(m),
            "Permalink": m.get("permalink"),
            "Caption": (m.get("caption") or "")[:100],
            "Reach": metrics.get("reach"),
            "Likes": m.get("like_count"),
            "Comments": m.get("comments_count"),
            "Shares": metrics.get("shares"),
            "Saves": metrics.get("saved"),
            "Last Synced": dt.date.today().isoformat(),
        })
        if i % 50 == 0:
            log(f"  [{i}/{len(media)}] media insights fetched…")
        time.sleep(0.15)  # stay well inside IG API rate limits
    return posts


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default="2024-01-01",
                        help="earliest Monday to backfill (default 2024-01-01, "
                             "matching the Facebook CSV backfill)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_env()
    token = os.environ["META_ACCESS_TOKEN"]
    ig_id = os.environ["IG_BUSINESS_ACCOUNT_ID"]

    start = dt.date.fromisoformat(args.start)
    monday = start + dt.timedelta(days=(7 - start.weekday()) % 7)
    last_monday = dt.date.today() - dt.timedelta(days=dt.date.today().weekday() or 7)

    log(f"Backfilling weekly IG snapshots {monday} → {last_monday}…")
    rows = []
    while monday <= last_monday:
        try:
            rows.append(week_snapshot(ig_id, token, monday))
        except GraphError as e:
            log(f"  {monday}: skipped ({e.message[:80]})")
        monday += dt.timedelta(days=7)
        time.sleep(0.3)
    with_reach = sum(1 for r in rows if r["Reach"])
    log(f"  built {len(rows)} weekly rows ({with_reach} with reach data)")

    log("Reconstructing follower totals for the last ~30 days…")
    totals = recent_follower_totals(ig_id, token)
    filled = 0
    for r in rows:
        day = dt.date.fromisoformat(r["Date"])
        if day in totals:
            r["Followers"] = totals[day]
            filled += 1
    log(f"  follower totals attached to {filled} recent row(s) "
        "(Meta only exposes the last 30 days)")

    log("Fetching full post archive…")
    posts = all_media_posts(ig_id, token)

    if args.dry_run:
        log(json.dumps({"first_rows": rows[:3], "last_rows": rows[-3:],
                        "oldest_post": posts[-1] if posts else None,
                        "n_posts": len(posts)}, indent=2, ensure_ascii=False))
        return

    n1 = airtable_upsert("Snapshots", rows, ["Date", "Platform"])
    n2 = airtable_upsert("Posts", posts, ["Post ID"])
    log(f"Done: upserted {n1} snapshot row(s) and {n2} post row(s).")


if __name__ == "__main__":
    main()

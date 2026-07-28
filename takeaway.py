#!/usr/bin/env python3
"""Rule-based weekly takeaway generator — plain English, no AI, no API cost.

Reads the metrics the collector already wrote to Airtable and composes a
2–4 sentence summary of the week, then upserts it to the Takeaways table.
Runs as the final step of the GitHub Action, after the collector.

    python3 takeaway.py             # generate and write
    python3 takeaway.py --dry-run   # print, write nothing

The takeaway is built from up to four template slots; a slot only appears
when its trigger fires, so the text stays meaningful instead of turning into
the same four sentences every week. A one-sentence takeaway is valid output.

Comparison window: the trailing 7 days vs the 7 days before that. (Not
"calendar week so far vs last full week" — that would show a fake drop every
Monday.) The row is keyed on the current week's Monday and refreshed on each
daily run.
"""

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector import AIRTABLE, load_env, log, _http_json

# --------------------------------------------------------------- tunables
# Staff can adjust these without touching any logic below.
TREND_THRESHOLD_PCT = 10          # % change that counts as up/down vs flat
STANDOUT_MULTIPLIER = 1.5         # best post must beat the average by this
DIP_THRESHOLD_MULTIPLIER = 0.7    # engagement rate below this × last week = dip
FOLLOWER_MILESTONE_STEP = 100     # celebrate crossing each multiple of this

PLATFORMS = ["Facebook", "Instagram"]
WINDOW_DAYS = 7
BASELINE_WEEKS = 8                # history used for "unusually fast" follower moves
# A post keeps gaining for days after it goes out (median engagement rate on
# this account roughly doubles between day 2 and day 7). Comparing fresh posts
# against matured ones invents a collapse every week, so posts younger than
# this are left out of week-on-week engagement comparisons on both sides.
POST_MATURITY_DAYS = 3
MIN_POSTS_FOR_BLOCK = 2           # floor for the per-period post sample size
BASELINE_BLOCKS = 8               # earlier blocks the baseline is taken from


# ------------------------------------------------------------ Airtable IO

def fetch_all(table, fields=None):
    token = os.environ["AIRTABLE_TOKEN"]
    base = os.environ["AIRTABLE_BASE_ID"]
    rows, offset = [], None
    while True:
        url = f"{AIRTABLE}/{base}/{urllib.parse.quote(table)}?pageSize=100"
        for f in fields or []:
            url += "&fields%5B%5D=" + urllib.parse.quote(f)
        if offset:
            url += "&offset=" + offset
        d = _http_json(url, headers={"Authorization": f"Bearer {token}"})
        if "error" in d:
            raise RuntimeError(f"Airtable read failed on '{table}': {d['error']}")
        rows += [r["fields"] for r in d.get("records", [])]
        offset = d.get("offset")
        if not offset:
            return rows


def merge_snapshots(rows):
    """One row per (Date, Platform); a real API reading beats a backfilled one."""
    best = {}
    for r in rows:
        if not r.get("Date") or not r.get("Platform"):
            continue
        k = (r["Date"], r["Platform"])
        prev = best.get(k)
        if not prev or (prev.get("Source") == "Backfill" and r.get("Source") == "API"):
            best[k] = r
    return sorted(best.values(), key=lambda r: r["Date"])


# --------------------------------------------------------------- helpers

def pct_change(cur, prev):
    """Percent change, or None when it cannot be expressed (no/zero baseline)."""
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / prev * 100


def engagement(p):
    return ((p.get("Likes") or 0) + (p.get("Comments") or 0)
            + (p.get("Shares") or 0) + (p.get("Saves") or 0))


def typical_post_count(posts, plat, days, today):
    """How many posts a typical `days`-long period holds for this platform.

    Mirrors typicalPostCount() in docs/shared.js — engagement is sampled in
    POSTS, not days, because this account posts irregularly and a fixed
    calendar window often contains nothing finished enough to judge.
    """
    counts = []
    for i in range(max(1, 365 // days)):
        end = today - dt.timedelta(days=days * i)
        start = end - dt.timedelta(days=days - 1)
        counts.append(sum(1 for p in posts
                          if p.get("Platform") == plat and p.get("Published")
                          and start.isoformat() <= p["Published"] <= end.isoformat()))
    return max(MIN_POSTS_FOR_BLOCK, round(statistics.median(counts) or 0))


def post_blocks(posts, plat, n, today):
    """Finished posts for `plat`, newest first, chunked into blocks of `n`."""
    matured = sorted(
        (p for p in posts if p.get("Platform") == plat and is_matured(p, today)),
        key=lambda p: p["Published"], reverse=True)
    return [matured[i * n:(i + 1) * n]
            for i in range(len(matured) // n)][:BASELINE_BLOCKS + 1]


def block_stat(block, fn):
    vals = [v for v in (fn(p) for p in block) if v is not None]
    return statistics.mean(vals) if vals else None


def is_matured(p, today):
    """Has this post had long enough to finish accumulating engagement?"""
    published = p.get("Published")
    if not published:
        return False
    return (today - dt.date.fromisoformat(published)).days >= POST_MATURITY_DAYS


def eng_rate(p):
    """Engagement ÷ reach. None when reach is missing — Meta killed Facebook
    post reach, so FB posts have no rate and are excluded from rate maths."""
    reach = p.get("Reach")
    if not reach:
        return None
    return engagement(p) / reach


def snapshot_on_or_before(snaps, platform, date_iso, field):
    """Latest non-null `field` for `platform` at or before `date_iso`."""
    hits = [s for s in snaps
            if s["Platform"] == platform and s["Date"] <= date_iso
            and s.get(field) is not None]
    return hits[-1].get(field) if hits else None


def fmt_pct(v):
    return f"{abs(v):.0f}%"


def crossed_milestone(before, after, step):
    """Highest multiple of `step` crossed upward between two counts."""
    if before is None or after is None or after <= before:
        return None
    m = (after // step) * step
    return int(m) if m > before else None


# ----------------------------------------------------------------- slots

def slot_trend(ctx):
    """Slot 1 — overall trend. Always attempted; one sentence per platform
    when the platforms diverge, otherwise a single combined sentence."""
    parts = []
    for plat in PLATFORMS:
        c = ctx[plat]
        delta = c["reach_delta_pct"]
        if delta is None:
            # No reach signal (Facebook, or not enough history) — fall back to
            # the engagement total so the platform still gets a sentence.
            if c["first_run"] and c["engagement"]:
                parts.append((plat, None,
                    f"{plat} drew {c['engagement']:,} interactions across "
                    f"{c['post_count']} post{'s' if c['post_count'] != 1 else ''} this week."))
            elif c["eng_delta_pct"] is not None:
                d = c["eng_delta_pct"]
                over = f"over its last {c['sample_n']} posts"
                if d > TREND_THRESHOLD_PCT:
                    parts.append((plat, d, f"{plat} engagement grew {fmt_pct(d)} {over}."))
                elif d < -TREND_THRESHOLD_PCT:
                    parts.append((plat, d, f"{plat} engagement dropped {fmt_pct(d)} {over}."))
                else:
                    parts.append((plat, d, f"{plat} engagement held steady {over} ({d:+.0f}%)."))
            continue
        if delta > TREND_THRESHOLD_PCT:
            parts.append((plat, delta, f"{plat} reach grew {fmt_pct(delta)} this week."))
        elif delta < -TREND_THRESHOLD_PCT:
            parts.append((plat, delta, f"{plat} reach dropped {fmt_pct(delta)} this week."))
        else:
            parts.append((plat, delta, f"{plat} reach held steady this week ({delta:+.0f}%)."))

    if not parts:
        return []
    # Combine only when both platforms tell the same story with the same metric
    if len(parts) == 2:
        (p1, d1, s1), (p2, d2, s2) = parts
        same_metric = ("reach" in s1) == ("reach" in s2)
        if (d1 is not None and d2 is not None and same_metric
                and (d1 > 0) == (d2 > 0)
                and abs(d1 - d2) <= TREND_THRESHOLD_PCT):
            metric = "reach" if "reach" in s1 else "engagement"
            avg = (d1 + d2) / 2
            if abs(avg) <= TREND_THRESHOLD_PCT:
                return [f"Facebook and Instagram {metric} both held steady this week "
                        f"({avg:+.0f}%)."]
            verb = "grew" if avg > 0 else "dropped"
            return [f"Facebook and Instagram {metric} both {verb} about "
                    f"{fmt_pct(avg)} this week."]
    return [s for _, _, s in parts]


def slot_best_post(ctx):
    """Slot 2 — a genuine standout only. Needs engagement rates, so this is
    Instagram-only in practice (Facebook post reach is gone)."""
    # Same pool the engagement figure uses — the most recent finished posts —
    # so the "standout" is always drawn from posts that have actually settled.
    rated = [(p, r) for p, r in
             ((p, eng_rate(p)) for p in ctx["sample_posts"]) if r]
    if len(rated) < 2:
        return []
    rates = [r for _, r in rated]
    avg = statistics.mean(rates)
    if not avg:
        return []
    best, best_rate = max(rated, key=lambda pr: pr[1])
    if best_rate < STANDOUT_MULTIPLIER * avg:
        return []
    day = dt.date.fromisoformat(best["Published"]).strftime("%A")
    kind = (best.get("Type") or "post").lower()
    return [f"{day}'s {kind} was the standout, pulling "
            f"{best_rate / avg:.1f}x average engagement."]


def slot_gap_or_dip(ctx):
    """Slot 3 — silence or a softening audience response."""
    out = []
    for plat in PLATFORMS:
        c = ctx[plat]
        if not c["has_history"]:
            continue
        # Only claim silence where we actually have post coverage. The Posts
        # table holds Instagram's full archive but only a recent window of
        # Facebook, so "no posts" outside that window would report a gap in
        # our data as a fact about the account.
        if c["post_count"] == 0 and c["posts_covered"]:
            out.append(f"No posts went out on {plat} this week.")
        elif (c["eng_rate"] is not None and c["prev_eng_rate"]
              and c["eng_rate"] < DIP_THRESHOLD_MULTIPLIER * c["prev_eng_rate"]):
            out.append(f"{plat} engagement rate fell despite steady reach — "
                       "worth checking content mix.")
    return out


def slot_too_fresh(ctx):
    """Note posts still accumulating, in the same words the summary page uses.

    Engagement itself is always judged (over the last N finished posts), so
    this never claims otherwise — it only says which posts aren't in the
    figure yet.
    """
    fresh = sum(ctx[p].get("fresh_posts", 0) for p in PLATFORMS)
    if not fresh:
        return []
    return [f"{fresh} post{'s' if fresh > 1 else ''} from the last "
            f"{POST_MATURITY_DAYS} days {'are' if fresh > 1 else 'is'} still "
            "gaining and not counted yet."]


def slot_milestone(ctx):
    """Slot 4 — a round-number crossing, or unusually fast movement."""
    out = []
    for plat in PLATFORMS:
        c = ctx[plat]
        m = crossed_milestone(c["prev_followers"], c["followers"],
                              FOLLOWER_MILESTONE_STEP)
        if m:
            out.append(f"{plat} crossed {m:,} followers this week.")
            continue
        delta, typical = c["follower_delta"], c["typical_follower_delta"]
        if delta is not None and typical and abs(delta) > 2 * typical:
            out.append(f"{plat} follower count moved unusually fast this week "
                       f"({delta:+,}).")
    return out


# --------------------------------------------------------------- assembly

def build_context(snaps, posts, today):
    """Everything the slots need, per platform plus shared post lists."""
    win_start = (today - dt.timedelta(days=WINDOW_DAYS - 1)).isoformat()
    prev_end = (today - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    prev_start = (today - dt.timedelta(days=2 * WINDOW_DAYS - 1)).isoformat()
    today_iso = today.isoformat()

    cur_posts = [p for p in posts
                 if p.get("Published") and win_start <= p["Published"] <= today_iso]
    prev_posts = [p for p in posts
                  if p.get("Published") and prev_start <= p["Published"] <= prev_end]

    ctx = {"all_posts": cur_posts,
           "matured_posts": [p for p in cur_posts if is_matured(p, today)],
           "sample_posts": [],   # filled per-platform below (most recent finished block)
           "window": f"{win_start} to {today_iso} vs {prev_start} to {prev_end}"}

    for plat in PLATFORMS:
        pc = [p for p in cur_posts if p.get("Platform") == plat]
        pp = [p for p in prev_posts if p.get("Platform") == plat]
        plat_snaps = [s for s in snaps if s["Platform"] == plat]

        # Earliest post we hold for this platform — anything before it is a
        # gap in collection, not a quiet week.
        plat_dates = sorted(p["Published"] for p in posts
                            if p.get("Platform") == plat and p.get("Published"))
        posts_covered = bool(plat_dates) and win_start >= plat_dates[0]

        reach_now = snapshot_on_or_before(snaps, plat, today_iso, "Reach")
        reach_prev = snapshot_on_or_before(snaps, plat, prev_end, "Reach")
        followers = snapshot_on_or_before(snaps, plat, today_iso, "Followers")
        prev_followers = snapshot_on_or_before(snaps, plat, prev_end, "Followers")

        # Engagement is compared over the last N finished posts against the N
        # before them — the same sampling the summary page uses (shared.js), so
        # the two never contradict each other. Calendar weeks contain nothing
        # judgeable about half the time on this account.
        pc_mature = [p for p in pc if is_matured(p, today)]
        eng_now = sum(engagement(p) for p in pc)          # headline: reported as-is
        eng_prev = sum(engagement(p) for p in pp)

        n_sample = typical_post_count(posts, plat, WINDOW_DAYS, today)
        blocks = post_blocks(posts, plat, n_sample, today)
        # rate where reach exists (Instagram); mean interactions per post
        # otherwise (Facebook has no post-level reach since Meta removed it)
        stat = eng_rate if any(eng_rate(p) for b in blocks for p in b) else engagement
        block_vals = [v for v in (block_stat(b, stat) for b in blocks) if v is not None]
        cur_block = block_vals[0] if block_vals else None
        base_block = (statistics.median(block_vals[1:]) if len(block_vals) >= 2 else None)
        sample_dates = sorted(p["Published"] for p in blocks[0]) if blocks else []
        if blocks:
            ctx["sample_posts"].extend(blocks[0])

        # typical weekly follower movement over the trailing baseline
        weekly_deltas = []
        for w in range(BASELINE_WEEKS):
            a = (today - dt.timedelta(days=7 * (w + 1))).isoformat()
            b = (today - dt.timedelta(days=7 * w)).isoformat()
            fa = snapshot_on_or_before(snaps, plat, a, "Followers")
            fb_ = snapshot_on_or_before(snaps, plat, b, "Followers")
            if fa is not None and fb_ is not None:
                weekly_deltas.append(abs(fb_ - fa))

        ctx[plat] = {
            "post_count": len(pc),
            "engagement": eng_now,
            "reach": reach_now,
            "followers": followers,
            "prev_followers": prev_followers,
            "reach_delta_pct": pct_change(reach_now, reach_prev),
            # last N finished posts vs the median of earlier blocks of N
            "eng_delta_pct": pct_change(cur_block, base_block),
            "fresh_posts": len(pc) - len(pc_mature),
            "sample_n": n_sample if blocks else 0,
            "sample_from": sample_dates[0] if sample_dates else None,
            "sample_to": sample_dates[-1] if sample_dates else None,
            "eng_rate": cur_block,
            "prev_eng_rate": base_block,
            "follower_delta": (followers - prev_followers)
                              if (followers is not None and prev_followers is not None) else None,
            "typical_follower_delta": (statistics.mean(weekly_deltas)
                                       if len(weekly_deltas) >= 2 else None),
            # first_run: we have current data but nothing to compare against
            "has_history": bool([s for s in plat_snaps if s["Date"] <= prev_end]),
            "first_run": not [s for s in plat_snaps if s["Date"] <= prev_end],
            "posts_covered": posts_covered,
        }
    return ctx


def generate(snaps, posts, today=None):
    today = today or dt.date.today()
    ctx = build_context(snaps, posts, today)
    sentences = (slot_trend(ctx) + slot_best_post(ctx)
                 + slot_gap_or_dip(ctx) + slot_milestone(ctx)
                 + slot_too_fresh(ctx))
    if not sentences:
        sentences = ["Not enough data yet to summarise this week."]
    return " ".join(sentences), ctx["window"]


def monday_of(d):
    return d - dt.timedelta(days=d.weekday())


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the takeaway without writing to Airtable")
    ap.add_argument("--date", help="pretend today is this ISO date (for testing)")
    args = ap.parse_args()

    load_env()
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    snaps = merge_snapshots(fetch_all("Snapshots"))
    posts = fetch_all("Posts")
    text, window = generate(snaps, posts, today)

    log(f"Week of {monday_of(today)} ({window})")
    log(f"  {text}")

    if args.dry_run:
        log("--dry-run: not writing to Airtable.")
        return

    record = {
        "Week Of": monday_of(today).isoformat(),
        "Weekly Takeaway": text,
        "Generated": today.isoformat(),
        "Window": window,
    }
    token = os.environ["AIRTABLE_TOKEN"]
    base = os.environ["AIRTABLE_BASE_ID"]
    d = _http_json(
        f"{AIRTABLE}/{base}/{urllib.parse.quote('Takeaways')}",
        method="PATCH",
        payload={"performUpsert": {"fieldsToMergeOn": ["Week Of"]},
                 "typecast": True, "records": [{"fields": record}]},
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    if "error" in d:
        raise RuntimeError(f"Airtable write failed: {d['error']}")
    log("Takeaway written to Airtable.")


if __name__ == "__main__":
    main()

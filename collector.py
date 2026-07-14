#!/usr/bin/env python3
"""Mamram Alumni Association — social metrics collector.

Pulls current metrics for one Facebook Page and one Instagram Business
account from the Meta Graph API and upserts snapshot + post rows into
Airtable. Designed to run weekly via GitHub Actions or manually:

    python3 collector.py            # normal run
    python3 collector.py --days 60  # widen the post lookback window

Idempotent: Snapshots upsert on (Date, Platform), Posts upsert on Post ID,
so re-running on the same day never duplicates rows.

Graceful degradation: a metric that Meta has deprecated (they do this
often) is logged and skipped; one failed platform does not kill the run.
An invalid/expired Meta token DOES fail the run loudly on purpose — see
README for how to renew it.

No dependencies outside the Python 3 standard library.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v23.0"
AIRTABLE = "https://api.airtable.com/v0"
SNAPSHOTS_TABLE = "Snapshots"
POSTS_TABLE = "Posts"
TOKEN_WARN_DAYS = 14  # warn when the Meta token expires within this many days

REQUIRED_ENV = [
    "META_ACCESS_TOKEN",
    "FB_PAGE_ID",
    "IG_BUSINESS_ACCOUNT_ID",
    "AIRTABLE_TOKEN",
    "AIRTABLE_BASE_ID",
]


def log(msg):
    print(msg, flush=True)


def warn(msg):
    # "::warning::" makes the message stand out in GitHub Actions logs too
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"WARNING: {msg}",
          flush=True)


def die(msg):
    print(f"::error::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"ERROR: {msg}",
          file=sys.stderr, flush=True)
    sys.exit(1)


def load_env():
    """Read a local .env if present (values already in the environment win)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v.strip().strip('"').strip("'"))
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        die(f"Missing required environment variables: {', '.join(missing)}. "
            "Set them in .env (local) or repository secrets (GitHub Actions).")


class GraphError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"Graph API error {code}: {message}")


def _http_json(url, method="GET", payload=None, headers=None, retries=2):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                if attempt < retries and e.code >= 500:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}: {body[:300]}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Network error calling {url.split('?')[0]}: {e}")


def graph_get(path, token, **params):
    """GET a Graph API path. Raises GraphError on API errors."""
    params["access_token"] = token
    d = _http_json(f"{GRAPH}/{path}?" + urllib.parse.urlencode(params))
    if "error" in d:
        err = d["error"]
        raise GraphError(err.get("code"), err.get("message", str(err)))
    return d


def check_meta_token(user_token):
    """Fail loudly on an invalid token; warn when expiry is near.

    The expiry check needs META_APP_ID/META_APP_SECRET; if they aren't
    configured we skip it silently (an expired token still fails loudly
    below when the first real call returns error code 190).
    """
    app_id, app_secret = os.environ.get("META_APP_ID"), os.environ.get("META_APP_SECRET")
    if not (app_id and app_secret):
        return
    try:
        d = graph_get("debug_token", f"{app_id}|{app_secret}", input_token=user_token)["data"]
    except (GraphError, RuntimeError) as e:
        warn(f"Could not check Meta token expiry ({e}); continuing.")
        return
    if not d.get("is_valid"):
        die("The Meta access token is INVALID or EXPIRED. No metrics can be "
            "collected until it is renewed — see 'Renewing the Meta token' in the README.")
    expires = d.get("expires_at")
    if expires:
        days_left = (dt.datetime.fromtimestamp(expires) - dt.datetime.now()).days
        expiry_date = dt.datetime.fromtimestamp(expires).date()
        if days_left <= TOKEN_WARN_DAYS:
            warn(f"The Meta access token expires in {days_left} day(s), on {expiry_date}. "
                 "Renew it NOW — see 'Renewing the Meta token' in the README.")
        else:
            log(f"Meta token OK, expires {expiry_date} ({days_left} days left).")


def get_page_token(user_token, page_id):
    """Exchange the long-lived user token for a Page token (needed for Page insights)."""
    try:
        return graph_get(page_id, user_token, fields="access_token")["access_token"]
    except GraphError as e:
        if e.code == 190:
            die("Meta rejected the access token (OAuth error 190) — it has likely "
                "expired. See 'Renewing the Meta token' in the README.")
        warn(f"Could not obtain a Page access token ({e.message}); "
             "falling back to the user token — some Page metrics may fail.")
        return user_token


def insight_value(page_or_ig_id, token, metric, **params):
    """Latest value of a single insights metric, or None if unavailable."""
    try:
        d = graph_get(f"{page_or_ig_id}/insights", token, metric=metric, **params)
    except GraphError as e:
        if e.code == 190:
            raise
        log(f"  metric '{metric}' unavailable (likely deprecated by Meta) — skipping. "
            f"[{e.message[:90]}]")
        return None
    data = d.get("data") or []
    if not data:
        log(f"  metric '{metric}' returned no data — skipping.")
        return None
    entry = data[0]
    if "total_value" in entry:
        return entry["total_value"].get("value")
    values = entry.get("values") or []
    return values[-1].get("value") if values else None


# ---------------------------------------------------------------- Facebook

FB_TYPE_MAP = {"photo": "Photo", "album": "Carousel", "video": "Video",
               "share": "Link", "link": "Link"}


def collect_facebook(user_token, page_id, since):
    page_token = get_page_token(user_token, page_id)

    log("Facebook: fetching page-level metrics…")
    page = graph_get(page_id, page_token, fields="name,followers_count")
    log(f"  page '{page.get('name')}': {page.get('followers_count')} followers")

    snapshot = {
        "Date": dt.date.today().isoformat(),
        "Platform": "Facebook",
        "Followers": page.get("followers_count"),
        # Meta deprecated ALL page-level reach/impressions metrics (June 2026).
        # These calls log-and-skip until/unless Meta ships a replacement.
        "Reach": insight_value(page_id, page_token, "page_impressions_unique",
                               period="days_28"),
        "Impressions": insight_value(page_id, page_token, "page_impressions",
                                     period="days_28"),
        "Profile Views": insight_value(page_id, page_token, "page_views_total",
                                       period="days_28"),
        "Source": "API",
    }

    log("Facebook: fetching posts…")
    posts = []
    # Reaction/comment summary fields need the pages_read_user_content scope;
    # try with them first and fall back to post insights if denied.
    rich_fields = ("id,created_time,message,permalink_url,attachments{media_type},"
                   "shares,reactions.summary(true).limit(0),comments.summary(true).limit(0)")
    base_fields = "id,created_time,message,permalink_url,attachments{media_type},shares"
    have_summaries = True
    try:
        raw = list(_paged_posts(page_id, page_token, rich_fields, since))
    except GraphError as e:
        if e.code != 10:
            raise
        have_summaries = False
        log("  token lacks 'pages_read_user_content' — reading reactions via post "
            "insights instead (comment counts unavailable; see README).")
        raw = list(_paged_posts(page_id, page_token, base_fields, since))

    for p in raw:
        media_type = None
        att = (p.get("attachments") or {}).get("data") or []
        if att:
            media_type = att[0].get("media_type")
        post_type = FB_TYPE_MAP.get((media_type or "").lower(), "Text")
        if post_type == "Video" and "/reel/" in (p.get("permalink_url") or ""):
            post_type = "Reel"
        likes = comments = None
        if have_summaries:
            likes = ((p.get("reactions") or {}).get("summary") or {}).get("total_count")
            comments = ((p.get("comments") or {}).get("summary") or {}).get("total_count")
        else:
            reactions = insight_value(p["id"], page_token, "post_reactions_by_type_total")
            if isinstance(reactions, dict):
                likes = sum(reactions.values())
        posts.append({
            "Post ID": p["id"],
            "Platform": "Facebook",
            "Published": p["created_time"][:10],
            "Type": post_type,
            "Permalink": p.get("permalink_url"),
            "Caption": (p.get("message") or "")[:100],
            # post-level reach (post_impressions_unique) was deprecated by Meta
            "Reach": None,
            "Likes": likes,
            "Comments": comments,
            "Shares": (p.get("shares") or {}).get("count"),
            "Saves": None,  # not a Facebook concept
        })
    log(f"  {len(posts)} Facebook post(s) in window.")
    return snapshot, posts


def _paged_posts(page_id, token, fields, since):
    params = {"fields": fields, "limit": 100, "since": int(since.timestamp())}
    d = graph_get(f"{page_id}/posts", token, **params)
    while True:
        yield from d.get("data") or []
        next_url = (d.get("paging") or {}).get("next")
        if not next_url:
            return
        d = _http_json(next_url)
        if "error" in d:
            err = d["error"]
            raise GraphError(err.get("code"), err.get("message", str(err)))


# --------------------------------------------------------------- Instagram

def ig_media_type(media):
    if media.get("media_product_type") == "REELS":
        return "Reel"
    return {"CAROUSEL_ALBUM": "Carousel", "VIDEO": "Video",
            "IMAGE": "Photo"}.get(media.get("media_type"), "Photo")


def collect_instagram(user_token, ig_id, since):
    log("Instagram: fetching account-level metrics…")
    acct = graph_get(ig_id, user_token, fields="username,followers_count")
    log(f"  @{acct.get('username')}: {acct.get('followers_count')} followers")

    now = int(time.time())
    window = {"period": "day", "metric_type": "total_value",
              "since": now - 28 * 86400, "until": now}
    snapshot = {
        "Date": dt.date.today().isoformat(),
        "Platform": "Instagram",
        "Followers": acct.get("followers_count"),
        "Reach": insight_value(ig_id, user_token, "reach", **window),
        # Meta replaced IG "impressions" with "views" (2024) — stored as Impressions
        "Impressions": insight_value(ig_id, user_token, "views", **window),
        "Profile Views": insight_value(ig_id, user_token, "profile_views", **window),
        "Source": "API",
    }

    log("Instagram: fetching media…")
    posts = []
    params = {
        "fields": "id,caption,media_type,media_product_type,permalink,"
                  "timestamp,like_count,comments_count",
        "limit": 100,
        "since": int(since.timestamp()),
    }
    d = graph_get(f"{ig_id}/media", user_token, **params)
    media = list(d.get("data") or [])
    while (d.get("paging") or {}).get("next"):
        d = _http_json(d["paging"]["next"])
        if "error" in d:
            break
        media.extend(d.get("data") or [])

    for m in media:
        metrics = {}
        try:
            ins = graph_get(f"{m['id']}/insights", user_token,
                            metric="reach,shares,saved")
            metrics = {e["name"]: (e.get("values") or [{}])[-1].get("value")
                       for e in ins.get("data") or []}
        except GraphError as e:
            log(f"  insights unavailable for media {m['id']} — {e.message[:80]}")
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
        })
    log(f"  {len(posts)} Instagram post(s) in window.")
    return snapshot, posts


# ---------------------------------------------------------------- Airtable

def airtable_upsert(table, records, merge_on):
    """Batched upsert; Airtable allows 10 records per request."""
    if not records:
        return 0
    token = os.environ["AIRTABLE_TOKEN"]
    base = os.environ["AIRTABLE_BASE_ID"]
    url = f"{AIRTABLE}/{base}/{urllib.parse.quote(table)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    written = 0
    for i in range(0, len(records), 10):
        batch = records[i:i + 10]
        payload = {
            "performUpsert": {"fieldsToMergeOn": merge_on},
            "typecast": True,
            "records": [
                {"fields": {k: v for k, v in r.items() if v is not None}}
                for r in batch
            ],
        }
        d = _http_json(url, method="PATCH", payload=payload, headers=headers)
        if "error" in d:
            raise RuntimeError(f"Airtable error writing to '{table}': {d['error']}")
        written += len(d.get("records") or [])
        time.sleep(0.25)  # stay far below Airtable's 5 req/s limit
    return written


# -------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=35,
                        help="post lookback window in days (default 35; overlap is "
                             "safe — posts upsert on Post ID and recent posts get "
                             "their engagement numbers refreshed)")
    parser.add_argument("--dry-run", action="store_true",
                        help="collect and print, but write nothing to Airtable")
    args = parser.parse_args()

    load_env()
    user_token = os.environ["META_ACCESS_TOKEN"]
    since = dt.datetime.now() - dt.timedelta(days=args.days)

    check_meta_token(user_token)

    snapshots, posts, failures = [], [], []
    for name, fn, target in [
        ("Facebook", collect_facebook, os.environ["FB_PAGE_ID"]),
        ("Instagram", collect_instagram, os.environ["IG_BUSINESS_ACCOUNT_ID"]),
    ]:
        try:
            snap, plist = fn(user_token, target, since)
            snapshots.append(snap)
            posts.extend(plist)
        except GraphError as e:
            if e.code == 190:
                die("Meta rejected the access token (OAuth error 190) — it has "
                    "likely expired. See 'Renewing the Meta token' in the README.")
            failures.append(name)
            warn(f"{name} collection failed and was skipped: {e.message}")
        except RuntimeError as e:
            failures.append(name)
            warn(f"{name} collection failed and was skipped: {e}")

    if not snapshots:
        die("Both platforms failed — nothing to write.")

    if args.dry_run:
        log("\n--dry-run: would write the following, skipping Airtable:")
        log(json.dumps({"snapshots": snapshots, "posts": posts},
                       indent=2, ensure_ascii=False))
        return

    log("Writing to Airtable…")
    n_snap = airtable_upsert(SNAPSHOTS_TABLE, snapshots, ["Date", "Platform"])
    n_posts = airtable_upsert(POSTS_TABLE, posts, ["Post ID"])
    log(f"Done: upserted {n_snap} snapshot row(s) and {n_posts} post row(s)."
        + (f" Platforms skipped due to errors: {', '.join(failures)}." if failures else ""))


if __name__ == "__main__":
    main()

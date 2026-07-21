# Mamram Alumni Association — Social Media Dashboard

Tracks growth and engagement of the association's **Facebook Page** and
**Instagram Business account**. Metrics are collected daily from the Meta
Graph API, stored in **Airtable**, and shown on a static **dashboard** web page.

This README is written for a non-technical maintainer. You do not need to
understand the code — only the three routine tasks below.

---

## How it works (the 60-second version)

```
Meta Graph API ──▶ collector.py (runs daily via GitHub Actions)
                        │
                        ▼
                 Airtable base ("Snapshots" + "Posts" tables)
                        │
                        ▼
              docs/index.html (GitHub Pages, reads Airtable)
```

- **`collector.py`** — pulls current follower counts, reach, views, profile
  views, and recent posts for both platforms, and writes one snapshot row per
  platform per run into Airtable. Re-running it on the same day is safe: it
  updates the existing rows instead of duplicating them.
- **`backfill.py`** — one-time import of historical Facebook CSV exports
  (already run; you should never need it again).
- **`docs/`** — a single web page that reads the Airtable data and draws
  the charts. No server needed.
- **`.github/workflows/collect.yml`** — the schedule. GitHub runs the
  collector automatically every day at 09:00 Israel time.

---

## Routine task 1 — check that it's working

1. Open the GitHub repository → **Actions** tab.
2. The latest "Collect social metrics" run should have a green check.
3. If it's red, open it and read the error message at the bottom — it says in
   plain English what to do (almost always: renew the Meta token, below).

You can also just open the Airtable base: the **Snapshots** table should have
two new rows (Facebook + Instagram) dated today (or the most recent run).

## Routine task 2 — run the collector manually

- **From GitHub** (easiest): Actions tab → "Collect social metrics" →
  **Run workflow** button → green **Run workflow**.
- **From a computer**: with Python 3 installed and a `.env` file in the
  project folder (copy the variable names from the list below):
  `python3 collector.py`

## Routine task 3 — renew the Meta token (only if a run fails)

The stored token is a **Page access token with no expiry date**, so under
normal circumstances there is nothing to renew. It can still die in rare
cases — the Facebook account that created it changes its password, gets
logged out by a Facebook security check, or loses admin access to the Page.
If that happens the daily run **fails loudly** with a message pointing here.
To create a fresh token:

1. Go to <https://developers.facebook.com/tools/explorer/> and log in with the
   Facebook account that manages the עמותת בוגרי ממר"ם Page.
2. In the top-right **Meta App** dropdown, pick **Mamram Social**.
3. Click **Generate Access Token**. Approve the permissions dialog
   (it should list: `pages_show_list`, `pages_read_engagement`,
   `read_insights`, `instagram_basic`, `instagram_manage_insights` —
   if you're asked about `pages_read_user_content`, approve that too; it adds
   Facebook comment counts).
4. Copy the token shown in the "Access Token" field.
5. That token only lasts ~2 hours — extend it: open
   <https://developers.facebook.com/tools/debug/accesstoken/>, paste the
   token, press **Debug**, then press **Extend Access Token** (bottom of the
   page). Copy the new long-lived token it gives you.
6. Turn it into a **never-expiring Page token** (so you don't have to do this
   again in 60 days): back in the Graph API Explorer, paste the extended token
   into the **Access Token** box, set the query field to
   `147353218653013?fields=access_token`, press **Submit**, and copy the
   `access_token` value from the response. That's the token to store.
7. Put the new token where the collector reads it:
   - GitHub: repository → **Settings → Secrets and variables → Actions** →
     edit **META_ACCESS_TOKEN** → paste → save.
   - Local `.env` file (if you run manually): replace the `META_ACCESS_TOKEN`
     value.
8. Re-run the workflow (Routine task 2) and confirm it is green.

---

## Configuration (environment variables / GitHub secrets)

| Name | What it is |
|---|---|
| `META_ACCESS_TOKEN` | Never-expiring Meta Page token (see renewal steps above if it ever dies) |
| `FB_PAGE_ID` | Facebook Page ID |
| `IG_BUSINESS_ACCOUNT_ID` | Instagram Business account ID |
| `META_APP_ID` / `META_APP_SECRET` | The "Mamram Social" Meta app — used only to check token expiry and warn early (optional but recommended) |
| `AIRTABLE_TOKEN` | Airtable personal access token with read/write access to the base (collector only) |
| `AIRTABLE_BASE_ID` | The Airtable base ID (starts with `app`) |

Never commit any of these to git. Locally they live in `.env` (gitignored);
on GitHub they live in Actions secrets.

## Airtable schema

**Snapshots** — one row per platform per run (daily):
`Date`, `Platform` (Facebook/Instagram), `Followers`, `Reach`, `Impressions`,
`Profile Views`, `Source` (API/Backfill).

**Posts** — one row per post, refreshed on later runs while recent:
`Post ID` (unique), `Platform`, `Published`, `Type`, `Permalink`, `Caption`
(first 100 chars), `Reach`, `Likes`, `Comments`, `Shares`, `Saves`,
`Last Synced` (date the metrics were last fetched from Meta).

## Dashboard

`docs/index.html` + `docs/config.js`. What it shows: follower growth,
weekly/monthly net new followers, Instagram reach &amp; views, engagement rate,
format performance (avg interactions by post type), best day to post, top-5
posts, and a sortable all-posts table. Click any post row for a detail view
with an embedded preview of the actual post and comparisons against similar
posts (post embeds are loaded from instagram.com/facebook.com and only work
for public posts). Date range presets + a custom from/to picker scope
everything; the tiles show change vs the previous equal-length period. The
"Print report" button (or ⌘P) produces a clean report of the current view
for management meetings.

Host on GitHub Pages
(repository → Settings → Pages → deploy from branch, folder `/docs` — or
serve the folder any other way). To preview locally:
`python3 -m http.server 8123 --directory docs` then open
<http://localhost:8123>.

**Security note:** `config.js` is downloaded by every visitor, so the Airtable
token in it is public. It must be a **dedicated read-only token**: create it
at <https://airtable.com/create/tokens> with only the `data.records:read`
scope and access to only this base. Anyone with the dashboard URL can read
(not change) the metrics data — acceptable for this data. If that ever becomes
a problem, the upgrade path is a tiny server-side proxy that keeps the token
private (not part of v1).

## Known limitations (as of July 2026)

- **Post likes/comments are a snapshot, not live.** Every number on the
  dashboard is whatever the collector last stored in Airtable — it is not
  fetched live from Instagram/Facebook (the public dashboard has no Meta
  token, and one could not be exposed safely). A post keeps gaining likes and
  comments after we capture it, so a *recent* post can read lower here than in
  the Instagram app; older posts match exactly because they have stopped
  changing. Each post's detail view shows its **Last Synced** date, and the
  embedded post in that view shows the platform's current count. The collector
  runs **daily** (see the workflow) so recent posts stay within ~24h, and it
  refreshes every post published in the last ~35 days on each run.
- **Facebook page-level Reach and Impressions are empty going forward** — Meta
  removed those metrics from the API for all apps on June 15, 2026. The
  collector logs this and skips them rather than failing; if Meta ships a
  replacement metric, add it in `collect_facebook()` in `collector.py`.
- **Facebook comment counts are empty** unless the token includes the
  `pages_read_user_content` permission (see renewal step 3). Likes are
  collected either way (via post insights when the permission is missing).
- **Instagram "Impressions" actually stores Meta's "views" metric** — Meta
  replaced impressions with views in 2024; it's the closest equivalent.
- **Backfilled Facebook follower history is approximate.** The Meta CSV export
  only contains *new follows per day*, so history was reconstructed backwards
  from the follower count on 2026-07-14; unfollows aren't in the export, so
  older totals may be slightly high. The trend shape is correct.
- **Instagram history has three different start dates**, set by what Meta's
  API exposes: reach/views/profile-views were backfilled to **Aug 2024**
  (`backfill_instagram.py`, already run), the full post archive goes back to
  the first post (**Mar 2022**), but Meta refuses to report follower history
  older than 30 days. IG follower counts **between July 2025 and June 2026
  are linear estimates** anchored to one verified data point (2025-07-27:
  1,559 followers, from a screenshot) — the trend is real, weekly wiggles are
  not. Counts before July 2025 are blank (no data source exists), and counts
  from mid-June 2026 onward are real.

## Files

| File | Purpose |
|---|---|
| `collector.py` | Weekly metrics collector (no dependencies beyond Python 3) |
| `backfill.py` | One-time historical CSV import, Facebook (already run) |
| `backfill_instagram.py` | One-time Instagram history pull from the API (already run) |
| `csv/` | The original Meta Business Suite exports (Facebook, 2024-01 → 2026-07). Kept on the maintainer's machine only — not committed to the public repo |
| `docs/index.html` | The dashboard page |
| `docs/config.js` | Dashboard's Airtable credentials (public, read-only) |
| `docs/config.example.js` | Template for the above |
| `.github/workflows/collect.yml` | Weekly schedule + manual run button |

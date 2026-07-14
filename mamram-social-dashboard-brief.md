# Project Brief: Mamram Alumni Association — Social Media Growth Dashboard (Meta)

## Context

I'm building a leave-behind system for the Mamram Alumni Association (a nonprofit running startup accelerator programs in Israel) that tracks growth and engagement of its Facebook Page and Instagram Business account. The system must keep working after my internship ends with near-zero maintenance by non-technical staff.

## Goal

A pipeline that pulls metrics from the Meta Graph API on a schedule, stores snapshots in Airtable, and a dashboard that visualizes growth and insights from that Airtable data.

## Architecture (already decided — don't redesign)

1. **Collector script** (Python preferred) that:
   - Pulls current metrics from the Meta Graph API for one Facebook Page and one linked Instagram Business account
   - Appends a snapshot row per platform to an Airtable base
   - Runs on a schedule via **GitHub Actions cron** (weekly), and can also be run manually
2. **One-time backfill script** that ingests historical CSV exports from Meta Business Suite into the same Airtable tables (I have these CSVs; ask me for column samples before writing the parser — Meta export formats vary)
3. **Dashboard**: a static single-page web app (HTML/JS, no build step required, hostable on GitHub Pages) that reads from Airtable via its API and renders charts

## Credentials (I will provide via .env / GitHub Actions secrets — NEVER hardcode)

- `META_ACCESS_TOKEN` — long-lived Page token
- `FB_PAGE_ID`
- `IG_BUSINESS_ACCOUNT_ID`
- `AIRTABLE_TOKEN` — personal access token scoped to the base
- `AIRTABLE_BASE_ID`

Include token-refresh handling or clear documentation for renewing the long-lived Meta token (~60-day expiry). If a refresh can't be fully automated, add an expiry warning (e.g., the GitHub Action fails loudly with a clear message when the token is invalid).

## Metrics — v1 scope (keep it tight, extensible later)

Per weekly snapshot, per platform:

| Metric | Facebook | Instagram |
|---|---|---|
| Followers / Page likes | ✔ | ✔ |
| Reach (trailing 28 days if available, else weekly) | ✔ | ✔ |
| Impressions/views | ✔ | ✔ |
| Profile views | — | ✔ |

Plus a **Posts table**: for posts published since the last run — post ID, date, type, permalink, caption (first 100 chars), reach, likes/reactions, comments, shares/saves.

Use whichever current Graph API metric names are valid — verify against current API docs rather than memory; Meta deprecates insight metric names frequently. If a metric in the table above is no longer available, log it and skip rather than failing the whole run.

## Airtable schema

**Table: Snapshots**
- `Date` (date)
- `Platform` (single select: Facebook, Instagram)
- `Followers` (number)
- `Reach` (number)
- `Impressions` (number)
- `Profile Views` (number, IG only)
- `Source` (single select: API, Backfill)

**Table: Posts**
- `Post ID` (text, unique — dedupe on this)
- `Platform` (single select)
- `Published` (date)
- `Type` (single select: Photo, Video, Reel, Carousel, Link, Text)
- `Permalink` (URL)
- `Caption` (text, truncated)
- `Reach`, `Likes`, `Comments`, `Shares`, `Saves` (numbers)

Create the tables via the Airtable API if they don't exist, or document the manual setup in the README.

## Dashboard requirements

- Follower growth line chart over time, per platform (this is the headline visual)
- Reach/impressions trend
- Engagement rate over time (engagement ÷ reach or ÷ followers — pick one, label it clearly)
- Top 5 posts by engagement (with permalinks)
- Date range selector
- Clean, presentable look — this will be shown to the association's management team
- Note: the Airtable token used by a public dashboard is exposed client-side. Default to a **read-only token scoped to only this base**, and flag this limitation in the README with the alternative (a tiny proxy) as a future upgrade, not part of v1.

## Non-functional requirements

- **Idempotent runs**: re-running the collector on the same day must not duplicate rows
- **Graceful degradation**: one failed metric or platform must not kill the run; log and continue
- **README for a non-technical successor**: how it works, how to re-run manually, how to renew the Meta token (with screenshots-level step descriptions), what to do if the GitHub Action fails
- **Rate limits**: stay well within Meta and Airtable rate limits (trivial at this volume, but batch Airtable writes)

## Working style

- Start by confirming the plan and asking me anything ambiguous before writing code
- Build in this order: (1) collector against live API with my credentials, (2) Airtable write layer, (3) GitHub Action, (4) backfill parser, (5) dashboard
- Test the collector end-to-end with a real run before moving on
- Ask me for a sample of the Meta CSV exports before writing the backfill parser

## Out of scope for v1

- TikTok and LinkedIn (manual CSV ingestion may be added later — keep the Snapshots schema platform-agnostic so they can slot in)
- Automated posting or any write operations to Meta
- Auth/user accounts on the dashboard

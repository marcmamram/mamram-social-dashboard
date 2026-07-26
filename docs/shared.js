"use strict";
/* Shared data + logic layer for both dashboard views.
 *
 * Loaded by index.html (Executive View) and details.html (Operator View) so
 * the comparison logic lives in exactly one place. Everything here is
 * read-only against Airtable; no writes happen from the browser.
 */

/* ------------------------------------------------------------- tunables
 * Staff can adjust these without touching logic. They mirror the constants
 * in takeaway.py — keep the two in step if you change a threshold.
 */
const TREND_THRESHOLD_PCT = 10;      // % change that counts as up/down vs flat
const VERDICT_WEIGHTS = {            // must sum to 1
  engagementRate: 0.5,               // weighted highest: hardest to inflate
  followerGrowth: 0.3,
  reach: 0.2,                        // can be inflated by boosts
};
const BASELINE_PERIODS = 8;          // trailing periods for the rolling baseline
/* Net follower moves are small integers, so a percentage change between two of
 * them (+78 vs −4) produces nonsense like "up 2050%". Follower growth is
 * therefore judged on the change in net adds and always DISPLAYED as counts. */
const FOLLOWER_NOISE_FLOOR = 3;      // net-add difference below this reads as flat
const MAX_DISPLAY_PCT = 300;         // clamp absurd percentages in the text

const FB = "Facebook", IG = "Instagram";
const PLATFORMS = [FB, IG];
const COLOR = { [FB]: "var(--fb)", [IG]: "var(--ig)" };

/* ------------------------------------------------------------ formatting */

const fmt = n => n == null ? "—" : n.toLocaleString("en-US");
const fmtDate = iso => new Date(iso + "T00:00:00").toLocaleDateString("en-GB",
  { day: "numeric", month: "short", year: "numeric" });
const fmtMonth = iso => new Date(iso + "T00:00:00").toLocaleDateString("en-GB",
  { month: "short", year: "2-digit" });
const addDays = (iso, n) => {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};
const todayISO = () => new Date().toISOString().slice(0, 10);
const mondayOf = iso => {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
  return d.toISOString().slice(0, 10);
};

const ENG = p => (p.Likes || 0) + (p.Comments || 0) + (p.Shares || 0) + (p.Saves || 0);
/* Engagement ÷ reach. Null when reach is missing — Meta removed Facebook post
 * reach, so FB posts carry no rate and stay out of rate maths entirely. */
const engRate = p => p.Reach ? ENG(p) / p.Reach : null;

const pctChange = (cur, prev) =>
  (cur == null || prev == null || prev === 0) ? null : (cur - prev) / prev * 100;

const mean = arr => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null;

/* ---------------------------------------------------------- data loading */

async function fetchTable(table) {
  const cfg = window.DASHBOARD_CONFIG || {};
  const out = [];
  let offset;
  do {
    const url = new URL(`https://api.airtable.com/v0/${cfg.AIRTABLE_BASE_ID}/${encodeURIComponent(table)}`);
    url.searchParams.set("pageSize", "100");
    if (offset) url.searchParams.set("offset", offset);
    const r = await fetch(url, { headers: { Authorization: `Bearer ${cfg.AIRTABLE_TOKEN}` } });
    if (!r.ok) throw new Error(`Airtable ${table}: HTTP ${r.status}`);
    const d = await r.json();
    out.push(...d.records.map(rec => rec.fields));
    offset = d.offset;
  } while (offset);
  return out;
}

/* One row per (Date, Platform); a real API reading beats a backfilled one. */
function mergeSnapshots(rows) {
  const byKey = new Map();
  for (const r of rows) {
    if (!r.Date || !r.Platform) continue;
    const k = r.Date + "|" + r.Platform;
    const prev = byKey.get(k);
    if (!prev || (prev.Source === "Backfill" && r.Source === "API")) byKey.set(k, r);
  }
  return [...byKey.values()].sort((a, b) => a.Date < b.Date ? -1 : 1);
}

async function loadAll() {
  const [snapRows, posts, takeaways] = await Promise.all([
    fetchTable("Snapshots"),
    fetchTable("Posts"),
    fetchTable("Takeaways").catch(() => []),   // optional table
  ]);
  return { snapshots: mergeSnapshots(snapRows), posts, takeaways };
}

function latestTakeaway(takeaways) {
  const sorted = [...takeaways]
    .filter(t => t["Weekly Takeaway"])
    .sort((a, b) => (a["Week Of"] || "") < (b["Week Of"] || "") ? 1 : -1);
  return sorted[0] || null;
}

/* ------------------------------------------------------- verdict scoring */

const RANGES = {
  week:    { days: 7,  label: "Week",    baselineLabel: "the previous week" },
  month:   { days: 30, label: "Month",   baselineLabel: "the previous month" },
  quarter: { days: 90, label: "Quarter", baselineLabel: "the previous quarter" },
};

/* Latest non-null value of `field` for `platform` at or before `dateISO`. */
function snapshotAt(snapshots, platform, dateISO, field) {
  let hit = null;
  for (const s of snapshots) {
    if (s.Platform !== platform || s.Date > dateISO) continue;
    if (s[field] != null) hit = s[field];
  }
  return hit;
}

function classify(deltaPct) {
  if (deltaPct == null) return null;
  if (deltaPct > TREND_THRESHOLD_PCT) return 1;               // green
  if (deltaPct < -TREND_THRESHOLD_PCT) return -1;             // red
  return 0;                                                    // yellow
}

/**
 * Compute the verdict for a range.
 * Compares the trailing `days` against the immediately preceding equal-length
 * period. Metrics with no data (e.g. Facebook reach) are skipped, and the
 * weights of the surviving metrics are renormalised so a missing metric can
 * never drag the score toward red.
 */
function computeVerdict(data, rangeKey) {
  const { snapshots, posts } = data;
  const days = RANGES[rangeKey].days;
  const to = todayISO();
  const from = addDays(to, -(days - 1));
  const prevTo = addDays(from, -1);
  const prevFrom = addDays(prevTo, -(days - 1));

  const inRange = (d, a, b) => d && d >= a && d <= b;
  const metrics = [];

  for (const plat of PLATFORMS) {
    const cur = posts.filter(p => p.Platform === plat && inRange(p.Published, from, to));
    const prev = posts.filter(p => p.Platform === plat && inRange(p.Published, prevFrom, prevTo));

    // engagement rate — mean per-post rate (needs reach, so IG only in practice)
    const curRates = cur.map(engRate).filter(r => r);
    const prevRates = prev.map(engRate).filter(r => r);
    if (curRates.length && prevRates.length) {
      metrics.push({ key: "engagementRate", platform: plat, label: "engagement rate",
        delta: pctChange(mean(curRates), mean(prevRates)) });
    }

    // follower growth — net adds this period vs net adds last period, scored
    // on the difference in counts (see FOLLOWER_NOISE_FLOOR above)
    const fNow = snapshotAt(snapshots, plat, to, "Followers");
    const fMid = snapshotAt(snapshots, plat, prevTo, "Followers");
    const fThen = snapshotAt(snapshots, plat, prevFrom, "Followers");
    if (fNow != null && fMid != null && fThen != null) {
      const growth = fNow - fMid, prevGrowth = fMid - fThen;
      const diff = growth - prevGrowth;
      const score = Math.abs(diff) < FOLLOWER_NOISE_FLOOR ? 0 : (diff > 0 ? 1 : -1);
      metrics.push({
        key: "followerGrowth", platform: plat, label: "follower growth",
        counts: { growth, prevGrowth },
        display: `${growth >= 0 ? "+" : "−"}${Math.abs(growth)}`,
        // delta is kept only so the "sharply red" test has a magnitude to read;
        // it is never shown as a percentage
        delta: fNow ? diff / fNow * 100 * 20 : 0,
        forcedScore: score,
      });
    }

    // reach — snapshots are trailing-28-day totals, so compare like with like
    const rNow = snapshotAt(snapshots, plat, to, "Reach");
    const rPrev = snapshotAt(snapshots, plat, prevTo, "Reach");
    const rDelta = pctChange(rNow, rPrev);
    if (rDelta != null) {
      metrics.push({ key: "reach", platform: plat, label: "reach", delta: rDelta });
    }
  }

  // Platforms that produced no usable metric at all — named explicitly rather
  // than silently omitted, so a verdict is never read as covering a platform
  // it could not actually measure.
  const silent = PLATFORMS.filter(p => !metrics.some(m => m.platform === p));

  if (!metrics.length) {
    return { badge: "unknown", emoji: "⚪", text: "Not enough data yet",
             sentence: "There isn't enough history yet to judge performance — "
                       + "check back after a few more days of collection.",
             metrics: [], silent, range: rangeKey, from, to };
  }

  // weighted average of per-metric scores, renormalised over present metrics
  let weighted = 0, totalWeight = 0;
  for (const m of metrics) {
    const w = (VERDICT_WEIGHTS[m.key] || 0) / metrics.filter(x => x.key === m.key).length;
    m.score = m.forcedScore !== undefined ? m.forcedScore : classify(m.delta);
    weighted += m.score * w;
    totalWeight += w;
  }
  const result = totalWeight ? weighted / totalWeight : 0;
  const sharplyRed = metrics.some(m => m.delta < -2 * TREND_THRESHOLD_PCT);
  const anyRed = metrics.some(m => m.score === -1);

  let badge, emoji, text;
  if (result > 0.15 && !anyRed) {
    badge = "green"; emoji = "🟢"; text = "On track";
  } else if (sharplyRed && result < 0) {
    badge = "red"; emoji = "🔴"; text = "Underperforming";
  } else {
    badge = "yellow"; emoji = "🟡"; text = "Mixed";
  }

  return { badge, emoji, text, metrics, silent, result, range: rangeKey, from, to,
           sentence: verdictSentence(metrics, rangeKey, silent) };
}

/* One plain sentence explaining the badge. Deliberately describes the
 * CHANNEL's performance, never a person's — this text may be screenshotted
 * and forwarded without context. */
function verdictSentence(metrics, rangeKey, silent = []) {
  const baseline = RANGES[rangeKey].baselineLabel;
  const describe = m => {
    // Follower growth speaks in counts — a percentage between two small net
    // changes is misleading (see FOLLOWER_NOISE_FLOOR).
    if (m.key === "followerGrowth") {
      const { growth, prevGrowth } = m.counts;
      if (m.score === 0) {
        return `${m.platform} followers held about steady (${m.display})`;
      }
      const verb = growth >= 0 ? "gained" : "lost";
      return `${m.platform} ${verb} ${Math.abs(growth)} followers, `
           + `${m.score > 0 ? "up from" : "down from"} ${prevGrowth >= 0 ? "+" : "−"}`
           + `${Math.abs(prevGrowth)}`;
    }
    if (m.score === 0) return `${m.platform} ${m.label} is flat`;
    const pct = Math.min(Math.abs(m.delta), MAX_DISPLAY_PCT);
    const over = Math.abs(m.delta) > MAX_DISPLAY_PCT ? "over " : "";
    return `${m.platform} ${m.label} is ${m.score > 0 ? "up" : "down"} `
         + `${over}${pct.toFixed(0)}%`;
  };
  // lead with the biggest mover, mention at most two things
  const rank = m => m.key === "followerGrowth"
    ? Math.abs(m.counts.growth - m.counts.prevGrowth) : Math.abs(m.delta);
  const ranked = [...metrics].sort((a, b) => rank(b) - rank(a));
  let s = ranked.slice(0, 2).map(describe).join("; ") + ` vs ${baseline}.`;
  if (silent.length) {
    s += ` ${silent.join(" and ")} had no measurable change in this window.`;
  }
  return s;
}

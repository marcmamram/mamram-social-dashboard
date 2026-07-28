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
/* A post keeps gaining for days after publication — on this account the median
 * engagement rate roughly doubles between day 2 and day 7. Comparing brand-new
 * posts against matured ones makes every current period look like a collapse,
 * so posts younger than this are excluded from rate comparisons on BOTH sides. */
const POST_MATURITY_DAYS = 3;
const MIN_POSTS_FOR_RATE = 2;        // fewer than this either side → skip the metric
/* How decisive the weighted score must be before the badge commits to green or
 * red; inside this band it stays "Mixed". */
const VERDICT_DECISION_MARGIN = 0.15;

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
/* Median, not mean: one exceptional week (a post that went viral) would drag a
 * mean baseline up and make every ordinary week afterwards look like a
 * failure. */
function median(arr) {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

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
  week:    { days: 7,  label: "Week",    baselineLabel: "its usual week" },
  month:   { days: 30, label: "Month",   baselineLabel: "its usual month" },
  quarter: { days: 90, label: "Quarter", baselineLabel: "its usual quarter" },
};

/* Latest non-null value of `field` for `platform` at or before `dateISO`,
 * together with the date it actually came from. The date matters: snapshot
 * coverage is uneven (weekly for backfilled history, daily since), so two
 * nominally equal windows can span different numbers of real days. */
function snapshotAtDated(snapshots, platform, dateISO, field) {
  let hit = null;
  for (const s of snapshots) {
    if (s.Platform !== platform || s.Date > dateISO) continue;
    if (s[field] != null) hit = { value: s[field], date: s.Date };
  }
  return hit;
}

function snapshotAt(snapshots, platform, dateISO, field) {
  const hit = snapshotAtDated(snapshots, platform, dateISO, field);
  return hit ? hit.value : null;
}

const daysBetween = (a, b) =>
  Math.round((new Date(b + "T00:00:00Z") - new Date(a + "T00:00:00Z")) / 86400000);

/* The most recent date we actually hold data for. Anchoring to this instead of
 * "today" stops a not-yet-collected today from silently shortening the current
 * window (6 days measured against a 7-day baseline). */
function dataEnd(snapshots) {
  let last = null;
  for (const s of snapshots) if (!last || s.Date > last) last = s.Date;
  return last || todayISO();
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
  // Anchor on the last day we actually have data for, not the wall clock.
  const to = dataEnd(snapshots);
  const from = addDays(to, -(days - 1));
  const prevTo = addDays(from, -1);
  const prevFrom = addDays(prevTo, -(days - 1));

  const inRange = (d, a, b) => d && d >= a && d <= b;
  const matured = p => p.Published && daysBetween(p.Published, to) >= POST_MATURITY_DAYS;
  const metrics = [];
  const notes = [];

  // Boundaries of period i back from the anchor: i = 0 is the current period.
  const periodEnd = i => addDays(to, -(days * i));
  const periodStart = i => addDays(periodEnd(i), -(days - 1));

  /* Net followers added during period i, normalised to a per-day rate and
   * re-expressed over the nominal period. Snapshot coverage is uneven (weekly
   * for backfilled history, daily since), so without this a 6-day window
   * silently loses to a 7-day baseline. */
  function netAdds(plat, i) {
    const endS = snapshotAtDated(snapshots, plat, periodEnd(i), "Followers");
    const startS = snapshotAtDated(snapshots, plat, addDays(periodStart(i), -1), "Followers");
    if (!endS || !startS) return null;
    const span = daysBetween(startS.date, endS.date);
    if (span <= 0) return null;
    return Math.round((endS.value - startS.value) / span * days);
  }

  const meanRate = (plat, i) => {
    const rates = posts
      .filter(p => p.Platform === plat && inRange(p.Published, periodStart(i), periodEnd(i)))
      .filter(matured).map(engRate).filter(r => r);
    return rates.length >= MIN_POSTS_FOR_RATE ? mean(rates) : null;
  };

  const reachAt = (plat, i) => snapshotAt(snapshots, plat, periodEnd(i), "Reach");

  /* Baseline = median of the preceding periods, not just the one before.
   * A single exceptional week otherwise makes the next ordinary week look
   * like a collapse — which is exactly how a healthy channel ends up flagged
   * "underperforming". */
  const baselineOf = fn => {
    const vals = [];
    for (let i = 1; i <= BASELINE_PERIODS; i++) {
      const v = fn(i);
      if (v != null) vals.push(v);
    }
    return vals.length ? { value: median(vals), n: vals.length } : null;
  };

  for (const plat of PLATFORMS) {
    const curPosts = posts.filter(p => p.Platform === plat && inRange(p.Published, from, to));

    // Engagement rate — matured posts only, on every side, so a period is
    // never marked down merely for containing fresh posts.
    const curRate = meanRate(plat, 0);
    const rateBase = baselineOf(i => meanRate(plat, i));
    if (curRate != null && rateBase) {
      metrics.push({ key: "engagementRate", platform: plat, label: "engagement rate",
        delta: pctChange(curRate, rateBase.value), baselineN: rateBase.n });
    } else if (curPosts.length) {
      const tooNew = curPosts.filter(p => !matured(p)).length;
      if (tooNew) {
        notes.push(`${plat}: ${tooNew} recent post${tooNew > 1 ? "s are" : " is"} `
                 + "too new to judge engagement yet");
      }
    }

    // Follower growth — counts, never a percentage between two small numbers.
    const growth = netAdds(plat, 0);
    const growthBase = baselineOf(i => netAdds(plat, i));
    if (growth != null && growthBase) {
      const typical = Math.round(growthBase.value);
      const diff = growth - typical;
      let score = Math.abs(diff) < FOLLOWER_NOISE_FLOOR ? 0 : (diff > 0 ? 1 : -1);
      // Still clearly growing, just not as fast as usual, is not "red" to a
      // reader — only shrinking, or a collapse in the rate, earns that.
      if (score < 0 && growth > 0 && growth >= typical * 0.5) score = 0;
      metrics.push({
        key: "followerGrowth", platform: plat, label: "follower growth",
        counts: { growth, prevGrowth: typical }, baselineN: growthBase.n,
        display: `${growth >= 0 ? "+" : "−"}${Math.abs(growth)}`,
        delta: typical ? (growth - typical) / Math.abs(typical) * 100 : 0,
        forcedScore: score,
      });
    }

    // Reach — snapshots are trailing-28-day totals, so compare like with like
    const curReach = reachAt(plat, 0);
    const reachBase = baselineOf(i => reachAt(plat, i));
    if (curReach != null && reachBase) {
      metrics.push({ key: "reach", platform: plat, label: "reach",
        delta: pctChange(curReach, reachBase.value), baselineN: reachBase.n });
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
             metrics: [], silent, notes, range: rangeKey, from, to };
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
  // Only a metric we actually scored red can count as "sharply" red — the
  // follower-growth magnitude is a ratio of small counts and would otherwise
  // trip this on ordinary noise.
  const sharplyRed = metrics.some(m => m.score === -1 && m.delta < -2 * TREND_THRESHOLD_PCT);
  const anyRed = metrics.some(m => m.score === -1);

  /* Symmetric dead zone: a score hovering either side of zero is "Mixed".
   * Without this, a barely-negative result (one soft metric among several
   * healthy ones) reads as "Underperforming", which overstates the case. */
  let badge, emoji, text;
  if (result > VERDICT_DECISION_MARGIN && !anyRed) {
    badge = "green"; emoji = "🟢"; text = "On track";
  } else if (sharplyRed && result < -VERDICT_DECISION_MARGIN) {
    badge = "red"; emoji = "🔴"; text = "Underperforming";
  } else {
    badge = "yellow"; emoji = "🟡"; text = "Mixed";
  }

  return { badge, emoji, text, metrics, silent, notes, result, range: rangeKey, from, to,
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
      const verb = growth >= 0 ? "gained" : "lost";
      return `${m.platform} ${verb} ${Math.abs(growth)} followers `
           + `(usual ${prevGrowth >= 0 ? "" : "−"}${Math.abs(prevGrowth)})`;
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
  let s = ranked.slice(0, 2).map(describe).join("; ")
        + `, compared with ${baseline}.`;
  if (silent.length) {
    s += ` ${silent.join(" and ")} had no measurable change in this window.`;
  }
  return s;
}

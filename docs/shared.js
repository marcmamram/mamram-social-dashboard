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

  /* How many posts a typical period of this length actually contains for this
   * account, from its own history. Calendar windows fit this account badly
   * (0–13 posts a week), so the engagement sample is sized in POSTS, and this
   * keeps "the last N posts" aligned with what a week/month/quarter really
   * means here rather than a guessed constant. */
  function typicalPostCount(plat) {
    const counts = [];
    const blocks = Math.max(1, Math.floor(365 / days));
    for (let i = 0; i < blocks; i++) {
      const end = addDays(to, -(days * i));
      const start = addDays(end, -(days - 1));
      counts.push(posts.filter(p => p.Platform === plat && inRange(p.Published, start, end)).length);
    }
    return Math.max(MIN_POSTS_FOR_RATE, Math.round(median(counts) || 0));
  }

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

    /* Engagement rate — the last N matured posts against the N before them,
     * and the N before those, taking the median of the earlier blocks as the
     * baseline. Sized in posts rather than dates because a fixed 7-day window
     * left this metric — half the whole score — missing about half of all
     * weeks on an account that posts irregularly. */
    const sampleSize = typicalPostCount(plat);
    const rated = posts
      .filter(p => p.Platform === plat && matured(p))
      .map(p => ({ p, r: engRate(p) }))
      .filter(x => x.r)
      .sort((a, b) => a.p.Published < b.p.Published ? 1 : -1);   // newest first
    const blocks = [];
    for (let i = 0; (i + 1) * sampleSize <= rated.length && i <= BASELINE_PERIODS; i++) {
      blocks.push(rated.slice(i * sampleSize, (i + 1) * sampleSize));
    }
    if (blocks.length >= 2) {
      const rateOf = b => mean(b.map(x => x.r));
      const dates = blocks[0].map(x => x.p.Published).sort();
      metrics.push({
        key: "engagementRate", platform: plat, label: "engagement rate",
        delta: pctChange(rateOf(blocks[0]), median(blocks.slice(1).map(rateOf))),
        baselineN: blocks.length - 1,
        sample: { n: sampleSize, from: dates[0], to: dates[dates.length - 1] },
      });
    } else if (rated.length) {
      // Some rated posts exist, just not enough blocks yet — a real "wait for
      // more data". A platform with NO rated posts at all (Facebook, whose
      // post-level reach Meta removed) gets no note: that is a permanent
      // platform limitation documented in the README, not a freshness issue.
      notes.push(`${plat}: not enough finished posts yet to judge engagement`);
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

  /* Always state what the engagement figure was actually measured on, and how
   * many posts are still too new to count. Shown every time rather than only
   * on failure, so the number is never a black box. */
  for (const m of metrics.filter(m => m.sample)) {
    notes.push(`${m.platform} engagement: last ${m.sample.n} finished post`
             + `${m.sample.n > 1 ? "s" : ""} (${fmtDate(m.sample.from)} – ${fmtDate(m.sample.to)})`);
  }
  const fresh = posts.filter(p => p.Published && p.Published <= to
                 && daysBetween(p.Published, to) < POST_MATURITY_DAYS).length;
  if (fresh) {
    notes.push(`${fresh} post${fresh > 1 ? "s" : ""} from the last `
             + `${POST_MATURITY_DAYS} days still gaining — not counted yet`);
  }

  if (!metrics.length) {
    return { badge: "unknown", emoji: "⚪", text: "Not enough data yet",
             sentence: "There isn't enough history yet to judge performance — "
                       + "check back after a few more days of collection.",
             metrics: [], silent, notes, range: rangeKey, from, to };
  }

  /* Two numbers per metric:
   *   score — green / yellow / red, used for the chips and the green gate
   *   norm  — how far it moved, -1..1, used for the weighted total
   * Scoring on norm rather than the flat ±1 keeps the badge proportionate: a
   * metric 11% below its usual should not weigh the same as one 80% below. */
  let weighted = 0, totalWeight = 0;
  for (const m of metrics) {
    const w = (VERDICT_WEIGHTS[m.key] || 0) / metrics.filter(x => x.key === m.key).length;
    m.score = m.forcedScore !== undefined ? m.forcedScore : classify(m.delta);
    const full = 2 * TREND_THRESHOLD_PCT;   // movement counted as "all the way"
    m.norm = Math.max(-1, Math.min(1, (m.delta || 0) / full));
    // a metric inside the flat band contributes nothing either way
    if (m.score === 0) m.norm = 0;
    m.weight = w;
    m.impact = Math.abs(m.norm) * w;        // share of the badge this drove
    weighted += m.norm * w;
    totalWeight += w;
  }
  const result = totalWeight ? weighted / totalWeight : 0;
  /* "Severe" means the channel is genuinely going backwards, not merely
   * growing more slowly than usual. A page that added 10 followers instead of
   * its usual 34 is still growing, and calling that severe is how a dashboard
   * ends up shouting Underperforming at a healthy month. */
  const severe = metrics.some(m => {
    if (m.score !== -1) return false;
    // shedding a couple of followers out of thousands is noise, not severity
    if (m.key === "followerGrowth") return m.counts.growth < -FOLLOWER_NOISE_FLOOR;
    return m.delta < -2 * TREND_THRESHOLD_PCT;
  });
  const anyRed = metrics.some(m => m.score === -1);

  /* Symmetric dead zone: a score hovering either side of zero is "Mixed".
   * Without this, a barely-negative result (one soft metric among several
   * healthy ones) reads as "Underperforming", which overstates the case. */
  let badge, emoji, text;
  if (result > VERDICT_DECISION_MARGIN && !anyRed) {
    badge = "green"; emoji = "🟢"; text = "On track";
  } else if (severe && result < -VERDICT_DECISION_MARGIN) {
    badge = "red"; emoji = "🔴"; text = "Underperforming";
  } else {
    badge = "yellow"; emoji = "🟡"; text = "Mixed";
  }

  return { badge, emoji, text, metrics, silent, notes, result, range: rangeKey, from, to,
           sentence: verdictSentence(metrics, rangeKey, silent, badge) };
}

/* One plain sentence explaining the badge. Deliberately describes the
 * CHANNEL's performance, never a person's — this text may be screenshotted
 * and forwarded without context. */
function verdictSentence(metrics, rangeKey, silent = [], badge = "yellow") {
  const baseline = RANGES[rangeKey].baselineLabel;
  const describe = m => {
    // Follower growth speaks in counts — a percentage between two small net
    // changes is misleading (see FOLLOWER_NOISE_FLOOR).
    if (m.key === "followerGrowth") {
      const { growth, prevGrowth } = m.counts;
      const gaining = growth >= 0, usuallyGaining = prevGrowth >= 0;
      const n = Math.abs(growth);
      const head = `${m.platform} ${gaining ? "gained" : "lost"} ${n} `
                 + `follower${n === 1 ? "" : "s"}`;
      /* Spell out the baseline's direction whenever it differs from this
       * period's, or "lost 1 follower (usual 5)" reads as though losing five
       * were the norm. When both point the same way the bare number is
       * unambiguous and less clumsy. */
      return gaining === usuallyGaining
        ? `${head} (usual ${Math.abs(prevGrowth)})`
        : `${head} (usually ${usuallyGaining ? "gains" : "loses"} ${Math.abs(prevGrowth)})`;
    }
    if (m.score === 0) return `${m.platform} ${m.label} is flat`;
    const pct = Math.min(Math.abs(m.delta), MAX_DISPLAY_PCT);
    const over = Math.abs(m.delta) > MAX_DISPLAY_PCT ? "over " : "";
    return `${m.platform} ${m.label} is ${m.score > 0 ? "up" : "down"} `
         + `${over}${pct.toFixed(0)}%`;
  };
  /* The sentence has to justify the badge. Picking the two biggest movers
   * regardless of sign meant a red badge could be explained entirely with
   * good news, leaving the reader to wonder what was actually wrong — and
   * hiding whichever platform was dragging. So: a red badge leads with what
   * pulled it down, a green one with what lifted it, and Mixed shows one of
   * each so the word means something. */
  /* Rank by how much each metric actually moved the badge. Ranking on raw
   * figures compared follower counts against percentages — different units,
   * so a 6-follower swing could outrank a 17% move purely by being a bigger
   * number. */
  const byImpact = arr => [...arr].sort((a, b) => (b.impact || 0) - (a.impact || 0));
  const downs = byImpact(metrics.filter(m => m.score < 0));
  const ups = byImpact(metrics.filter(m => m.score > 0));
  const flats = byImpact(metrics.filter(m => m.score === 0));

  let picked;
  if (badge === "red") {
    picked = [...downs, ...ups, ...flats].slice(0, 2);
  } else if (badge === "green") {
    picked = [...ups, ...downs, ...flats].slice(0, 2);
  } else {
    // Mixed: show the tension explicitly — best thing and worst thing.
    picked = downs.length && ups.length ? [ups[0], downs[0]]
           : byImpact(metrics).slice(0, 2);
  }
  let s = picked.map(describe).join("; ") + `, compared with ${baseline}.`;
  if (silent.length) {
    s += ` ${silent.join(" and ")} had no measurable change in this window.`;
  }
  return s;
}

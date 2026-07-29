// PUBLIC file — served to every dashboard visitor.
// This token is deliberately READ-ONLY (data.records:read) and scoped to only
// this Airtable base, and is owned by the shared office Airtable account so it
// does not depend on any individual. Never put a write-capable token here:
// selfcheck.py fails the build if this ever matches the collector's token.
window.DASHBOARD_CONFIG = {
  AIRTABLE_TOKEN: "patLaoi16ffM5Xz9A.8bbb3dc16c5a9a6e9dab0df271939fe9024f8059899c05cd3559eedf8e525207",
  AIRTABLE_BASE_ID: "appZskf5zx6ewhjMW",
};

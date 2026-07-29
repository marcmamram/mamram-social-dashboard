// PUBLIC file — served to every dashboard visitor.
// This token is deliberately READ-ONLY (data.records:read) and scoped to only
// this Airtable base, and is owned by the shared office Airtable account so it
// does not depend on any individual. Never put a write-capable token here:
// selfcheck.py fails the build if this ever matches the collector's token.
window.DASHBOARD_CONFIG = {
  AIRTABLE_TOKEN: "patjqvXIIjcj8i4Sl.21fe163bd8cb5fdd2cc0670e038d4a733df3abd94a6bbd5bc5d8b5c2e6bac346",
  AIRTABLE_BASE_ID: "appZskf5zx6ewhjMW",
};

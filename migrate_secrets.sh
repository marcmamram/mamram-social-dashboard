#!/usr/bin/env bash
# Copy every required secret from the local .env into a GitHub repository.
#
# GitHub does NOT carry Actions secrets across a repository transfer, so after
# moving the repo the workflow will fail until these are re-created. Doing it
# by hand means pasting seven long tokens into a web form; this does it in one
# command and never prints a value to the screen.
#
#   ./migrate_secrets.sh <owner>/<repo>
#
# Run it while `gh` is authenticated as an account with admin on the TARGET
# repo (i.e. the new owner). Check with:  gh auth status
set -euo pipefail

REPO="${1:-}"
if [ -z "$REPO" ]; then
  echo "usage: ./migrate_secrets.sh <owner>/<repo>" >&2
  echo "example: ./migrate_secrets.sh mamram-office/mamram-social-dashboard" >&2
  exit 1
fi

ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.env"
[ -f "$ENV_FILE" ] || { echo "ERROR: no .env next to this script ($ENV_FILE)" >&2; exit 1; }

REQUIRED=(META_ACCESS_TOKEN FB_PAGE_ID IG_BUSINESS_ACCOUNT_ID
          META_APP_ID META_APP_SECRET AIRTABLE_TOKEN AIRTABLE_BASE_ID)

echo "Target repository: $REPO"
echo "Authenticated as:  $(gh api user --jq .login 2>/dev/null || echo '(gh not logged in)')"
echo

# Confirm we can actually write secrets there before touching anything.
if ! gh api "repos/$REPO" --silent 2>/dev/null; then
  echo "ERROR: cannot see $REPO. Is the name right, and is gh logged in as an" >&2
  echo "       account with access?  Run: gh auth login" >&2
  exit 1
fi

set -a; . "$ENV_FILE"; set +a

missing=()
for name in "${REQUIRED[@]}"; do
  [ -n "${!name:-}" ] || missing+=("$name")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "ERROR: these are missing from .env: ${missing[*]}" >&2
  exit 1
fi

for name in "${REQUIRED[@]}"; do
  # --body reads the value directly; nothing is echoed
  printf '%s' "${!name}" | gh secret set "$name" --repo "$REPO" >/dev/null
  echo "  set $name"
done

echo
echo "All ${#REQUIRED[@]} secrets set on $REPO."
echo "Next: Actions tab -> 'Collect social metrics' -> Run workflow, and confirm it is green."

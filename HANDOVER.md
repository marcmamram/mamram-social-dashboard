# Handover: moving everything to the office account

One-time migration. Follow in order — later steps depend on earlier ones.
Budget about 45 minutes. Nothing here is reversible-by-accident, but the one
genuinely disruptive change is flagged in step 1.

The destination is the office account, **office@mamramalumni.org.il**.
Throughout, replace `OFFICE` with that account's GitHub **username** (not the
email — GitHub transfers are by username).

---

## Before you start — what actually changes

**The dashboard address will change.** It is currently

    https://marcmamram.github.io/mamram-social-dashboard/

and the owner's username is part of it. After the transfer it becomes

    https://OFFICE.github.io/mamram-social-dashboard/

**The old link will stop working.** Anyone who has bookmarked it — management
especially — needs the new one. Send it round *after* step 4 confirms the new
address is live, not before.

**What does survive the transfer** (verified on the real migration, 29 Jul
2026): the Actions secrets, the Pages site, the workflow history, and the old
Pages address, which kept working and served identical content. Do not assume
any of that — *check* it, per steps 3 and 4, and only act if something is
actually missing.

---

## Step 1 — Transfer the GitHub repository

1. Sign in as **the current owner** (`marcmamram`).
2. Go to the repository → **Settings** → scroll to **Danger Zone** →
   **Transfer ownership**.
3. Enter the new owner: `OFFICE`. Confirm by typing the repository name.
4. GitHub sends `OFFICE` an invitation to accept the transfer. **Sign in as
   the office account and accept it.** Nothing moves until it is accepted.

The repository is now `OFFICE/mamram-social-dashboard`. Old repository links
redirect automatically; the *Pages* address does not.

> **Why a shared account rather than an Organisation.** A repository owned by
> a personal account has exactly one administrator, and Admin cannot be
> delegated to a collaborator. That is fine here *by design*: the office
> account is shared, and everyone who needs to maintain this has its
> credentials. It does mean maintenance is done **signed in as the office
> account**, not as an individual — including renewing the Meta token and
> editing secrets.

## Step 2 — Point your local copy at the new home

On the machine that has this project checked out:

```bash
git remote set-url origin https://github.com/OFFICE/mamram-social-dashboard.git
git remote -v          # confirm it shows OFFICE
```

## Step 3 — Check the Actions secrets

First look: repository → **Settings → Secrets and variables → Actions**. There
should be seven: `META_ACCESS_TOKEN`, `FB_PAGE_ID`, `IG_BUSINESS_ACCOUNT_ID`,
`META_APP_ID`, `META_APP_SECRET`, `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`.

In the real migration they came across intact and nothing was needed here. If
any are missing, restore them all in one command from this project folder,
signed in as the office account (`gh auth login`, `gh auth status` to confirm):

```bash
./migrate_secrets.sh OFFICE/mamram-social-dashboard
```

It reads the values from your local `.env` and never prints them.

## Step 4 — Check the website

Open `https://OFFICE.github.io/mamram-social-dashboard/`. In the real
migration Pages rebuilt itself automatically and this was already live.

If it 404s: repository → **Settings → Pages** → *Build and deployment* →
**Source: Deploy from a branch**, **Branch: `main`**, **Folder: `/docs`**,
Save, and wait a couple of minutes.

Once it shows the status badge and a sentence, send the new address to whoever
used the old one. The old address happened to keep working too, but do not
rely on that — it is not a documented guarantee and could stop at any time.

## Step 5 — Move Airtable onto the office account

The office account already has access to the base, so no data needs moving.
What must change is **who owns the two API tokens** — they currently belong to
a personal Airtable account and will stop working when it goes, taking both
the collector and the public dashboard with them.

Signed in to Airtable **as the office account**:

1. Confirm the base sits in a workspace the office account owns
   (Workspace → **Share** → the office account should be *Owner*). If it is
   not, move the base: base menu → **Move base** → choose that workspace.
2. Create the **collector token** at <https://airtable.com/create/tokens>:
   - Name: `mamram-collector`
   - Scopes: `data.records:read`, `data.records:write`, `schema.bases:read`
   - Access: this base only
3. Create the **dashboard token**, separately:
   - Name: `mamram-dashboard-public`
   - Scopes: **`data.records:read` only** — nothing else
   - Access: this base only

   > This one is embedded in a public web page and is readable by anyone who
   > visits. That is acceptable *only* because it can do nothing but read this
   > one base. Never give it write access.

4. Put the collector token into the repository secret `AIRTABLE_TOKEN`
   (Settings → Secrets and variables → Actions → update).
5. Put the dashboard token into `docs/config.js`, replacing the existing
   value, then commit and push:

   ```bash
   git add docs/config.js && git commit -m "Use office-owned Airtable token" && git push
   ```

6. Once step 7 confirms everything still works, **delete the two old tokens**
   from the personal Airtable account so nothing depends on it any more.

## Step 6 — Meta: nothing to move, but one person to brief

**Nothing on the Meta side belongs to the departing developer.** Checked on
29 Jul 2026: the app "Mamram Social" (`839252902454928`) has exactly one
administrator — **Bar Turgeman**, the social media manager — and the access
token in use was granted by her Facebook account. She is staying, so no
migration is required.

What she does need to know:

- **She renews the Meta access token, start to finish.** When the daily run
  starts failing with a token-deadline message (expected around **12 October
  2026**, and roughly every 90 days after), the steps are in *Renewing the
  Meta token* in `README.md`. Bar can do the whole thing alone: she is the app
  administrator, so she can generate the token, and she has the office login,
  so she can paste it into the **`META_ACCESS_TOKEN`** repository secret and
  re-run the workflow.
- **The app still has a single administrator, and shared logins do not fix
  that.** App administrator rights belong to Bar's *personal Facebook account*
  (`10239184754749211`), not to any email address. Sharing the office mailbox
  or the GitHub login gives nobody else the ability to issue a Meta token. If
  that Facebook account is ever lost or leaves the organisation, the app is
  orphaned: no new token can be created through it, and collection cannot be
  restored without registering a new Meta app and re-approving its permissions.

  The fix is five minutes and only Bar can do it: add a second **Administrator**
  at <https://developers.facebook.com/apps/839252902454928/roles/>. Not urgent
  — it costs nothing while she is here — but it is the one failure this project
  cannot recover from on its own.

## Step 7 — Verify the whole chain

1. Repository → **Actions** → **Collect social metrics** → **Run workflow**.
2. It should finish green, with all of *Run collector*, *Generate weekly
   takeaway* and *Self-check* passing.
3. Open `https://OFFICE.github.io/mamram-social-dashboard/` — the badge and
   sentence should appear, and **View details →** should load the charts.
4. Open the Airtable base: **Snapshots** should have two rows dated today.

If all four are true, the migration is complete and nothing depends on the
outgoing owner's accounts.

## Step 8 — Tidy up

- Update the "Read this first" section of `README.md` to name the new owner.
- Remove the outgoing owner as a collaborator if they are still listed.
- Keep `.env` only if someone still needs to run the scripts locally; it holds
  live credentials, so store it somewhere the association controls.

---

## If something breaks

| Symptom | Cause | Fix |
|---|---|---|
| Workflow fails right after transfer | Secrets did not transfer | Step 3 |
| Dashboard shows "Could not load data from Airtable" | `docs/config.js` token is wrong, revoked, or not read-only | Step 5.3 and 5.5 |
| Dashboard shows "not configured" | `config.js` still has the placeholder | Step 5.5 |
| Old dashboard link 404s | Expected — the address changed | Send the new one (step 4) |
| Workflow fails with "META TOKEN DEADLINE" | Meta permission is lapsing | *Renewing the Meta token* in `README.md` |
| Anything else | Read the first red step in the failed run | It explains itself in plain English |

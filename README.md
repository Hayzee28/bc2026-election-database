# BC 2026 Local Elections Candidate Database

Static Cloudflare Pages site with an automatic Elections BC candidate-data updater.

## What the robot does

Every hour GitHub Actions checks the official Elections BC candidate register. The updater parses all pages twice using two independent extraction paths, requires both passes to agree exactly, validates the structure, and compares the result with the currently published dataset.

- **No real candidate-data change:** no commit, so Cloudflare does not rebuild.
- **Valid candidate-data change:** `data.json` and the downloadable CSV are updated and committed. Cloudflare Pages sees the push and redeploys.
- **Parser/source anomaly:** the workflow fails before touching the published files. The last known-good site stays live.

The public dataset includes the financial agent **name**, but does **not** contain financial-agent service addresses.

## Built-in fail-safes

The updater blocks publication if its two extraction passes disagree, or if it sees a missing/repositioned table header, unexpected PDF dimensions/page count, too few rows, malformed required fields, duplicate candidates, a large candidate-count jump/drop, unusually low overlap with the prior candidate list, unusually low jurisdiction overlap, or any address field in public output.

Existing `Result`, `Elected`, `Result Source`, and `Notes` values are preserved when a matching candidate is refreshed.

## GitHub setup

1. Create a new GitHub repository.
2. Upload **everything in this folder**, including `.github`, `scripts`, and `requirements.txt`.
3. Use `main` as the repository's default branch.
4. In **Settings → Actions → General**, allow Actions to run. The workflow already requests `contents: write` so the bot can commit validated updates.
5. Open **Actions → Update Elections BC candidates → Run workflow** once. A clean run should finish green and, if Elections BC has not changed, make no commit.

The scheduled check runs at minute 17 of every hour.

## Cloudflare Pages setup

Connect the GitHub repository to Cloudflare Pages and use:

- Production branch: `main`
- Framework preset: none
- Build command: `exit 0` (blank also works for a no-framework site)
- Build output directory: `.`
- Root directory: repository root

Cloudflare Pages automatically deploys when the production branch receives a push.

### Optional: avoid irrelevant Cloudflare builds

In Cloudflare Pages **Settings → Build → Build watch paths**, include only the public site files:

- `index.html`
- `styles.css`
- `app.js`
- `data.json`
- `bc-2026-candidates.csv`

That way edits to the updater script, workflow, or README do not spend a Pages build unless a public-site file changes.

## Manual parser self-test

From the repository root:

```bash
pip install -r requirements.txt
python scripts/update_candidates.py --pdf Registered-Candidates-LEGE-2026-10-17.pdf --expect-exact-current
```

The baseline package was tested against the Sep. 21, 2026 Elections BC register and its known-good 3,479-row dataset.

## Source

Elections BC candidate register:
https://elections.bc.ca/docs/lecfa/Registered-Candidates-LEGE-2026-10-17.pdf

Candidate information page:
https://elections.bc.ca/local-elections/candidate-information/

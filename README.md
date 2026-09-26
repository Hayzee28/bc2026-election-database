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

## Candidate change investigation

The updater remains the gate: two independent PDF passes and the existing public-file validation must pass before analysis runs. The hourly schedule remains at minute 17. A separate local-source workflow checks configured official pages once daily at 13:43 UTC. Both workflows share a concurrency group and commit only their recorded output changes.

Run locally:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/investigate_candidates.py
python scripts/validate_investigation.py
python scripts/watch_local_sources.py --discover
python scripts/check_official_explanations.py
python scripts/investigate_candidates.py
```

The initial `python scripts/investigate_candidates.py --backfill` reconstructed five distinct committed `data.json` versions. That flag is only needed when establishing an archive from an existing Git repository. It refuses to fill missing revisions. Backfilled entries are marked `reconstructed`; historical full PDF hashes/page counts that were never saved remain null. Future validated source revisions store the full source SHA-256 and page count. Approximate absence duration measures the gap between **observation timestamps**, not a known exact removal time. The source PDF can change with zero candidate changes and still receives a snapshot.

| File | Purpose |
|---|---|
| `candidate-change-history.json`, `candidate-source-state.json` | Existing updater audit and last observed PDF hash. |
| `source-history.json`, `source-snapshots/*.json.gz` | Append-only source revision index and full address-free candidate states. Each snapshot is immutable. |
| `candidate-timelines.json` | One identity's observed transitions across the available snapshots. `BASELINE_PRESENT` means earlier history is unknown. |
| `candidate-anomalies.json`, `candidate-anomaly-report.json` | Removals, returns, withdrawals and name corrections, plus batches and hourly/daily timing. |
| `jurisdiction-churn.json` | Raw change counts, distinct affected candidates and revision counts, ranked by change count. |
| `local-source-state.json`, `local-source-history.json` | Latest check and append-only checks of configured official local lists. A failed check preserves the last good names and hash but is marked `ERROR`. |
| `local-ebc-mismatches.json` | Literal case-insensitive name presence in each source for configured jurisdictions, with both observation times. Name spelling differences can produce two rows. |
| `official-explanations.json` | Candidate-specific official-page text matches. A match is a review clue, never proof that text explains a revision. |
| `reports/latest-candidate-anomalies.md`, `reports/local-source-discovery.json` | Readable factual report and unapproved official-host candidate URL suggestions. |

**FACT:** A record was present, absent, changed, or returned between two captured revisions. A local list had or lacked the exact name at its own check time. **INFERENCE:** A temporary publishing issue or local status change could fit those observations. Evidence indicators use `SUPPORTS`, `WEAKENS`, or `UNKNOWN`, without numerical probabilities. `NO PUBLIC EXPLANATION FOUND` means the configured official pages yielded no candidate-specific keyword match; it says nothing about why the list changed.

Identity uses jurisdiction, office, and candidate name without the `(Withdrawn)`/`(Acclaimed)` display suffix. A name correction links only a unique similar name with the same jurisdiction, office, and nonblank agent. Ambiguous links remain separate records. Returns from earlier captured snapshots are `RESTORED_AFTER_ABSENCE`, never `NEW`. A return compares name, jurisdiction, office, affiliation, agent and status; PDF page changes are not treated as candidate changes. Batches report counts and names from the same source revision; a central-wide label is an update-scope description, not a claim about who caused it.

### Add a local jurisdiction

Edit `config/local-sources.json` with the exact Elections BC jurisdiction name, a verified official `https` URL, `officialHosts`, and `type` (`HTML` or `PDF`). HTML sources require an explicit CSS `selector` or `sections` whose `heading` precedes a candidate table with names in its first column. PDF sources require a reviewed `linePattern` regex with a named `(?P<name>...)` capture. Set `minimumNames` to stop silently accepting a broken page. `officialWebsite` optionally provides same-host discovery suggestions for review; it never auto-enrolls a URL. Municipality, regional district and school district lists all use the same configuration format. Avoid mixing a municipal list with school-trustee rows under one jurisdiction: configure each source/section for its own Elections BC jurisdiction.

No financial-agent service addresses are stored in any generated file. The public site (`index.html`, `app.js`, `styles.css`) and its existing `data.json`/CSV schema are unchanged. Parsing failures fail the hourly workflow before a commit; the public branch keeps the last known-good files. Individual local-source errors do not block other local checks.

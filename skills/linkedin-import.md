# LinkedIn Import — export → memory database

Turn a LinkedIn data export into a local SQLite database your agent can reason over.
This is the ingest step; `/observatory` is the visual step.

Hand your agent a LinkedIn export and it will parse your connections, infer a
function and seniority for each person from their job title, and save everything
into one portable SQLite file on your machine.

## What you provide
A LinkedIn "Basic" export — any of:
- the `.zip` LinkedIn emails you,
- an unzipped export folder,
- or a direct path to `Connections.csv`.

To get your export: LinkedIn → Settings → Data Privacy → *Get a copy of your
data*. Your connections are in the "Basic" archive, which usually arrives within
minutes. If you drop the file into this repo's `exports/` folder, the script
finds it on its own.

## Run it
```bash
# from the repo root
python3 scripts/linkedin_import.py                 # auto-find the export
python3 scripts/linkedin_import.py path/to/export.zip
python3 scripts/linkedin_import.py --dry-run       # report only, write nothing
```

## Where it lands
- Database: `data/linkedin.db` (SQLite, one file — copy it, back it up, or query
  it directly).
- Table `connections`: name, url, email, company, raw title, the inferred `func`
  and `rank`, connection year/month, and timestamps.
- Table `import_runs`: one row per run, so re-imports stay auditable.

## How it behaves
- **Idempotent.** Keyed on the LinkedIn profile URL (name + company when the URL
  is missing). Re-run with a fresh export and people update in place instead of
  duplicating. Pull a new export whenever you like and run it again.
- **Honest inference.** Function (Engineering, Sales, Leadership, Founder, and so
  on) and seniority (Entry through Executive) are read from the job title with
  fixed rules. Nothing is invented, and the visual always marks these as
  *inferred*. Titles with no clear domain land in "Other," which is expected.
- The rules live in `scripts/linkedin_import.py` (`infer_func`, `infer_rank`) and
  are the shared logic both steps rely on.

## Next
Run `/observatory` to render the database into the star-map explorer.

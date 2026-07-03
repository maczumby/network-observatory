# Network Observatory — agent guide

This repo turns a person's LinkedIn export into a local database and a visual star
map of their professional network. It runs in two steps: **import** (export →
SQLite) and **observatory** (SQLite → a standalone HTML page).

If you are an AI agent working in this repo, this file tells you what to do.

---

## When the user gives you a LinkedIn export

They will hand you a LinkedIn "Basic" export — a `.zip`, an unzipped folder, or a
`Connections.csv` — often with a message like "set this up" or "show me my
network." Run the whole pipeline for them. Don't make them run commands one at a
time unless they ask to.

1. **Find the export.** If they attached or named a file, use that path. Otherwise
   the export is probably in `exports/`, the repo root, or their Downloads — the
   import script searches all of those on its own. If you can move the file, put
   it in `exports/` first.

2. **Import it.**
   ```bash
   python3 scripts/linkedin_import.py            # auto-finds the export
   # or: python3 scripts/linkedin_import.py <path>
   ```
   This writes `data/linkedin.db` and prints a summary (count, date span, top
   companies, function mix). Read the summary back to the user in plain language.

3. **Build the visual.**
   ```bash
   python3 scripts/observatory_export.py --open
   ```
   This writes `dashboard/observatory.html` and opens it in the browser. If
   `--open` can't launch a browser (headless machine), tell them the file path so
   they can open it themselves.

4. **Tell them what they're looking at.** Point out the three views (Field /
   Orbit / Strata), the color-by options, search, filters, the preset chips, and
   that clicking a star opens a detail panel where they can add a private note and
   flag someone to reconnect. Mention that function and seniority are *inferred
   from job titles*, not stated facts.

That's the default flow. Steps 2 and 3 are both quick and safe to run.

---

## Requirements

- **Python 3.8+.** No packages to install — the scripts use the standard library
  only. Do not add dependencies or a virtualenv unless the user asks.
- **A web browser** to view the output. The HTML is fully self-contained (fonts
  embedded, no external requests) and works offline; nothing is uploaded anywhere.

---

## Privacy — treat their data as private

The export, the database, and the generated HTML all contain personal contact
data. `.gitignore` already excludes `exports/*`, `data/*.db`, and
`dashboard/*.html`. Never commit those, never paste connection records into a
message to anyone but the user, and never send them to an external service. The
whole tool is designed to run locally for this reason.

---

## Re-running with a newer export

The import is idempotent — keyed on each person's LinkedIn URL. When the user gets
a fresh export later, run step 2 again (it updates people in place) and then step
3. Their saved notes and reconnect flags live in the browser and survive rebuilds.

---

## Optional: install as slash commands

For Claude Code users who want `/linkedin-import` and `/observatory` as reusable
commands, copy the skill files into their commands folder:
```bash
cp skills/linkedin-import.md skills/observatory.md ~/.claude/commands/
```
The scripts work fine on their own without this.

---

## How it's built (for when you need to change it)

- `scripts/linkedin_import.py` — parses the CSV, infers `func` and `rank` from the
  job title (`infer_func`, `infer_rank`), upserts into SQLite. This inference is
  the shared logic; the visual just displays what's stored.
- `scripts/observatory_export.py` — reads the DB, shapes each record, and injects
  the data into the template.
- `scripts/observatory/template.html` — the standalone visual. A canvas starfield
  engine plus a small vanilla reactive layer. Data arrives via a single
  `window.OBS_DATA` object the exporter fills in.
- To QA a change: rebuild, then open the HTML and check the Field / Orbit / Strata
  views and a detail panel for any text overflow or overlap.

Keep the data honest. Anything the code guesses (function, seniority) must stay
labeled as inferred in the interface.

**Scale and language.** The pipeline handles small networks up to ~12,000+
connections, and reads exports in any language (the CSV parser anchors on the URL
column, not English headers). The one English-tuned part is the role/seniority
guess: `infer_func` and `infer_rank` match English title keywords. If the user's
export is in another language and most people land in "Other," offer to add their
language's common title words (e.g. "directeur", "ingénieur", "ventes") to those
two functions. Everything else already works regardless of language.

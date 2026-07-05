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
   it in `exports/` first. You do **not** need to unzip it — the importer reads a
   `.zip` directly. On a hosted/chat agent, the user's uploaded file lands in your
   workspace; use it wherever the platform saved it.

2. **Import it.**
   ```bash
   python3 scripts/linkedin_import.py            # auto-finds the export
   # or: python3 scripts/linkedin_import.py <path>
   ```
   This writes `data/linkedin.db` and prints a summary (count, date span, top
   companies, function mix). Read the summary back to the user in plain language.

3. **Build and show the map.**
   ```bash
   python3 scripts/observatory_export.py
   ```
   This writes the self-contained `dashboard/observatory.html` and prints a short,
   plain-language summary you can share.
   - **On the user's own computer:** add `--open` to open it in their browser.
   - **On a hosted / cloud agent** (the user is in a chat and you can't reach their
     screen — e.g. Agent37/Hermes): do **not** try to open a browser, and do **not**
     hand over an internal IP or a `localhost`/preview URL. Those are unreachable or
     auth-gated, and you cannot mint a working link (signing needs the account's
     secret key, which the agent doesn't have). Instead, **send the
     `dashboard/observatory.html` file to the user in the chat** (or tell them to
     open it from the platform's Files browser). It's fully self-contained, so they
     just open it — nothing to install or unzip. Then **paste the summary** the
     exporter printed.

4. **What they can explore** is covered by that summary: three views (Field / Orbit
   / Strata), color by company / role / seniority / era, search, filters, and
   clicking a star for details, a private note, or a reconnect flag. Remind them
   that function and seniority are *inferred from job titles*, not stated facts.

When they later say "show me my map," just rebuild (step 3) and send the fresh file.

---

## Trellis — the relationship memory (third capability)

`scripts/trellis.py` is a local relationship memory on the same DB as the graph. It
remembers who people are, what the user owes them, and who's worth reaching out to —
every answer citing its source. Run it when the user asks relationship questions:

- "who is X / when did we last talk / what do I owe them / who do I know at Y" →
  `python3 scripts/trellis.py recall "<query>"`
- "log this" / after a meeting or intro → `trellis.py capture --name … [--interaction …
  --loop … --note … --priority … --mode …]`. You turn the user's words into the flags;
  Trellis just writes.
- "who did I leave hanging / who should I reach out to" → `trellis.py loops` / `radar`.
  Read the reason lines back; if radar is quiet, say so — don't invent reasons.
- "help me write to X" → `trellis.py context --name X`, then draft **only** from those
  facts. Never invent shared history. **Never send** — draft for the user to review.

**Source-adaptive.** If you have email/calendar/meeting tools connected, enrich Trellis
by normalizing each item to an event and calling `trellis.py ingest` (idempotent on
`source_ref`). Trellis never fetches or stores tokens — that's your job with your own
tools. With nothing connected, recall + loops still work from the LinkedIn graph.

**Map ↔ Trellis.** The Observatory reflects Trellis (flagged people highlighted, notes
pre-filled). When the user flags/notes people in the map and pastes the "Sync to your
agent" block to you, fold it in with `trellis.py apply --json '…'` (or `--file`).

Keep the trust contract: cite sources, show the reason, never invent, confirm
duplicates (`trellis.py dupes` / `merge`), never auto-send.

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

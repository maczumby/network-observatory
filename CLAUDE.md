# Network Observatory — agent guide

This repo turns a person's LinkedIn export into a local database and a visual star
map of their professional network. It runs in two steps: **import** (export →
SQLite) and **observatory** (SQLite → a standalone HTML page).

If you are an AI agent working in this repo, this file tells you what to do.

---

## The runbook: LinkedIn export → map → shared link → community querying

The user will hand you a LinkedIn "Basic" export — a `.zip`, an unzipped folder,
or a `Connections.csv` — often with "set this up" or "show me my network."

**Do these steps in order, one at a time. At each ✋ checkpoint, stop and wait for
the user before continuing. Don't run ahead or batch steps.** Later steps are
optional and build on the earlier ones, so the map always comes first.

### Step 1 — Build the map

1. **Find the export.** If they attached or named a file, use that path. Otherwise
   it's probably in `exports/`, the repo root, or their Downloads — the import
   script searches all of those. If you can move it, put it in `exports/` first.
   You do **not** need to unzip it. On a hosted/chat agent, the uploaded file lands
   in your workspace; use it wherever the platform saved it.
2. **Import it.** `python3 scripts/linkedin_import.py` (or pass the path). This
   writes `data/linkedin.db` and prints a summary.
3. **Build it.** `python3 scripts/observatory_export.py` writes the self-contained
   `dashboard/observatory.html`.

✋ **Checkpoint:** confirm the map built, and read the printed summary back to the
user in plain language (count, span, top companies). Remind them function and
seniority are *inferred from job titles*, not stated facts.

### Step 2 — Publish it (get them a link)

- **On the user's own computer:** re-run with `--open`
  (`python3 scripts/observatory_export.py --open`) to open it in their browser.
  You're done — skip to step 4 if they want it.
- **On a hosted / cloud agent (Agent37 / Hermes),** the map needs a public link:

  a. **Serve it.** `python3 scripts/serve.py` serves `dashboard/` on port **8766**.
     Run it in the background so it keeps serving after this step.

  b. **Expose the port.** Give port 8766 a public URL using agent37's in-VM host
     helper. On current builds that's:
     ```bash
     agent37-host_add 8766
     ```
     It returns a public URL shaped like
     `https://exposed-port-8766-<hash>.h48.openclaw.agent37.com/observatory.html`.
     The `<hash>` is unique to *this* instance and minted by the command, so use
     the exact URL it returns — never copy an example or someone else's link.
     Agent37's tooling changes and this exact command isn't in their published
     docs, so if it's missing or named differently, check
     `agent37-host_add --help` or agent37's docs
     (https://agent37.com/docs/agents-api/public-ports) for the current way to
     expose a port. Whatever you use, the verify step next is what proves you got
     a real, working link — don't skip it.

  c. **Verify before you send.** Never hand over a link you haven't confirmed loads:
     ```bash
     curl -sS -o /dev/null -w "%{http_code}\n" "<url>/observatory.html"
     ```
     Require `200`. If it's not 200, fix the path or re-expose and try again.

  d. **Send it as a hyperlink, never the raw URL.** Post a Markdown link:
     `[Open Network Observatory](<url>/observatory.html)`.

  ✋ **Checkpoint:** confirm they can open it. Note plainly that right now the link
  is public — anyone who has it can see the map. That sets up step 3.

### Step 3 — Offer a password (optional)

Ask if they want to lock the link. If **yes**, restart the server with a password
and re-verify:
```bash
python3 scripts/serve.py --password "<user>:<pass>"
curl -sS -o /dev/null -w "%{http_code}\n" -u "<user>:<pass>" "<url>/observatory.html"   # expect 200; without -u, 401
```
Give them the username and password **privately** (backchannel or DM), never in a
shared channel.

✋ **Checkpoint:** act on their yes/no before moving on. If no, leave it open but
make sure they heard that it's public.

### Step 4 — Let other people query the network (optional)

Ask if they'd like people in their channels to be able to ask *their agent* about
their network ("does she know anyone in AI enablement?"). If **yes**, hand them
`skills/network-answers.md` and walk them through pasting it into their agent's
backchannel. It answers high-level questions and suggests profiles with links,
and routes anything sensitive back to them first.

✋ **Checkpoint:** done when they've pasted it and their agent has read it back.

When they later say "show me my map," rebuild (step 1) — the served link stays the
same, so you don't need to re-expose or re-send it.

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
pre-filled). When the user flags/notes people in the map, its "Sync to your agent" panel
gives them a plain-text block (a list of people to flag + notes) that they paste into
the chat. Read that block, then save each person — call `trellis.py capture` per person
(`--priority important` for a flag, `--note` for a note) or build the JSON and call
`trellis.py apply`. It's a text instruction, not a file; no JSON required from the user.

Keep the trust contract: cite sources, show the reason, never invent, confirm
duplicates (`trellis.py dupes` / `merge`), never auto-send.

## Updating the tool

When the user says "update the network-observatory tool" (or asks for the latest):

```bash
python3 scripts/update.py                 # auto: git pull if a clone, else download latest
python3 scripts/update.py --from-zip PATH # if the user sent you a new zip
```

It fetches the latest code, copies it over scripts/skills/docs, and rebuilds the map.
It **never touches `data/`, `exports/`, or `dashboard/`** — the user's graph, flags, and
notes are safe — and the DB only ever adds tables, so a newer version keeps working with
existing memory. The `VERSION` file shows what's installed; tell the user the old→new
version after updating. You can't be *pushed* updates on a hosted VM — this is always
pull-on-request.

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
message to anyone but the user, and never send them to an external service.

**Publishing the map (step 2) is the one deliberate exception, and it's opt-in
per share.** When you expose the map on a public port, anyone with that link can
see it, so *tell the user that plainly* when you send it, and offer the password
(step 3) as the real lock. Exposing the built HTML is the only thing that leaves
the machine — the raw export, the database, and connection records still never
get committed, pasted to anyone but the user, or sent to a third-party service.
Step 4 is narrower still: the agent surfaces public profile links in reply to a
question, never the underlying database.

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
- To QA a change: rebuild, then open the HTML and check the Groups / Timeline / Ranked
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

# Network Observatory — agent guide

This repo turns a person's LinkedIn export into a local database, and that database
into a small personal CRM: a visual star **map** of their network, a **warmth**
table showing how alive each relationship is, and a **People** workbench for
marking, snoozing, and reconciling contacts. Import first (export → SQLite), then
build the screens (SQLite → standalone HTML pages).

If you are an AI agent working in this repo, this file tells you what to do.

**Before changing anything that touches people, identity, or status, read
[`docs/DATA_INTEGRITY.md`](docs/DATA_INTEGRITY.md).** It is the contract the code
follows: one canonical person, `people_v` as the only trusted read,
propose-confirm-reverse for identity, owner exclusion, the single meeting model,
and the two-layer status model.

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
   `dashboard/observatory.html`. Once there's any contact signal, build the other
   two screens as well — they share one design and link to each other:

   ```bash
   python3 scripts/warmth_export.py       # dashboard/warmth.html
   python3 scripts/reconcile.py           # identity proposals for the People screen
   python3 scripts/workbench_export.py    # dashboard/workbench.html  ("People")
   ```

✋ **Checkpoint:** confirm the map built, and read the printed summary back to the
user in plain language (count, span, top companies). Remind them function and
seniority are *inferred from job titles*, not stated facts. Then teach by
example — offer three starter questions: "Who do I know at <a real company
from their map>?", "Who have I gone cold on?" (needs Gmail), and "Remind me
to follow up with someone."

**Every answer about the graph follows the answer contract** in
`skills/network-observatory/SKILL.md` ("How to talk about the graph"): one
plain sentence first, people hyperlinked to LinkedIn, warmth claims dated,
no tool jargon, one offer at the end.

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

Ask if they want to lock the link. If **yes**, set the password once and restart
the server — after that, every restart stays locked with no flags:
```bash
python3 scripts/serve.py --set-password "<pass>"
```
Visitors get a normal login page (paste and password managers work; no browser
popup, no username). Verify the lock before sending the link:
```bash
curl -sS "<url>/observatory.html" | grep -c "This map is private"   # expect 1
```
Give them the password **privately** (backchannel or DM), never in a shared
channel. To change it, run `--set-password` again; to remove it,
`--clear-password`.

✋ **Checkpoint:** act on their yes/no before moving on. If no, leave it open but
make sure they heard that it's public.

**Editing from the screens (optional, local).** By default the served pages are
read-only: marking someone on a screen saves it in that browser and gives them a
sync block to paste to you. If the user wants the controls to write straight to
their memory instead, serve with `--rw`:

```bash
python3 scripts/serve.py --rw                  # requires a saved password
python3 scripts/serve.py --rw --rw-local-only  # they can edit; viewers can only look
```

Say the trade-off plainly before turning it on: **anyone with the viewing
password can also change their data unless `--rw-local-only` is set.** Never
enable `--rw` on a link they're sharing with a group without asking first.

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
  Radar orders what it finds: a follow-up date the user set (110), then a loop
  they owe and are **late** on (100), then **someone they met recently and
  haven't spoken to since** (90), then a loop with no deadline (85), then
  cadence. A meeting outranks an undated loop on purpose — the follow-up decays
  in about three weeks while an undated loop doesn't, and a handful of open
  loops shouldn't push a fresh meeting off the default five-item list. That
  third one is what calendar data buys — see below.
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
duplicates (`trellis.py dupes` / `merge`), keep identity decisions reversible
(`trellis.py merges` / `unmerge`), never auto-send.

### Prioritizing and snoozing people

Two independent layers — importance and time — both driven from natural language:

```bash
trellis.py capture --name "Ada" --prioritize            # star them everywhere
trellis.py capture --name "Ada" --deprioritize          # hide from warmth/radar (reversible)
trellis.py capture --name "Ada" --follow-up "in 6 months" --follow-up-reason "after their launch"
trellis.py capture --name "Ada" --clear-follow-up
```

"Remind me about them in a week", "park this one for six months", "stop showing me
this person" all land here. A **due follow-up fires even for a deprioritized
person** — that's the point of two layers — and `radar` lists due follow-ups
first. Never clear a follow-up on the user's behalf; the date is theirs.

### After a meeting

Once calendar events are ingested, `radar` surfaces anyone met in the last
three weeks with no contact since — *"you met Ada 6 days ago and haven't been
in touch since"*. Offer the two useful moves: a follow-up date
(`--follow-up "in 1 week"`) or a draft built only from stored facts
(`trellis.py context --name X`). Never send it.

It deliberately stays silent when they've already been in touch (including the
same day), when another meeting is booked, when the user set their own date,
when the person is deprioritized, and after three weeks — a nudge that fires
forever is one people learn to ignore.

### Keeping the graph honest

```bash
python3 scripts/reconcile.py            # regenerate the review queues (changes nothing)
python3 scripts/reconcile.py --apply    # mute owner addresses + confident non-people
python3 scripts/calendar_crm.py --file events.json   # meetings: who/when only
```

`reconcile.py` writes `data/linkedin_identity_review_queue.json` (email contacts
that look like LinkedIn people you already know) and
`data/contact_quality_review.json` (addresses that may not be people). Bring the
**ambiguous** ones to the user with the evidence — don't guess. Identity merges are
never applied automatically, in either mode.

**Run `reconcile.py` before `workbench_export.py`.** The People screen reads that
queue rather than recomputing it, because matching is a whole-graph pass and a
page rebuild shouldn't pay for it. If the screen shows no "same person as…?"
proposals, the queue hasn't been generated since the last sweep — run reconcile,
then rebuild.

Set up `data/owner_identities.json` early (their own addresses, plus
`owner_domains` for teammates) — otherwise the user's own inbox becomes part of
their network. See `docs/DATA_INTEGRITY.md`.

## Email recency — the Gmail sweep (fourth capability)

`scripts/email_recency.py` answers "when did I last exchange email with X" straight
from Gmail headers — see `skills/email-recency.md` for the full contract. It is the
one component here that holds a token, deliberately the weakest one Gmail issues
(`gmail.metadata`): Google's servers refuse it message bodies outright. Run it for
"when did I last talk to / who's gone cold / triage my leads" questions:

```bash
python3 scripts/email_recency.py "sam@example.com" --json
python3 scripts/email_recency.py --from-db --json   # LinkedIn connections with emails
```

Read `skills/email-recency.md` before first use — it covers the sweep-completeness
check, the substring-match caveat (always name `matched_address` in your answer),
and when subjects may be pulled. First run needs an OAuth client JSON at
`data/gmail_oauth_client.json` and opens a one-click browser consent.

**Hosted agents (Hermes/Agent37): skip the local OAuth script.** Prefer the
Network Observatory Connect service described in `skills/network-observatory/SKILL.md`.
It creates one private, revocable MCP endpoint per tester through the operator's
custom Composio Gmail auth config. The project credential stays on the server,
and the endpoint returns only allowlisted sender, recipient, date, label, and ID
metadata. Feed those events to `trellis.py ingest` as described in
`skills/email-recency.md`.

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
3. Notes and reconnect flags saved in the browser survive rebuilds (and write straight to Trellis when served with --rw).

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
- `scripts/warmth_export.py` + `observatory/warmth_template.html` — the warmth
  table with its receipts.
- `scripts/workbench_export.py` + `observatory/workbench_template.html` — the
  People screen. Counts use correlated subqueries on purpose: joining
  interactions × open_loops × calendar_plans in one query multiplies the rows.
- `scripts/observatory/{fonts.css, tokens.css, common.py}` — the shared spine.
  `common.py` holds the one HTML escaper, the nav, the `people_v` read, and
  `person_key` (which must keep matching the template's own key expression — the
  map's saved notes and flags are keyed by it). Fonts and tokens are **inlined at
  build time**, never linked, so every page stays self-contained and offline.
  The map's 12-color canvas `PALETTE` is data encoding, not chrome — it stays in
  the template and is not themed.
- To QA a change: rebuild all three, then open each and check the Groups /
  Timeline / Ranked views and a detail panel for text overflow or overlap, that
  the nav links work in both directions, and that a person shows the same warmth
  and status on every screen.
- To run the tests: `python3 -m unittest discover -s tests` from the repo root.
  Standard library only — there's no pytest config and no CI, so run them before
  you hand work back.

Keep the data honest. Anything the code guesses (function, seniority) must stay
labeled as inferred in the interface.

**Scale and language.** The pipeline handles small networks up to ~12,000+
connections, and reads exports in any language (the CSV parser anchors on the URL
column, not English headers). The one English-tuned part is the role/seniority
guess: `infer_func` and `infer_rank` match English title keywords. If the user's
export is in another language and most people land in "Other," offer to add their
language's common title words (e.g. "directeur", "ingénieur", "ventes") to those
two functions. Everything else already works regardless of language.

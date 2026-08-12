---
name: network-observatory
version: 1.12.1
description: Build, update, query, and optionally enrich a private professional network from a LinkedIn export using the public Network Observatory repository. Use when a user asks Hermes or another coding agent to set up their network map, warmth table, or People workbench; remember relationships with Trellis; connect the Network Observatory Gmail metadata endpoint; check email recency; ingest optional calendar or meeting events; prioritize, deprioritize, or set a follow-up date on someone; reconcile duplicate identities; or update an existing Observatory.
---

# Network Observatory

Keep LinkedIn sufficient by itself. Treat Gmail, Calendar, and meeting sources as
optional enrichment. Keep all identity matches reversible.

## Get the code

Use the existing checkout when present. Otherwise clone:

```bash
git clone https://github.com/maczumby/network-observatory.git
cd network-observatory
```

Read `CLAUDE.md` before acting. Never commit `exports/`, `data/`, or `dashboard/`.

## Build the LinkedIn map first

1. Find the user's LinkedIn Basic export.
2. Run `python3 scripts/linkedin_import.py [PATH]`.
3. Run `python3 scripts/observatory_export.py`.
4. Follow the local or hosted sharing flow in `CLAUDE.md`.
5. Report the imported count, date span, and inferred fields plainly.

Stop here successfully when the user does not want enrichment.

The map is one of three screens that share a design and a nav bar: the **map**
(who you know), **warmth** (how alive each relationship is), and **People** (the
working list). Warmth and People only say something once there's contact signal,
so build them after the first enrichment pass and hand over one link, not three.

## Connect optional Gmail metadata

Do not ask for a Google client secret, Composio API key, token, or project
credential. Those belong to the hosted onboarding service.

If `network-observatory-gmail` is not configured on this agent, never work
around it by connecting Gmail through your own Composio toolkit or any other
default Gmail tool. Those request broad mailbox scopes, which is exactly what
this design avoids. The Connect page below is the only path.

After the map is built, offer this once: "Want to add Gmail metadata search?
It can only see who you emailed and when, never the messages themselves."
If the user says yes:

1. Send the user to
   `https://network-observatory-connect.mari-network-observatory.workers.dev`.
   They enter the Gmail they want connected (no invite code needed) and
   authorize their own Google account in the browser. Google will warn the
   app is unverified; that is expected while it is in Testing.
2. If Google refuses with "access denied", they are not on the tester list
   yet. Have them send the exact Gmail they used to the operator
   (mari@filament.dm), wait for her confirmation, then retry the same page.
   There are at most 100 tester seats, and access needs a one-click
   reconnect every 7 days until Google verification lands.
3. When the page shows a command starting `hermes mcp add`, ask the user to
   paste it to you, then run it. The new tools appear in your NEXT session,
   not this one — tell the user that, have them start a fresh chat, and test
   there:

```bash
hermes mcp test network-observatory-gmail
```

4. When the test passes, record durably — in your memory, notes, or runbook —
   that email metadata search is installed via `network-observatory-gmail`
   and is the only sanctioned Gmail path for this user: metadata-only by
   design, never open a default Gmail or Composio connection for them.
   Future sessions must reach for this server on any email-history or
   contact-recency question.

The private endpoint exposes only:

- `network_observatory_sweep_email_metadata`
- `network_observatory_get_message_metadata`

It returns message IDs, labels, From, To, Cc, Bcc, and Date. It cannot return
subjects, snippets, bodies, or attachments. Never try to work around that limit.
Gmail's metadata scope does not permit the `q` search parameter, so sweep pages
and match correspondents locally.

If a tool returns `reconnectUrl`, give that link to the user, wait for them to
finish Google authorization, then retry the same call. Google test access may
require this again after seven days.

## Ingest Gmail relationships

Call `network_observatory_sweep_email_metadata` with at most 25 messages per
page. Follow `nextPageToken` only as far as the user's question requires.

**Hygiene first — a CRM full of newsletters is worse than no CRM.** Before
creating anything from a message:

- Skip the whole message when its `labelIds` include `CATEGORY_PROMOTIONS`,
  `CATEGORY_UPDATES`, or `CATEGORY_FORUMS`, unless the correspondent already
  exists as a contact.
- Skip correspondents whose address local part is a system sender: no-reply,
  noreply, donotreply, notifications, notify, updates, newsletter, digest,
  alerts, billing, invoice, receipts, info, admin, hello, team, support,
  mailer-daemon, postmaster, and the like.
- Skip display names that are not person-shaped: single generic words
  ("Push", "Subscribed", "finance"), product names, anything of the form
  "'X' via system-address".
- Skip messages with more than about 6 external recipients unless a
  correspondent is already a contact — big CC threads mint acquaintances,
  not relationships.
- When you genuinely can't tell, ask the user with the evidence ("mail from
  'GDI Nova <nova@gdi.earth>' — is that a person you know?") instead of
  minting a contact.

`scripts/contact_quality.py` encodes these same rules, so you can check a
borderline address instead of guessing:

```bash
python3 scripts/contact_quality.py --name "Brex" --email "notifications@brex.com"
```

It answers `person`, `non_person`, or `ambiguous` with the signals behind the
verdict. Anything `ambiguous` is a question for the user, not a decision for
you.

For every message that survives:

1. Parse addresses from the allowed headers.
2. Exclude the user's own addresses.
3. Create one Trellis event per external correspondent.
4. Use the message date, source `gmail`, and source reference
   `<message-id>:<correspondent-email>`. For the summary, record direction
   and nothing else: `email received` when the correspondent is in the From
   header, `email sent` when the user is. (Older ingests wrote
   `email exchanged`; leave those alone.)
5. Ingest with `python3 scripts/trellis.py ingest --json ...`.

Do not copy any returned header value into the summary except identity and date
fields. Re-runs are idempotent because the source reference is stable.

Say when a sweep is partial. A remaining `nextPageToken`, failed page, or failed
message means absence and staleness are not proven.

**After every sweep, reconcile identities.** Run:

```bash
python3 scripts/trellis.py match
```

It proposes LinkedIn identities for contacts created from sweeps ("brock
<brock@filament.dm> -> Brock Kelly, because email matches their name").
Present each proposal to the user in plain language and merge ONLY the ones
they confirm, with the exact command it prints. Merges are reversible
(`merges` / `unmerge`), and nothing is ever merged automatically. Then offer a
cleanup pass: list any surviving contacts that don't look like people and, on
the user's yes, hide each with `python3 scripts/trellis.py mute --name "..."`
(reversible with `unmute`).

## Show the warmth table after enriching

After any enrichment pass, build the inspectable table and hand over the link:

```bash
python3 scripts/warmth_export.py
```

It writes `dashboard/warmth.html`, which `serve.py` already serves behind the
same password as the map. Report coverage plainly: how many interactions, over
what date range, and how many contacts remain unmeasured. The table separates
`no data` from `cold` on purpose — never describe an unmeasured contact as cold.

For one-off questions ("how warm is Tony?"), use:

```bash
python3 scripts/trellis.py warmth --name "Tony"
```

It answers with the last-contact date, direction, and bucket, and it never
writes suggestions, so it is safe to run any time. Carry the coverage caveat
whenever the sweep has not reached the end of the mailbox.

## Build the People workbench

```bash
python3 scripts/reconcile.py        # first: generates the identity proposals
python3 scripts/workbench_export.py
```

`dashboard/workbench.html` is the working view: every trusted contact with their
warmth, open loops, upcoming meetings, and status, plus filters for flagged
people, due follow-ups, and email-only contacts still waiting to be reconciled.
Expanding a row exposes the controls — prioritize, deprioritize, snooze to a
date, and confirm "this is the same person as this LinkedIn profile".

All three screens share one design and one nav, so build them together and hand
over one link. Served read-only (the default), the controls save in the
browser and produce a sync block the user pastes back to you — read it and run
`trellis.py apply` with the JSON it contains. Served with `--rw` (which requires
a saved password), the controls write straight to Trellis.

## Prioritize, deprioritize, and set follow-ups

Two independent layers. Importance:

```bash
python3 scripts/trellis.py capture --name "Ada Lovelace" --prioritize
python3 scripts/trellis.py capture --name "Ada Lovelace" --deprioritize
```

And time — a date to look again, in the user's own words:

```bash
python3 scripts/trellis.py capture --name "Ada Lovelace" \
  --follow-up "in 6 months" --follow-up-reason "after their launch"
python3 scripts/trellis.py capture --name "Ada Lovelace" --clear-follow-up
```

"Remind me about them next week", "park this one for six months", and "stop
surfacing this person" all map here. A due follow-up appears at the top of
`radar` **even when the person is deprioritized** — that combination is the
point. Both layers are reversible, and the date belongs to the user: never clear
or move a follow-up on your own initiative.

## Ingest other optional sources

Normalize Calendar or meeting events to Trellis only when the user's agent
already has those tools. Do not make Gmail or Calendar prerequisites for
recall, loops, radar, warmth, or the map — everything works from LinkedIn
alone and gets richer with each optional source.

For calendar events specifically:

1. One Trellis event per external attendee who accepted (or organized), kind
   `meeting`, summary just `meeting held`, source `calendar`, source_ref
   `<event-id>:<attendee-email>`. Never store the event title, description,
   or location — same who/when-only rule as email.
2. Skip events with more than ~10 attendees (webinars and all-hands are not
   relationships), recurring-event instances beyond the most recent one, and
   attendees matching the system-sender hygiene rules above.
3. Only create a NEW contact from an attendee when the user confirms it's a
   person worth tracking; attendees matching existing contacts ingest freely.
4. Meetings count toward warmth automatically — a meeting last week makes a
   contact `active` exactly like an email would.

`scripts/calendar_crm.py` applies all of these rules for you. Normalize the
events your calendar tool returns into `{"event_id", "date", "attendees":
[{"name", "email"}]}` and pipe them in:

```bash
python3 scripts/calendar_crm.py --file events.json
```

It splits them by tense: an event that already happened becomes an interaction
("meeting held"), while one still ahead becomes a planned meeting shown on the
People screen — the same person, one model. It skips the user's own and their
teammates' addresses (`data/owner_identities.json`), meeting rooms and other
non-person invitees, and oversized invitations. Re-running is idempotent, so
sweeping the same window twice is safe.

**After a meeting, `radar` does the asking for you.** Once meetings are ingested,
anyone the user met in the last three weeks with no contact since — and nothing
already scheduled or snoozed — surfaces as *"you met them N days ago and haven't
been in touch since"*. Read that back plainly and offer the two useful moves: a
follow-up date (`--follow-up "in 1 week"`), or a draft built only from stored
facts (`trellis.py context --name X`). Never send it. The nudge goes quiet after
three weeks rather than nagging about a conversation that has moved on.

## How to talk about the graph

Every user-facing reply follows these rules. They are the product; the
commands are plumbing.

- Lead with the answer in one plain sentence. At most 3 people per answer
  unless the user asks for more.
- Hyperlink every person you name to their LinkedIn URL when one exists, as
  a markdown link. When you cite email or meeting history, link the warmth
  table too — `<table-url>#p<id>` jumps straight to that person's receipts.
  Get ids and urls from `warmth --json`.
- Every warmth word carries its date: "warm — they wrote to you on
  2026-07-09 (22 days ago)". Never a bucket label without the date behind it.
- Plain words only. Words the user must never see: MCP, endpoint, sweep,
  ingest, source_ref, JSON, tool names, command lines. Say "your email
  history", "who wrote last", "your map".
- Source-state phrasing — one line, only when it changes the answer, and
  never twice in one conversation:
  - LinkedIn only: "That's from your LinkedIn map. Connect Gmail and I can
    tell you how warm these ties actually are."
  - Gmail connected: answer with recency; when coverage is partial, say what
    the data does and doesn't reach.
  - Calendar connected: meetings just count; don't mention calendar unless
    asked.
  - A missing source is never an error. It shrinks the answer, not the
    experience.
- End a substantive answer with at most ONE offer — log a follow-up, open
  the table, or go deeper — not a menu of options.
- When setup finishes (map built, or Gmail connected), teach by example:
  offer exactly three starter questions — "Who have I gone cold on?",
  "Who do I know at <a real company from their map>?", and "Remind me to
  follow up with someone."

## Answering questions with the graph

These are the flows the owner will actually ask for. Compose them from the
pieces rather than improvising:

- **"Who do I know who can do X?" / "Could I get an intro to Y?"** Find
  candidates in the connections table (title, func, company), then check
  `python3 scripts/trellis.py warmth --name "<each>"` for the top few. Answer
  with who they are, how warm the tie is, who wrote last, and the receipts.
  Offer to log a follow-up loop for anyone the user wants to act on.
- **"Who should I follow up with?"** `python3 scripts/trellis.py loops
  --overdue` plus `radar`. Read the reason lines back; if radar is quiet, say
  so.
- **"Remind me to follow up with Jay in two weeks."**
  `python3 scripts/trellis.py capture --name "Jay Sullivan" --loop "follow up"
  --due <YYYY-MM-DD>`. It surfaces in loops and radar when due. Calendar
  scheduling is out of scope; the loop system is the reminder.
- **"How warm is my connection with Tony?"** `warmth --name "Tony"`, with the
  coverage caveat when the sweep is partial.

## Keeping the graph fresh

The user can re-run their LinkedIn export any time; re-importing is idempotent
and never touches Trellis memory. After a re-import, run `match` again —
previously unmatched email and calendar contacts may now have LinkedIn rows to
tie to.

## Resolve identities safely

Prefer exact email matches. Treat name-only or fuzzy matches as candidates.
Never merge automatically.

```bash
python3 scripts/reconcile.py                    # regenerate the review queues; changes nothing
python3 scripts/trellis.py dupes                # possible duplicates, with evidence
python3 scripts/trellis.py match                # LinkedIn candidates for email contacts
python3 scripts/trellis.py merge --from OTHER_ID --into KEEP_ID
python3 scripts/trellis.py merges
python3 scripts/trellis.py unmerge --merge-id JOURNAL_ID
```

`--from` is the record that gets folded in; `--into` is the one that survives.
Show candidates to the user before `merge`. After a confirmed merge, mention
the journal ID so the user can reverse it.

`reconcile.py` writes two review files under `data/`:
`linkedin_identity_review_queue.json` (email or calendar contacts that look like
LinkedIn people already in the graph) and `contact_quality_review.json`
(addresses that may not be people at all). Run it after any sweep. Entries
marked **ambiguous** are questions for the user, with the evidence — never a
decision to make on their behalf. `reconcile.py --apply` does only the safe,
reversible part: muting the user's own and teammates' addresses and confidently
non-human senders.

Set up `data/owner_identities.json` early, or the user's own inbox becomes part
of their network:

```json
{"owner_emails": ["them@example.com"], "owner_domains": ["theircompany.com"]}
```

## Keep the trust contract

- Cite stored sources when answering.
- Explain why a person appears in radar.
- Draft only from stored facts.
- Never send a message.
- Never expose the private MCP URL in a public channel.
- Keep the Observatory useful with LinkedIn alone.

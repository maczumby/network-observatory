---
name: network-observatory
description: Build, update, query, and optionally enrich a private professional network from a LinkedIn export using the public Network Observatory repository. Use when a user asks Hermes or another coding agent to set up their network map, remember relationships with Trellis, connect the Network Observatory Gmail metadata endpoint, check email recency, ingest optional calendar or meeting events, resolve duplicate identities, or update an existing Observatory.
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
   paste it to you, then run it, then run:

```bash
hermes mcp test network-observatory-gmail
```

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

For every message:

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

## Ingest other optional sources

Normalize Calendar or meeting events to Trellis only when the user's agent
already has those tools. Store who, when, event kind, and a minimal source
label. Do not make Gmail or Calendar prerequisites for recall, loops, radar, or
the map.

## Resolve identities safely

Prefer exact email matches. Treat name-only or fuzzy matches as candidates.
Never merge automatically.

```bash
python3 scripts/trellis.py dupes
python3 scripts/trellis.py merge --keep-id KEEP --merge-id OTHER
python3 scripts/trellis.py merges
python3 scripts/trellis.py unmerge --merge-id JOURNAL_ID
```

Show candidates to the user before `merge`. After a confirmed merge, mention
the journal ID so the user can reverse it.

## Keep the trust contract

- Cite stored sources when answering.
- Explain why a person appears in radar.
- Draft only from stored facts.
- Never send a message.
- Never expose the private MCP URL in a public channel.
- Keep the Observatory useful with LinkedIn alone.

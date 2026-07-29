# Email recency — when did I last talk to this person?

Asks Gmail how long it's been since you exchanged email with someone: last
contact date, who wrote last, and a temperature label (active, warm, cooling,
cold). Feed it one contact, a list, or every LinkedIn connection in your
network database that shared an email address.

This is the one piece of network-observatory that holds a credential, so the
deal is spelled out here. Trellis and the map never touch the network; this
script talks to Google directly, using the most limited Gmail access that
exists (`gmail.metadata`). Under that permission Google's own servers return
message headers only — From, To, Cc, Date — and refuse to return bodies or
attachments no matter what the software asks for. The restriction is
enforced by Google, not promised by this code. Your token stays in
`data/gmail_token.json` on your machine and you can kill it any time at
myaccount.google.com/permissions.

## What you can do

```bash
# one person
python3 scripts/email_recency.py "sam@example.com"

# a list, one contact per line
python3 scripts/email_recency.py --batch contacts.txt

# every LinkedIn connection that shared an email with you
python3 scripts/email_recency.py --from-db

# machine-readable, deeper sweep
python3 scripts/email_recency.py --from-db --json --limit 2000
```

First run opens a browser consent screen; approve it once and you're set.
It needs an OAuth client JSON at `data/gmail_oauth_client.json` (ask Mari
for the Filament Email Recency client, or the script prints instructions
for making your own).

## For the agent

- Prefer `--json`. Check the `sweep` block first: nonzero `failed` or
  `undated` means the sweep was incomplete, and an incomplete sweep can only
  understate contact. Say so instead of reporting staleness as fact.
- "Nothing found" means "not in the last N messages swept" (default 500),
  not "never emailed". Raise `--limit` before concluding there's no history.
- A name fragment matches as a substring, so it can hit the wrong person.
  Every result carries `matched_address`; name it in your answer so the user
  can catch a mismatch you can't.
- Results sort stalest first. Lead with the people past 60 days; that's the
  actionable end.
- `--subjects` is the one flag that pulls message content (subject lines).
  Leave it off unless the user explicitly asks what the threads were about.
- Recency labels: active is 14 days or fewer, warm is 15 to 60, cooling is
  61 to 180, cold is over 180.
- Pairs well with Trellis: a `--from-db --json` run is exactly the shape to
  feed `trellis capture` interactions, so radar and loops can reason over
  real email recency. Do that only when the user asks for it.

## On hosted agents (Hermes): don't run this script, ingest instead

`email_recency.py` is for agents on the user's own computer. If you are a
hosted agent (Hermes, Agent37), do not set up OAuth here and do not ask the
user for a client JSON. You already have your own connected tools; token
custody stays with your platform's connector (for Gmail on Hermes that is
usually Composio), and this repo requests no Google scopes at all.

The flow on a hosted agent:

1. Fetch recent messages with your own Gmail tool. You only need sender,
   recipients, and date. Ignore bodies; they are none of this repo's
   business even when your connector can see them.
2. Normalize each message into a Trellis event and hand it over:

```bash
python3 scripts/trellis.py ingest --json '[
  {"person": {"name": "Sam Rivera", "email": "sam@example.com"},
   "kind": "email",
   "date": "2026-07-22",
   "summary": "email received",
   "source": "gmail",
   "source_ref": "<gmail message id>"}
]'
```

`source_ref` makes ingest idempotent, so re-running over the same messages
is safe: already-seen events are skipped. After ingest, `trellis.py recall`,
`loops`, and `radar` answer recency questions from the same data the local
script would have produced. Store who and when, not what was said.

## Design credit

The metadata-only approach follows Maria's email-recency skill
(github.com/123maria0608-star/email-recency-skill). This is an independent
implementation, written for this repo's zero-dependency rule: Python
stdlib only, raw Gmail REST, no Google client libraries.

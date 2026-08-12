# Trellis — your relationship memory

Trellis is the third piece of network-observatory. It remembers who people are, what
you owe them, and when it's worth reaching out — on the same local graph as your
LinkedIn import, with every suggestion explaining why. It runs entirely on your
machine. No server, no accounts, no tokens.

Trellis is **source-adaptive**: it works from your LinkedIn graph and things you tell
it, and gets richer as your agent pipes in more (meetings, email, calendar). It never
fetches anything itself — your agent does that with the tools it already has and hands
the results to Trellis.

## What you can do

```bash
# who is this person? when did we last talk? what do I owe them? who do I know at Y?
python3 scripts/trellis.py recall "Maya"
python3 scripts/trellis.py recall "Stripe"

# log something you just did or learned (your agent turns your words into this)
python3 scripts/trellis.py capture --name "Sam Rivera" --company "Walmart" \
  --interaction "Met at the Lux event, talked agent commerce" --kind event \
  --loop "Send the deck" --due 2026-07-12 --priority important --mode prospect

# what am I forgetting?
python3 scripts/trellis.py loops            # open loops — who you left hanging
python3 scripts/trellis.py radar            # a few reach-outs worth making, with reasons

# who's warm, who's going cold, and what's still unmeasured?
python3 scripts/trellis.py warmth                    # whole graph, warmest first
python3 scripts/trellis.py warmth --name "Tony"      # one person, with receipts context
python3 scripts/trellis.py warmth --bucket cold      # triage a temperature band
python3 scripts/warmth_export.py                     # bake the browsable table (dashboard/warmth.html)

# help me write to someone (agent drafts from this; never sends)
python3 scripts/trellis.py context --name "Maya Chen"

# mark someone, or park them until a date
python3 scripts/trellis.py capture --name "Ada" --prioritize      # star them everywhere
python3 scripts/trellis.py capture --name "Ada" --deprioritize    # hide from warmth/radar
python3 scripts/trellis.py capture --name "Ada" --follow-up "in 6 months" \
  --follow-up-reason "after their launch"
python3 scripts/trellis.py capture --name "Ada" --clear-follow-up

# housekeeping
python3 scripts/trellis.py dupes            # possible duplicate people to confirm
python3 scripts/trellis.py match            # tie email/calendar contacts to LinkedIn (proposals only)
python3 scripts/trellis.py mute --name X    # hide a newsletter/system sender (reversible: unmute)
python3 scripts/trellis.py merges           # audit active and undone identity merges
python3 scripts/trellis.py unmerge --merge-id 3
python3 scripts/reconcile.py                # regenerate the identity + contact-quality review queues
```

**Priority and follow-ups are two different things.** Priority is how much
someone matters (starred, normal, or deprioritized). A follow-up is a date to
look again. They're independent on purpose: a due follow-up appears at the top
of `radar` **even for a deprioritized person**, which is exactly what "not now,
but check back after their launch" means. Both are reversible, and `radar` never
clears a follow-up on its own — the date is yours.

Warmth buckets are absolute and match the email-recency skill: active is 14
days or fewer, warm 15 to 60, cooling 61 to 180, cold over 180. A person with
no interactions is `no data`, never `cold` — unmeasured is not the same as
ignored, and the table says how much of the graph is measured right at the
top. `warmth` is read-only: unlike `radar`, it never writes suggestions.

## The trust contract (why you can rely on it)

- **Every fact has a source.** Interactions and suggestions carry where they came from.
- **Every suggestion explains why.** `radar` shows the reason and the facts behind it.
- **It never invents.** `context` returns only stored facts for drafting.
- **It stays quiet.** When there's nothing real to surface, it says so — it won't
  invent reasons to bother people.
- **It never merges blindly and never sends.** Duplicates are shown for you to confirm.
  Every confirmed merge is journaled, and `unmerge` restores the records and relationship
  metadata to their exact pre-merge owners. Drafting is on request; sending is always yours.

## How the map and Trellis connect

The Observatory map reflects Trellis: people you've flagged show up highlighted and
your notes are pre-filled. When you flag or note someone *in the map*, click **"Sync
to your agent"** — it gives you a plain-text block listing who to flag and your notes.
Paste that into your agent chat. Your agent reads it and saves each person into Trellis
(via `capture`, or `apply`) — no file, no JSON required from you.

## Feeding Trellis from other sources (optional)

If your agent has email, calendar, or meeting notes connected, it can add interactions
by normalizing each item and calling `ingest` — no tokens ever live in Trellis:

```bash
python3 scripts/trellis.py ingest --json \
 '{"person":{"name":"Alex Rivera","email":"alex@co.com"},"kind":"meeting",
   "date":"2026-07-01","summary":"Partnership sync","source":"calendar","source_ref":"evt_123"}'
```

Re-runs are idempotent (same `source_ref` won't double-count). With nothing connected,
recall + loops still work from your LinkedIn graph and manual capture.

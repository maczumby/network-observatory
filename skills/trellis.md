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

# help me write to someone (agent drafts from this; never sends)
python3 scripts/trellis.py context --name "Maya Chen"

# housekeeping
python3 scripts/trellis.py dupes            # possible duplicate people to confirm
```

## The trust contract (why you can rely on it)

- **Every fact has a source.** Interactions and suggestions carry where they came from.
- **Every suggestion explains why.** `radar` shows the reason and the facts behind it.
- **It never invents.** `context` returns only stored facts for drafting.
- **It stays quiet.** When there's nothing real to surface, it says so — it won't
  invent reasons to bother people.
- **It never merges blindly and never sends.** Duplicates are shown for you to confirm;
  drafting is on request; sending is always yours.

## How the map and Trellis connect

The Observatory map reflects Trellis: people you've flagged show up highlighted and
your notes are pre-filled. When you flag or note someone *in the map*, click **"Sync
to your agent"** — it gives you a copy-paste block. Paste it to your agent, which folds
it back into Trellis:

```bash
python3 scripts/trellis.py apply --file trellis-sync.json   # or --json '...'
```

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

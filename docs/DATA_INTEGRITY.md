# Data integrity: one person, one memory

Everything in this tool joins on a single idea of a person. The map, the warmth
table, the People workbench, and every answer your agent gives are the same
rows read four ways. When that idea gets fuzzy — the same human living as two
rows, a billing robot counted as a contact, your own address in your own
network — every downstream number quietly lies.

This document is the contract the code follows. If you change the code, change
this first.

---

## 1. One canonical person

People live in `connections`. One row is one human, identified by
`connections.id`.

- LinkedIn rows are keyed on the profile URL and arrive rich (name, company,
  title, inferred function and seniority).
- Email- and calendar-minted rows are keyed on the address and arrive thin —
  often a first name ("brock") or no name at all.

These are two ways of learning about people, not two kinds of people. A row
minted from a sweep is a *provisional* person until it is either reconciled
with a LinkedIn row or confirmed on its own.

**`people_v` is the only trusted read.** Every exporter and every screen reads
this view rather than restating the filter. It encodes three rules:

| Rule | Why |
|---|---|
| No `merged_into_*` tombstones | A merged alias is kept for reversibility, not for display. |
| No `muted` people | Muting is how you say "this is not a person I know". |
| Gmail/calendar rows only once they carry signal | A bare address with nothing attached is a guess, not a contact. Signal means a past interaction **or an upcoming meeting** — the person you're seeing on Thursday shouldn't be invisible until after you've met them. |

Source comparisons are case-insensitive everywhere, and `source` is written
lowercase. A `Gmail` row and a `gmail` row are the same thing; if the gate only
matched one spelling, unreconciled contacts would leak onto every screen.

---

## 2. Reconciliation: propose, confirm, reverse

Identity decisions are yours. The tool never silently decides two rows are one
person.

1. **Propose.** `scripts/reconcile.py` regenerates
   `data/linkedin_identity_review_queue.json` from the database, using name
   similarity, email local-part matches, and unique-first-name matches. Each
   proposal carries its evidence and the exact command that would act on it.
2. **Confirm.** You (or your agent on your say-so) run
   `trellis.py merge --from N --into M`, or confirm it from the People screen.
   History moves; the old row is marked, never deleted.
3. **Reverse.** Every merge is journaled — priority, mode, **and follow-up
   dates**. `trellis.py unmerge --merge-id N` puts it all back, and refuses if
   metadata changed after the merge rather than guessing. When both people
   carry a follow-up, the merged person keeps the **earlier** date: a merge
   must never quietly push a reminder later.

The same shape applies to muting: reversible with `unmute`, always.

---

## 3. You are not in your own network

Your own addresses and your teammates' addresses are plumbing, not
relationships. They live in `data/owner_identities.json`, beside the database:

```json
{
  "owner_emails": ["you@example.com", "you@work.example"],
  "owner_domains": ["yourcompany.com"]
}
```

`owner_domains` catches a whole team without listing everyone. The exclusion
applies on **every** ingest path — agent email ingest, meetings that already
happened, and meetings still ahead. A missing file simply means no exclusions;
a malformed one fails loudly rather than silently ingesting your own inbox.

---

## 4. Not every address is a person

`scripts/contact_quality.py` classifies a contact as `person`, `non_person`, or
`ambiguous`, using the same hygiene rules the skill pack teaches agents: system
local parts (`no-reply@`, `billing@`, `notifications@`), machine sending
domains, calendar room resources, generic single-word display names ("Push",
"Transactions"), and "X via SomeService" relays.

The verdicts do different work:

- **`non_person`** is confident enough to act on — `reconcile.py --apply` mutes
  these (reversibly).
- **`ambiguous`** is never acted on. It goes in the review queue, and the honest
  move is to ask: *"mail from 'GDI Nova <nova@gdi.earth>' — is that someone you
  know?"* A relay display name always lands here, because a real person may be
  behind it.

Machine-domain detection matches **subdomains only** (`em.notion.so`,
`bounce.sendgrid.net`), never the registrable domain: `mail.ru` and `mail.com`
are consumer mail providers with hundreds of millions of human users.

And `--apply` never mutes someone you marked `important` or `critical`
yourself. Your judgement outranks the classifier — and since `unmute` can only
restore `normal`, muting there would erase a mark that nothing recorded. Those
people are listed separately so you can mute one by hand if you actually meant
to.

Muting hides someone from warmth, radar, and all three screens. It does not
delete anything.

---

## 5. One meeting, two tenses

A meeting is one model with two states, keyed identically
(`<event_id>:<attendee-email>`):

- **Already happened** → a row in `interactions` (`kind='meeting'`,
  summary `meeting held`, source `calendar`). It counts toward warmth.
- **Still ahead** → a row in `calendar_plans`. It does not count toward warmth,
  because it hasn't happened.

Both store **who and when only**. Titles, descriptions, and locations are never
stored. Invitations with more than ten external attendees are skipped: a big
invite mints acquaintances, not relationships.

This is what the calendar is *for*: `radar` surfaces anyone you met recently and
haven't spoken to since, so "we should stay in touch" doesn't quietly become
nothing. It stays silent when you've already been in touch, when you're seeing
them again, when you set your own date, and after three weeks — a nudge that
fires forever is one you learn to ignore.

---

## 6. Direction is a column, not a sentence

Who wrote last is `interactions.direction` (`sent`, `received`, or NULL) — not
a string parsed out of the summary. Older databases are backfilled once from
the legacy summaries (`email sent` → sent, `email received` → received; the
direction-less `email exchanged` era stays NULL).

Warmth reads the newest interaction **by date, not by row id** — paged sweeps
ingest older messages after newer ones — and takes its direction as-is. A newer
direction-less touch (a meeting) means we no longer know who wrote last, so it
wins over an older `sent`. When several touches share that newest date, they
have to agree: if you wrote and they wrote the same day, "who wrote last" has
no answer, and picking one by insertion order would be inventing one. Saying
"you wrote last" when the truth is "you met last" is exactly the kind of small
lie that erodes trust in the whole tool.

---

## 7. Two layers of status: importance and time

These answer different questions and are deliberately independent.

**Priority** — how much this person matters (`person_meta.priority`):

| Value | Meaning |
|---|---|
| `critical` / `important` | Starred on every screen; radar keeps them in view. |
| `normal` | Default cadence. |
| `muted` | Hidden from warmth, radar, and the screens. |

**Follow-up** — when to look again (`person_meta.follow_up_on` +
`follow_up_reason`): a date, set in natural language ("in 1 week", "in 6
months") or explicitly.

The important interaction: **a due follow-up fires even for a deprioritized
person.** "Not now, but check back in six months" is one of the most useful
things you can say about a relationship, and it only works if muting doesn't
swallow it. Radar surfaces due follow-ups first, above overdue loops, and never
clears them on its own — you set the date, you clear it.

---

## 8. Migrations are additive, and loud when they can't be

`migrate()` runs on every connection. It only ever adds columns, creates
missing tables, and recreates the `people_v` view so the shipped definition is
the one in effect. The backfill touches only NULLs, so it can't overwrite
anything you or your agent set.

Afterwards it asserts the schema it expects. A database that carries a
*different* CRM shape stops the tool with a plain-language error instead of
writing against a structure it doesn't understand. A loud failure on the first
run is recoverable; a silent one corrupts a graph you may have spent months
building.

One consequence worth knowing: because the exporters go through the same
`connect()`, **building a page opens the database read-write**. It adds columns
and recreates a view; it never rewrites your rows. If you need a strictly
read-only pass, copy the file first.

---

## Checking your own data

```bash
python3 scripts/reconcile.py                 # regenerate the review queues (changes nothing)
python3 scripts/reconcile.py --apply         # mute owner addresses + confident non-people
python3 scripts/trellis.py match             # LinkedIn identity proposals, propose-only
python3 scripts/trellis.py dupes             # possible duplicates to confirm
python3 scripts/trellis.py merges            # every identity merge and its status
python3 scripts/trellis.py warmth --json     # coverage + per-person signal
```

None of these send anything anywhere. This is a local file on your machine.

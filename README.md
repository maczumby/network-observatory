# Network Observatory

Turn your LinkedIn export into a private map of your professional network — a
star field of every person you're connected to, that you can pan, search, and
filter. It runs entirely on your own machine. Nothing is uploaded.

The idea: your network is a kind of memory, and right now it's locked in a CSV.
This gives your AI agent a database it can reason over, and gives you a way to
actually see who you know.

## The fastest way to use it

Give this whole folder to your coding agent (Claude Code or similar), then send
one message with your LinkedIn export attached or dropped in:

> Here's my LinkedIn export. Set this up and show me my network.

The agent reads `CLAUDE.md`, imports your data, builds the visual, and opens it.
You don't need to run anything yourself.

## Or run it yourself

Two commands, no installation (Python 3 and a browser are all you need):

```bash
# 1. put your LinkedIn export (.zip, folder, or Connections.csv) in exports/
python3 scripts/linkedin_import.py

# 2. build and open the map
python3 scripts/observatory_export.py --open
```

Step 1 creates `data/linkedin.db`. Step 2 creates `dashboard/observatory.html`, a
single file you can open any time or send to yourself.

**Running through a hosted agent** (like Agent37/Hermes) instead of on your own
machine? The agent can't open a browser on your screen, so skip `--open`. Have it
build `observatory.html` and send you that one file to open locally — it's
fully self-contained. The export step prints a short summary the agent can share
with you too.

## Getting your LinkedIn export

LinkedIn → **Settings → Data Privacy → Get a copy of your data**. Choose the
"Basic" archive that includes Connections; it usually arrives by email within a
few minutes. Drop the `.zip` into the `exports/` folder here.

## What you get

- **A local database** (`data/linkedin.db`) of every connection: name, company,
  title, when you connected, plus a function and seniority level read from each
  job title.
- **A visual explorer** with three ways to see the same people: clusters floating
  in space, orbits with you at the center and time as distance, or ranked columns.
  Color by company, role, seniority, or era. Search anyone, filter and combine,
  and click a star to read details, jot a private note, or flag someone to
  reconnect with.
- **A short reading** of your network: how many people over how many years, where
  your center of gravity sits, and who's worth reconnecting with.

## Will this work for my network?

Yes, with one caveat worth knowing up front.

- **Any size.** Tested from a handful of connections up to 12,000. Large networks
  (past ~15,000) still render but the animation gets heavier on older machines.
- **Any language.** LinkedIn localizes the export — French headers, German dates,
  and so on. The importer reads by column position rather than English text, so
  your names, companies, titles, and dates come through whatever language your
  export is in. The map, search, and the company and era views all work.
- **Any industry.** Roles are sorted into buckets that cover tech, healthcare,
  law, education, finance, trades, government, nonprofit, science, hospitality,
  the arts, and more. Anything that doesn't map cleanly goes to "Other."

**The caveat:** the role and seniority guess reads *English* job titles. If your
export is in another language, most people will land in "Other" and "Individual
contributor" for the role and seniority views, because the tool doesn't yet
recognize titles like "Directeur des ventes." Everything else still works. If you
want your language supported, it's a small change to the keyword lists in
`scripts/linkedin_import.py` — ask your agent to add it.

## Trellis — remembering, not just seeing

The map shows your network; **Trellis** helps you tend it. It's a local relationship
memory on the same data: ask "who is X, when did we last talk, what do I owe them,"
log what happened when you meet someone, and get a short, reasoned list of who's worth
reaching out to. Every answer cites where it came from, it drafts only from real facts
(never invents), and it never sends anything.

```bash
python3 scripts/trellis.py recall "Maya"     # who is she, our history, open loops
python3 scripts/trellis.py loops             # who you left hanging
python3 scripts/trellis.py radar             # a few reach-outs worth making, with reasons
```

It works from your LinkedIn graph and what you tell it, and gets richer if your agent
feeds it meetings, email, or calendar — no accounts or tokens, all local. When you flag
or note people in the map, the "Sync to your agent" button hands them back to Trellis.
See `skills/trellis.md`.

## A note on what's inferred

Your export gives a name, company, title, and connection date. Function
(Engineering, Sales, Founder, and so on) and seniority (Entry through Executive)
are read from the job title with fixed rules. These are labeled *inferred*
everywhere they appear. Titles that don't map cleanly are grouped as "Other"
rather than forced into a category.

## Privacy

Your export, the database, and the generated page contain personal contact data,
so the tool keeps all of it local. `.gitignore` excludes `exports/`, `data/`, and
`dashboard/` output, so you won't accidentally commit any of it. The generated HTML
is fully self-contained — fonts and everything else are embedded, so it makes no
network calls at all and works with the internet off. Your data never leaves your
machine.

## Keeping it current

The tool improves over time. To get the latest, just tell your agent **"update the
network-observatory tool."** It fetches the newest version, copies it over the code, and
rebuilds your map — **your data, flags, and notes are never touched** (they live in
`data/`, which updates leave alone). The `VERSION` file shows what you have.

Under the hood that's `python3 scripts/update.py` (or `--from-zip PATH` if someone sent
you a new zip instead of a link).

## Requirements

- Python 3.8 or newer (standard library only — nothing to `pip install`)
- A web browser to view the map

## How this was made

The visual started as a design built in [Claude](https://claude.ai) and was ported
into this self-contained page. The import and export scripts were built with an AI
agent too. It's meant to be read and changed; see `CLAUDE.md` for how the pieces
fit together.

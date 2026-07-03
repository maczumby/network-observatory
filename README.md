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

## A note on what's inferred

Your export gives a name, company, title, and connection date. Function
(Engineering, Sales, Founder, and so on) and seniority (Entry through Executive)
are read from the job title with fixed rules. These are labeled *inferred*
everywhere they appear. Titles that don't map cleanly are grouped as "Other"
rather than forced into a category.

## Privacy

Your export, the database, and the generated page contain personal contact data,
so the tool keeps all of it local. `.gitignore` excludes `exports/`, `data/`, and
`dashboard/` output, so you won't accidentally commit any of it. The HTML page
works offline and makes no network calls with your data.

## Requirements

- Python 3.8 or newer (standard library only — nothing to `pip install`)
- A web browser to view the map

## How this was made

The visual started as a design built in [Claude](https://claude.ai) and was ported
into this self-contained page. The import and export scripts were built with an AI
agent too. It's meant to be read and changed; see `CLAUDE.md` for how the pieces
fit together.

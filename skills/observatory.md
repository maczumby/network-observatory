# Observatory — your network as a star map

Render the LinkedIn memory database into a single HTML page you can double-click:
your whole network as a starfield you can pan, zoom, filter, and search. This is
the visual step; `/linkedin-import` is the ingest step.

Every connection is a star. Sized by seniority, glowing by recency, clustered by
whatever attribute you color on. It is a standalone build with no server and no
runtime — your data is baked into the file.

## Prerequisite
`data/linkedin.db` must exist. If it doesn't, run `/linkedin-import` first.

## Run it
```bash
python3 scripts/observatory_export.py --open      # build and open in the browser
python3 scripts/observatory_export.py             # build only
```
Output: `dashboard/observatory.html` (one file, ~400 KB, works fully offline).

## What's in the map
- **Three views** (bottom bar): *Groups* (clusters that share an attribute — hover
  one to light up its constellation lines), *Timeline* (you at center, time as
  distance), *Ranked* (columns by how many people you know).
- **Color by** company, role, seniority, or era connected. The legend updates live.
- **Search** any name, company, or title. **Filter** by company, role, seniority,
  or when you connected, and combine those filters.
- **Preset chips** built from what's actually in your network (for example
  "Founders," "My peers," "Only one I know there," and "Who's at *[company]*?"
  when you know a crowd somewhere recognizable). Chips appear only when they'd
  return results.
- **Click a star** for the detail panel: title, company, when you connected,
  inferred function and seniority, a LinkedIn link, a private note field, and a
  "flag to reconnect" toggle. Notes and flags are saved in your browser.
- **"Read my network"** opens a plain-language reading: how many people, over how
  many years, your center of gravity, and reconnect candidates.

## Share it (on a hosted agent)

The build is a standalone file, but on a hosted agent (Agent37/Hermes) the user
can't just double-click it, so hand them a link instead. In order:

1. **Serve it.** `python3 scripts/serve.py` serves `dashboard/` on port **8766**
   (run it in the background).
2. **Expose the port** with agent37's in-VM host helper (`agent37-host_add 8766`
   on current builds), which returns a public URL like
   `https://exposed-port-8766-<hash>.h48.openclaw.agent37.com/observatory.html`.
   That exact command isn't in agent37's published docs, so if it's missing or
   renamed, check `agent37-host_add --help` or
   [agent37's docs](https://agent37.com/docs/agents-api/public-ports).
3. **Verify** it returns `200` (`curl -sS -o /dev/null -w "%{http_code}\n" "<url>/observatory.html"`)
   before sending anything.
4. **Send a hyperlink,** `[Open Network Observatory](<url>/observatory.html)`, not
   the raw URL.
5. **Offer a password** — the link is public until you add one:
   `python3 scripts/serve.py --password "<user>:<pass>"`.
6. **Optionally** set up community querying with `network-answers.md`.

The full gated sequence, with checkpoints, is in `CLAUDE.md`.

## Data honesty
Everything shown comes straight from your export. Function and seniority are
*inferred from job titles* and labeled that way throughout. Long names, titles,
and companies wrap or truncate instead of overflowing, and blank fields degrade
gracefully.

## Refreshing
Re-run `/linkedin-import` with a newer export, then run this again. The output
file overwrites in place. Your saved notes and flags live in the browser and
survive the rebuild.

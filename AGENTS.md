# Agent instructions

The full runbook for this repo lives in [CLAUDE.md](./CLAUDE.md). It applies to
any AI agent, not just Claude Code.

Short version: when the user gives you a LinkedIn export, follow the runbook in
CLAUDE.md **top to bottom, one step at a time, pausing at each ✋ checkpoint** —
build the map first, then (only if they want them) publish a link, offer a
password, and set up community querying. Don't run ahead or batch the steps. No
packages to install. Their data is personal — keep it local and never commit
`exports/`, `data/`, or `dashboard/` output.

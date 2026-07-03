# Agent instructions

The full runbook for this repo lives in [CLAUDE.md](./CLAUDE.md). It applies to
any AI agent, not just Claude Code.

Short version: when the user gives you a LinkedIn export, run
`python3 scripts/linkedin_import.py` then `python3 scripts/observatory_export.py --open`,
and walk them through the result. No packages to install. Their data is personal —
keep it local and never commit `exports/`, `data/`, or `dashboard/` output.

#!/usr/bin/env python3
"""
trellis.py — a local, source-adaptive relationship memory on top of your network graph.

Trellis is the third piece of network-observatory. It remembers who people are, what
you owe them, and when it's worth reaching out — on a graph you own, with every
suggestion explaining why. It reuses the same local SQLite DB as the LinkedIn import
(the `connections` table is the people; Trellis adds interactions, open loops, notes,
priorities, and a suggestions log).

Design principles (the trust contract — enforced, not decorative):
  - Owned + local. One SQLite file. No server, no tokens, no network calls.
  - Source-adaptive. Works from LinkedIn + manual capture alone; gets richer as an
    agent pipes in meetings / email / calendar via `ingest`. Trellis never fetches
    or holds credentials — the agent does that with the tools it already has.
  - Provenance always. Every interaction and suggestion carries its source.
  - Never invents. `context` returns only stored facts for drafting; nothing else.
  - Never auto-merges, never sends. Duplicates are surfaced for the user to confirm.

Commands (all read/write the local DB; safe to run repeatedly):
  capture   record a person / interaction / open loop / note (agent parses language)
  ingest    add normalized event(s) as JSON from whatever the agent fetched
  recall    "who is X, when did we last talk, what do I owe them, who do I know at Y"
  loops     open loops — who you left hanging / what you owe
  radar     a few reason-lined reach-out suggestions (quiet when there's nothing real)
  context   the allowed context pack for one person (for the agent to draft from)
  dupes     possible duplicate people to confirm (never auto-merged)
  merge     merge one person into another after you've confirmed
  merges    list identity merges and their current status
  unmerge   reverse a confirmed identity merge

No third-party packages — standard library only (Python 3.8+).
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import date, datetime, timezone
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "linkedin.db")

# Reuse the import's title inference for manually-captured people (same repo).
sys.path.insert(0, HERE)
try:
    from linkedin_import import infer_func, infer_rank, infer_founder
except Exception:  # keep Trellis usable even if the importer isn't present
    def infer_founder(t): return False
    def infer_func(t, f): return "Other"
    def infer_rank(t): return 2

TODAY = date.today()

# Reach-out cadence (days) by relationship mode — how long a gap is "overdue".
CADENCE = {"collaborator": 30, "prospect": 45, "investor": 60, "mentor": 90,
           "friend": 90, "weak_tie": 240, None: 120, "": 120}
PRIORITY_FACTOR = {"critical": 0.5, "important": 0.75, "normal": 1.0, "muted": 99.0}
# How long after a meeting it still reads as a natural follow-up. Past this,
# radar stops mentioning it rather than nagging about a conversation that has
# moved on.
MEETING_FOLLOW_UP_DAYS = 21


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CONNECTIONS_DDL = """
CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    natural_key TEXT UNIQUE, first_name TEXT, last_name TEXT, full_name TEXT,
    url TEXT, email TEXT, company TEXT, title TEXT, func TEXT,
    is_founder INTEGER DEFAULT 0, rank INTEGER,
    connected_year INTEGER, connected_month INTEGER, connected_raw TEXT,
    source TEXT DEFAULT 'linkedin', first_seen_at TEXT, updated_at TEXT
);
"""

TRELLIS_DDL = """
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id),
    kind TEXT, occurred_on TEXT, summary TEXT,
    source TEXT, source_ref TEXT, confidence REAL DEFAULT 1.0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS open_loops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id),
    description TEXT, status TEXT DEFAULT 'open', due_on TEXT,
    source TEXT, source_ref TEXT, created_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id),
    content TEXT, category TEXT DEFAULT 'context', created_at TEXT
);
CREATE TABLE IF NOT EXISTS person_meta (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id),
    priority TEXT DEFAULT 'normal', mode TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER REFERENCES connections(id),
    kind TEXT, reason TEXT, score REAL, facts TEXT,
    created_at TEXT, user_action TEXT
);
CREATE TABLE IF NOT EXISTS identity_merges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_connection_id INTEGER NOT NULL REFERENCES connections(id),
    into_connection_id INTEGER NOT NULL REFERENCES connections(id),
    from_source TEXT, created_at TEXT NOT NULL, undone_at TEXT,
    merged_meta_existed INTEGER NOT NULL DEFAULT 0,
    merged_priority TEXT, merged_mode TEXT, merged_meta_updated_at TEXT
);
CREATE TABLE IF NOT EXISTS identity_merge_items (
    merge_id INTEGER NOT NULL REFERENCES identity_merges(id),
    table_name TEXT NOT NULL, row_id INTEGER NOT NULL,
    PRIMARY KEY (merge_id, table_name, row_id)
);
CREATE TABLE IF NOT EXISTS identity_merge_meta (
    merge_id INTEGER NOT NULL REFERENCES identity_merges(id),
    connection_id INTEGER NOT NULL REFERENCES connections(id),
    existed INTEGER NOT NULL, priority TEXT, mode TEXT, updated_at TEXT,
    follow_up_on TEXT, follow_up_reason TEXT,
    PRIMARY KEY (merge_id, connection_id)
);
CREATE INDEX IF NOT EXISTS idx_int_conn ON interactions(connection_id);
CREATE INDEX IF NOT EXISTS idx_int_conn_date ON interactions(connection_id, occurred_on);
CREATE INDEX IF NOT EXISTS idx_loop_conn ON open_loops(connection_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_int_srcref
    ON interactions(source, source_ref) WHERE source_ref IS NOT NULL;
"""

# Tables added after v1.11.1. migrate() creates these itself so that running it
# against an older DB is enough — it never depends on the DDL above having run.
CALENDAR_PLANS_DDL = """
CREATE TABLE IF NOT EXISTS calendar_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id),
    planned_on TEXT, source TEXT, source_ref TEXT, created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_srcref
    ON calendar_plans(source, source_ref) WHERE source_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_plan_conn ON calendar_plans(connection_id);
"""

# Everyone still standing (a merged alias is kept for reversibility, not for
# display), each row carrying WHY the product would hide them. Stating the
# rules once, here, is the point: people_v below is a filter over this view,
# and the "show me what's hidden" paths read this one — so the two can't drift.
# Created by migrate() as well as by the main DDL, so that migrating an older
# database reaches the schema assertion with these tables present.
IDENTITY_JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS identity_merge_meta (
    merge_id INTEGER NOT NULL REFERENCES identity_merges(id),
    connection_id INTEGER NOT NULL REFERENCES connections(id),
    existed INTEGER NOT NULL, priority TEXT, mode TEXT, updated_at TEXT,
    follow_up_on TEXT, follow_up_reason TEXT,
    PRIMARY KEY (merge_id, connection_id)
);
"""


# Bumped whenever migrate() gains work to do. A DB already stamped with this
# skips the whole pass, so ordinary reads and writes stop paying for it.
SCHEMA_VERSION = 3


PEOPLE_ALL_V_SQL = """
CREATE VIEW people_all_v AS
SELECT c.id, c.natural_key, c.first_name, c.last_name, c.full_name,
       c.url, c.email, c.company, c.title, c.func, c.is_founder, c.rank,
       c.connected_year, c.connected_month, c.connected_raw,
       lower(COALESCE(NULLIF(c.source,''),'manual')) AS source,
       c.first_seen_at, c.updated_at,
       COALESCE(m.priority,'normal') AS priority, m.mode,
       m.follow_up_on, m.follow_up_reason,
       CASE
         WHEN COALESCE(m.priority,'normal') = 'muted' THEN 'muted'
         -- A bare swept address with nothing attached is a guess, not a
         -- contact. An upcoming meeting counts: the person you're seeing on
         -- Thursday must not be invisible until after you've met them.
         WHEN lower(COALESCE(c.source,'')) IN ('gmail','calendar')
              AND NOT EXISTS (SELECT 1 FROM interactions i
                              WHERE i.connection_id = c.id)
              AND NOT EXISTS (SELECT 1 FROM calendar_plans cp
                              WHERE cp.connection_id = c.id)
           THEN 'no signal yet'
         ELSE NULL
       END AS hidden_reason
FROM connections c
LEFT JOIN person_meta m ON m.connection_id = c.id
WHERE lower(COALESCE(c.source,'')) NOT LIKE 'merged_into_%'
"""

# The one trusted read: everyone the product will show. Exporters and queries
# consume this instead of re-stating the filter anywhere.
PEOPLE_V_SQL = """
CREATE VIEW people_v AS
SELECT * FROM people_all_v WHERE hidden_reason IS NULL
"""

# Columns migrate() must find after running. A DB that fails this check carries a
# different CRM-unify schema (e.g. a divergent hand-applied patch) — stop loudly
# rather than write against it.
_REQUIRED_COLUMNS = {
    "interactions": ("direction",),
    "person_meta": ("follow_up_on", "follow_up_reason"),
    "calendar_plans": ("connection_id", "planned_on", "source", "source_ref"),
    # Must list every column migrate() adds: the fast path checks these, so a
    # column missing from here is a column that silently never gets added.
    "identity_merge_meta": ("follow_up_on", "follow_up_reason"),
}


def _add_column(conn, table, column, decl):
    """Add a column if it isn't there. Tolerates both 'already present' and
    'table not created yet' — the DDL above creates missing tables with the
    current shape, so there is nothing to upgrade in that case."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as e:
        message = str(e).lower()
        if "duplicate column" not in message and "no such table" not in message:
            raise


def _sql_shape(sql):
    """Whitespace-insensitive form of a CREATE VIEW statement, so formatting
    differences don't read as a changed definition."""
    return " ".join((sql or "").split())


def _schema_is_current(conn):
    """Cheap enough to run on every connection, and honest enough to be worth
    trusting: the version stamp alone would let a dropped view or a diverged
    table go unrepaired, so confirm the shape too. These are all reads — no
    schema change, no write lock, nothing for another connection to trip over."""
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        return False
    # Compare the stored definitions, not just the names: the shipped view is
    # meant to be authoritative, so a hand-edited one must still be replaced.
    stored = {r[0]: (r[1] or "") for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view'")}
    for name, want in (("people_all_v", PEOPLE_ALL_V_SQL),
                       ("people_v", PEOPLE_V_SQL)):
        if _sql_shape(stored.get(name, "")) != _sql_shape(want):
            return False
    for table, cols in _REQUIRED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not set(cols) <= have:
            return False
    return True


def migrate(conn):
    """Bring any older DB up to the current schema. Additive and idempotent:
    columns are only ever added, the backfill touches only NULLs, and the views
    are recreated so the shipped definitions are always the ones in effect.

    Runs on every connect(), so it takes a fast path once the DB is already at
    this version. That isn't only about speed: the view recreate is a schema
    change, and doing one per request invalidates prepared statements on other
    live connections — the usual source of "database schema has changed" when
    several writes land together, which is exactly what marking a run of people
    from the People screen produces."""
    if _schema_is_current(conn):
        return
    try:
        conn.executescript(CALENDAR_PLANS_DDL)
        conn.executescript(IDENTITY_JOURNAL_DDL)
    except sqlite3.OperationalError:
        # A pre-existing table of a different shape makes the indexes fail.
        # Swallow it here so the column assertion below can say what's wrong
        # in plain language instead of surfacing a raw sqlite traceback.
        pass
    _add_column(conn, "interactions", "direction", "TEXT")
    _add_column(conn, "person_meta", "follow_up_on", "TEXT")
    _add_column(conn, "person_meta", "follow_up_reason", "TEXT")
    # One-time backfill from the legacy summary strings. The match is a
    # substring test, deliberately: it reproduces exactly what the old
    # summary-scanning warmth code displayed ("sent" anywhere wins, else
    # "received"), so upgrading never loses a direction a user could already
    # see. "email exchanged" contains neither and stays NULL. Only NULL rows
    # are touched, so this can't overwrite anything set since.
    conn.execute("""UPDATE interactions SET direction='sent'
        WHERE direction IS NULL AND lower(summary) LIKE '%sent%'""")
    conn.execute("""UPDATE interactions SET direction='received'
        WHERE direction IS NULL AND lower(summary) LIKE '%received%'""")
    # The merge journal has to snapshot follow-ups too, or merging someone
    # silently destroys a date the user set and unmerge can't give it back.
    _add_column(conn, "identity_merge_meta", "follow_up_on", "TEXT")
    _add_column(conn, "identity_merge_meta", "follow_up_reason", "TEXT")
    conn.execute("DROP VIEW IF EXISTS people_v")
    conn.execute("DROP VIEW IF EXISTS people_all_v")
    conn.execute(PEOPLE_ALL_V_SQL)
    conn.execute(PEOPLE_V_SQL)
    for table, cols in _REQUIRED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        # An absent table is a failure too: migrate() creates every table it
        # needs, so if one still isn't here the DDL silently didn't take (a
        # lock, an index collision with a foreign schema) and stamping the
        # version would mark an unusable DB as migrated.
        missing = [c for c in cols if c not in have] or (
            ["the table itself"] if not have else [])
        if missing:
            raise SystemExit(
                f"schema mismatch: table '{table}' is missing column(s) "
                f"{', '.join(missing)} — this DB carries a different CRM-unify "
                f"schema. Do not proceed; run sqlite3 on the DB, capture "
                f"'.schema {table}', and report it.")
    # Stamped last, so an interrupted migration simply runs again next time.
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def connect(db_path, create=True):
    """Open the memory DB. `create=False` refuses to bring a missing file into
    existence, which is what every command wants when the user named a path:
    a typo used to produce a brand-new empty database, a confident "no contacts
    yet", and — worse — writes that landed in a file nobody would ever read."""
    if not create and not os.path.exists(db_path):
        raise SystemExit(
            f"No database at {db_path}\n"
            "Check the path. If this is a new setup, import a LinkedIn export "
            "first:  python3 scripts/linkedin_import.py")
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(CONNECTIONS_DDL)
    conn.executescript(TRELLIS_DDL)
    migrate(conn)
    return conn


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Owner identities — you and your teammates are not "contacts"
# ---------------------------------------------------------------------------

def load_owner_identities(db_path):
    """Read data/owner_identities.json (beside the DB). Shape:
    {"owner_emails": ["you@example.com", ...], "owner_domains": ["yourco.com", ...]}
    Missing file just means no exclusions; a malformed one fails loudly."""
    path = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                        "owner_identities.json")
    if not os.path.exists(path):
        return {"owner_emails": [], "owner_domains": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"Can't read {path}: {e} — fix or remove it.")
    return {
        "owner_emails": [str(e).strip().lower()
                         for e in data.get("owner_emails", []) if str(e).strip()],
        "owner_domains": [str(d).strip().lower().lstrip("@")
                          for d in data.get("owner_domains", []) if str(d).strip()],
    }


def is_owner_address(email, owner_ids):
    e = (email or "").strip().lower()
    if not e or "@" not in e or not owner_ids:
        return False
    if e in owner_ids.get("owner_emails", ()):
        return True
    return e.rsplit("@", 1)[1] in owner_ids.get("owner_domains", ())


# ---------------------------------------------------------------------------
# People: find-or-create (people live in `connections`, LinkedIn + manual)
# ---------------------------------------------------------------------------

def _natural_key(url, name, company):
    if url:
        return "url:" + url.rstrip("/").lower()
    return "nc:" + ((name or "") + "|" + (company or "")).lower()


def _canonical_person(conn, person):
    """Follow an active, confirmed merge without discarding the alias record."""
    seen = set()
    while person and person["id"] not in seen:
        seen.add(person["id"])
        marker = person["source"] or ""
        match = re.fullmatch(r"merged_into_(\d+)", marker)
        if not match:
            return person
        person = conn.execute(
            "SELECT * FROM connections WHERE id=?", (int(match.group(1)),)
        ).fetchone()
    return person


def find_person(conn, name=None, email=None, url=None):
    c = conn.cursor()
    if url:
        r = c.execute("SELECT * FROM connections WHERE lower(url)=?",
                      (url.rstrip("/").lower(),)).fetchone()
        if r:
            return _canonical_person(conn, r)
    if email:
        r = c.execute("SELECT * FROM connections WHERE email<>'' AND lower(email)=?",
                      (email.lower(),)).fetchone()
        if r:
            return _canonical_person(conn, r)
    if name:
        rows = c.execute("SELECT * FROM connections WHERE lower(full_name)=?",
                         (name.lower(),)).fetchall()
        if len(rows) == 1:
            return _canonical_person(conn, rows[0])
        if len(rows) > 1:
            raise SystemExit(
                f"'{name}' matches {len(rows)} people — pass --email or --url to "
                f"disambiguate, or run: trellis.py recall \"{name}\"")
    return None


def find_or_create_person(conn, name=None, email=None, url=None,
                          company=None, title=None, origin="manual"):
    existing = find_person(conn, name=name, email=email, url=url)
    if existing:
        # fill in any newly-supplied blanks without clobbering existing data
        fields = {}
        for col, val in (("email", email), ("company", company),
                         ("title", title), ("url", url)):
            if val and not (existing[col] or "").strip():
                fields[col] = val
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE connections SET {sets}, updated_at=? WHERE id=?",
                         (*fields.values(), now(), existing["id"]))
            conn.commit()
        return existing["id"]

    if not name and not email and not url:
        raise SystemExit("Need at least a --name (or --email/--url) to record a person.")
    name = name or (email.split("@")[0] if email else url)
    parts = name.split()
    first, last = (parts[0], " ".join(parts[1:])) if parts else (name, "")
    founder = infer_founder(title or "")
    cur = conn.execute(
        """INSERT INTO connections (natural_key, first_name, last_name, full_name,
            url, email, company, title, func, is_founder, rank, source,
            first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_natural_key(url, name, company), first, last, name, url or "", email or "",
         company or "", title or "", infer_func(title or "", founder),
         1 if founder else 0, infer_rank(title or ""),
         (origin or "manual").strip().lower(), now(), now()))
    conn.commit()
    return cur.lastrowid


def person_row(conn, cid):
    return conn.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def days_since(iso):
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso[:10]).date()
    except ValueError:
        return None
    return (TODAY - d).days


def last_interaction(conn, cid):
    return conn.execute(
        "SELECT * FROM interactions WHERE connection_id=? ORDER BY occurred_on DESC, id DESC LIMIT 1",
        (cid,)).fetchone()


def meta_for(conn, cid):
    return conn.execute("SELECT * FROM person_meta WHERE connection_id=?", (cid,)).fetchone()


# ---------------------------------------------------------------------------
# Two-layer status: priority (importance) + follow_up_on (a date-based snooze)
# ---------------------------------------------------------------------------

_FOLLOW_UP_MAX_YEARS = 10  # beyond this it's almost certainly a typo, not a plan


def _add_months(d, months):
    total = d.year * 12 + (d.month - 1) + months
    y, mo = divmod(total, 12)
    day_max = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
               31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo]
    return date(y, mo + 1, min(d.day, day_max))


def _valid_date(text):
    """A user-supplied date, normalized to YYYY-MM-DD or refused. Storing
    whatever was typed means a later reader gets None back from days_since()
    and prints it — better to catch the typo at the point of entry."""
    t = (text or "").strip()[:10]
    try:
        return date.fromisoformat(t).isoformat()
    except ValueError:
        raise SystemExit(f"'{text}' is not a date I can read. Use YYYY-MM-DD.")


def parse_follow_up(text, today=None):
    """'2026-09-01', 'in 1 week', 'in 6 months', '3 weeks' → an ISO date.
    Fails loudly (SystemExit, never a traceback) on garbage or absurd offsets."""
    today = today or TODAY
    t = (text or "").strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        try:
            date.fromisoformat(t)
        except ValueError:
            raise SystemExit(f"'{text}' is not a real date.")
        return t
    m = re.fullmatch(r"(?:in\s+)?(\d+)\s*(day|week|month|year)s?", t)
    if not m:
        raise SystemExit(
            f"Can't parse follow-up '{text}'. Use YYYY-MM-DD or a phrase like "
            "'in 1 week', 'in 6 months'.")
    n, unit = int(m.group(1)), m.group(2)
    try:
        if unit == "day":
            result = date.fromordinal(today.toordinal() + n)
        elif unit == "week":
            result = date.fromordinal(today.toordinal() + n * 7)
        elif unit == "month":
            result = _add_months(today, n)
        else:
            result = _add_months(today, n * 12)
    except (ValueError, OverflowError, IndexError):
        raise SystemExit(f"Follow-up '{text}' is too far in the future.")
    if result.year > today.year + _FOLLOW_UP_MAX_YEARS:
        raise SystemExit(
            f"Follow-up '{text}' lands in {result.year} — more than "
            f"{_FOLLOW_UP_MAX_YEARS} years out looks like a typo.")
    return result.isoformat()


def set_priority(conn, cid, priority):
    """Set the exact priority the user chose. (Bulk map sync uses cmd_apply's
    raise-never-downgrade instead; an explicit choice is authoritative.)"""
    m = meta_for(conn, cid)
    conn.execute("""INSERT INTO person_meta (connection_id, priority, mode, updated_at)
        VALUES (?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET
        priority=excluded.priority, updated_at=excluded.updated_at""",
        (cid, priority, m["mode"] if m else None, now()))


def set_follow_up(conn, cid, on, reason=None):
    """on = ISO date, or None to clear (also clears the reason)."""
    conn.execute("""INSERT INTO person_meta
        (connection_id, follow_up_on, follow_up_reason, updated_at)
        VALUES (?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET
        follow_up_on=excluded.follow_up_on,
        follow_up_reason=excluded.follow_up_reason,
        updated_at=excluded.updated_at""",
        (cid, on, reason if on else None, now()))


# Warmth buckets — same absolute thresholds skills/email-recency.md promises
# users. Distinct from CADENCE, which is mode-relative and drives radar only.
WARMTH_BUCKETS = ((14, "active"), (60, "warm"), (180, "cooling"))


def warmth_bucket(days):
    if days is None:
        return "no data"
    for cutoff, name in WARMTH_BUCKETS:
        if days <= cutoff:
            return name
    return "cold"


def warmth_rows(conn, include_muted=False):
    """One row per live connection with their aggregate contact signal.

    Read-only by design: unlike radar, this never writes suggestions, so it is
    safe to run for display as often as anyone likes. Muted contacts (bulk
    senders, not-a-person addresses) are hidden unless explicitly asked for.
    """
    # Same columns either way — include_muted just widens which view we read,
    # so the trust rules live in exactly one place (the views) and can't drift.
    source_view = "people_all_v" if include_muted else "people_v"
    sql = f"""
        SELECT p.id, p.full_name, p.company, p.title, p.email, p.url, p.source,
               p.priority, p.mode, p.follow_up_on, p.follow_up_reason,
               p.hidden_reason, agg.last_on, agg.n
        FROM {source_view} p
        LEFT JOIN (SELECT connection_id, MAX(occurred_on) AS last_on, COUNT(*) AS n
                   FROM interactions GROUP BY connection_id) agg
               ON agg.connection_id = p.id"""
    rows = []
    for r in conn.execute(sql):
        ds = days_since(r["last_on"])
        pri = r["priority"] or "normal"
        rows.append({
            "id": r["id"], "name": r["full_name"], "company": r["company"],
            "title": r["title"], "email": r["email"], "url": r["url"],
            "origin": r["source"] or "manual",
            "flagged": pri in ("important", "critical"),
            "muted": pri == "muted",
            "hidden_reason": r["hidden_reason"],
            "mode": r["mode"],
            "follow_up_on": r["follow_up_on"],
            "follow_up_due": bool(r["follow_up_on"]
                                  and r["follow_up_on"] <= TODAY.isoformat()),
            "last_contact": r["last_on"], "days_since": ds,
            "interactions": r["n"] or 0,
            "bucket": warmth_bucket(ds),
            "direction": None,
        })
    # Direction comes from the newest interaction's direction column, taken as-is:
    # a newer direction-less touch (a meeting, an "email exchanged" legacy row)
    # means we no longer know who wrote last, so NULL wins over an older value.
    # Match on the newest DATE, not the highest row id — paged sweeps ingest
    # older messages after newer ones, so id order isn't chronological.
    # Several touches can share the newest date, and ingest order is not
    # chronological — so rather than let insertion order pick a winner, agree
    # or say nothing: one direction across that day reports it, a mixed day
    # (you wrote and they wrote) reports None.
    by_id = {p["id"]: p for p in rows if p["interactions"]}
    if by_id:
        seen = {}
        for r in conn.execute("""
            SELECT i.connection_id AS cid, i.direction FROM interactions i
            JOIN (SELECT connection_id, MAX(occurred_on) AS lo
                  FROM interactions GROUP BY connection_id) x
              ON x.connection_id = i.connection_id AND i.occurred_on = x.lo"""):
            if r["cid"] in by_id:
                seen.setdefault(r["cid"], set()).add(r["direction"])
        for cid, directions in seen.items():
            by_id[cid]["direction"] = (directions.pop() if len(directions) == 1
                                       else None)
    return rows


def warmth_coverage(conn, rows):
    span = conn.execute(
        "SELECT COUNT(*) AS n, MIN(occurred_on) AS lo, MAX(occurred_on) AS hi FROM interactions"
    ).fetchone()
    sources = {r["source"] or "unknown": r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM interactions GROUP BY source")}
    measured = sum(1 for p in rows if p["interactions"])
    return {
        "contacts": len(rows),
        "contacts_with_signal": measured,
        "contacts_unmeasured": len(rows) - measured,
        "interactions": span["n"],
        "earliest": span["lo"], "latest": span["hi"],
        "by_source": sources,
    }


def _resolve_target(conn, a):
    """Resolve --name/--id to exactly one live connection row or exit loudly."""
    if getattr(a, "id", None):
        row = conn.execute(
            "SELECT * FROM connections WHERE id=? AND source NOT LIKE 'merged_into_%'",
            (a.id,)).fetchone()
        if not row:
            raise SystemExit(f"No connection with id {a.id}.")
        return row
    if not a.name:
        raise SystemExit("Pass --name (or --id).")
    rows = conn.execute(
        "SELECT * FROM connections WHERE lower(full_name)=? AND source NOT LIKE 'merged_into_%'",
        (a.name.lower(),)).fetchall()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise SystemExit(f"No one named '{a.name}'. Try: trellis.py recall \"{a.name}\"")
    ids = ", ".join(str(r["id"]) for r in rows)
    raise SystemExit(f"'{a.name}' matches {len(rows)} people (ids {ids}) — use --id.")


def cmd_mute(conn, a):
    row = _resolve_target(conn, a)
    conn.execute(
        """INSERT INTO person_meta (connection_id, priority, updated_at) VALUES (?, 'muted', ?)
           ON CONFLICT(connection_id) DO UPDATE SET priority='muted', updated_at=excluded.updated_at""",
        (row["id"], now()))
    conn.commit()
    print(f"Muted {row['full_name']} (id {row['id']}). Hidden from warmth and radar; "
          f"unmute any time: trellis.py unmute --id {row['id']}")


def cmd_unmute(conn, a):
    row = _resolve_target(conn, a)
    conn.execute(
        """INSERT INTO person_meta (connection_id, priority, updated_at) VALUES (?, 'normal', ?)
           ON CONFLICT(connection_id) DO UPDATE SET priority='normal', updated_at=excluded.updated_at""",
        (row["id"], now()))
    conn.commit()
    print(f"Unmuted {row['full_name']} (id {row['id']}).")


def _match_candidates(conn):
    """Propose LinkedIn identities for email-/calendar-created contacts.

    Cross-origin only: cmd_dupes needs a shared company to fuzzy-match, and
    contacts minted from a sweep have none, so 'brock' never meets
    'Brock Kelly' there. Proposals only — merging stays human-confirmed.
    """
    linkedin = conn.execute(
        "SELECT * FROM connections WHERE source='linkedin'").fetchall()
    strays = conn.execute("""
        SELECT c.* FROM connections c
        JOIN (SELECT DISTINCT connection_id FROM interactions) i ON i.connection_id = c.id
        WHERE c.source NOT IN ('linkedin') AND c.source NOT LIKE 'merged_into_%'""").fetchall()

    by_first = {}
    for p in linkedin:
        by_first.setdefault(_norm(p["first_name"]), []).append(p)

    # Blocking index. Comparing every stray against every LinkedIn person means
    # a fuzzy string compare per pair — 400 × 12,000 is five million of them,
    # which is minutes of work for a handful of proposals. Instead, index people
    # by the first two letters of each name part and by the shapes an email
    # local part can take, then only run the expensive comparison on people who
    # share one. Near-misses like "jon"/"john" still meet ("jo"), so this
    # narrows the search without narrowing the results.
    def _keys(text):
        """Blocking keys for one name string: its first two letters, plus the
        two after that. The second key tolerates a different FIRST letter, so
        transliterations meet ('cristina'/'kristina' both key on 'ri')."""
        out = set()
        for t in [text] + text.split():
            if len(t) >= 2:
                out.add(t[:2])
            if len(t) >= 3:
                out.add(t[1:3])
        return out

    def _prefixes(row):
        keys = set()
        for field in ("first_name", "last_name", "full_name"):
            keys |= _keys(_norm(row[field]))
        return keys

    by_prefix, by_local = {}, {}
    for p in linkedin:
        for pref in _prefixes(p):
            by_prefix.setdefault(pref, []).append(p)
        first, last = _norm(p["first_name"]), _norm(p["last_name"])
        for form in {first + last, f"{first}.{last}", first, last}:
            if form:
                by_local.setdefault(form, []).append(p)

    proposals = []
    for s in strays:
        sname = _norm(s["full_name"])
        local = (s["email"] or "").split("@")[0].lower()
        nearby = {}
        for pref in _keys(sname):
            for p in by_prefix.get(pref, ()):
                nearby[p["id"]] = p
        for form in {local, _norm_local(local), _norm_local(local).replace(".", "")}:
            for p in by_local.get(form, ()):
                nearby[p["id"]] = p
        best = []
        for p in nearby.values():
            pname = _norm(p["full_name"])
            reasons = []
            score = 0.0
            if sname and not _too_different(sname, pname):
                sim = SequenceMatcher(None, sname, pname).ratio()
                if sim >= NAME_SIMILARITY:
                    reasons.append(f"names {int(sim * 100)}% similar")
                    score = max(score, sim)
            compact = _norm(p["first_name"]) + _norm(p["last_name"])
            dotted = f"{_norm(p['first_name'])}.{_norm(p['last_name'])}"
            forms = {compact, dotted, _norm(p["first_name"]), _norm(p["last_name"])}
            # Compare the address the same way the names are normalized —
            # plenty of real addresses carry digits (john.smith2@…), and
            # comparing those raw against stripped names never matches.
            if local and ({local, _norm_local(local),
                           _norm_local(local).replace(".", "")} & forms):
                reasons.append(f"email '{local}@…' matches their name")
                # An address carrying both their names is the strongest signal
                # we have short of an exact match.
                score = max(score, 1.0 if local in (compact, dotted) else 0.95)
            if reasons:
                best.append((score, p, "; ".join(reasons)))
        # Strongest evidence first — when several people could be the match, the
        # three shown should be the three best, not the three lowest row ids.
        best.sort(key=lambda t: (-t[0], t[1]["id"]))
        best = [(p, why) for _, p, why in best]
        if not best and sname and " " not in (s["full_name"] or "").strip():
            firsts = by_first.get(sname, [])
            if len(firsts) == 1:
                best.append((firsts[0], "only LinkedIn contact with that first name"))
        for p, why in best[:3]:
            proposals.append({
                "stray_id": s["id"], "stray_name": s["full_name"],
                "stray_email": s["email"] or "",
                "linkedin_id": p["id"], "linkedin_name": p["full_name"],
                "linkedin_company": p["company"] or "",
                "why": why,
                "merge_command": f"trellis.py merge --from {s['id']} --into {p['id']}",
            })
    return proposals


def cmd_match(conn, a):
    proposals = _match_candidates(conn)
    if a.json:
        print(json.dumps(proposals, indent=2))
        return
    if not proposals:
        print("No LinkedIn matches to propose for email/calendar contacts.")
        return
    print(f"{len(proposals)} proposal(s). Nothing is merged automatically; "
          "confirm each with the command shown (reversible via unmerge):\n")
    for pr in proposals:
        email = f" <{pr['stray_email']}>" if pr["stray_email"] else ""
        company = f" ({pr['linkedin_company']})" if pr["linkedin_company"] else ""
        print(f"  {pr['stray_name']}{email}  ->  {pr['linkedin_name']}{company}")
        print(f"      because: {pr['why']}")
        print(f"      to confirm: {pr['merge_command']}\n")


def cmd_warmth(conn, a):
    rows = warmth_rows(conn, include_muted=getattr(a, "include_muted", False))
    cov = warmth_coverage(conn, rows)

    if a.name:
        needle = a.name.lower()
        rows = [p for p in rows if needle in (p["name"] or "").lower()
                or needle in (p["company"] or "").lower()
                or needle in (p["email"] or "").lower()]
    if a.bucket:
        rows = [p for p in rows if p["bucket"] == a.bucket]

    # Warmest first: measured people by recency, then the unmeasured tail.
    rows.sort(key=lambda p: (p["days_since"] is None,
                             p["days_since"] if p["days_since"] is not None else 0),
              reverse=a.stalest)
    if not a.stalest:
        rows.sort(key=lambda p: (p["days_since"] is None,
                                 p["days_since"] if p["days_since"] is not None else 10**6))

    if a.json:
        print(json.dumps({"coverage": cov, "results": rows[:a.limit] if a.limit else rows},
                         indent=2, default=str))
        return

    print(f"Coverage: {cov['interactions']} interactions "
          f"({cov['earliest'] or '—'} to {cov['latest'] or '—'}), "
          f"{cov['contacts_with_signal']} of {cov['contacts']} contacts have any signal.")
    if cov["interactions"] == 0:
        print("No contact data yet. Run the Gmail sweep (or capture interactions) first.")
        return
    for p in rows[:a.limit or 25]:
        tags = ("  [flagged]" if p["flagged"] else "") + \
               ("  [email only — not yet tied to LinkedIn]" if p["origin"] not in ("linkedin",) and p["interactions"] else "")
        if p["interactions"]:
            direction = f", they wrote last" if p["direction"] == "received" else \
                        (", you wrote last" if p["direction"] == "sent" else "")
            print(f"{p['name']}: {p['bucket']} — last contact {p['last_contact']} "
                  f"({p['days_since']}d ago{direction}); {p['interactions']} interaction(s)" + tags)
        else:
            print(f"{p['name']}: no data (unmeasured, not cold)" + tags)
    print("\n'No data' means not seen in what's been swept so far — not 'cold'.")


def label(p):
    bits = [p["full_name"]]
    if p["title"]:
        bits.append(p["title"])
    if p["company"]:
        bits.append(p["company"])
    return " · ".join(bits)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_capture(conn, a):
    cid = find_or_create_person(conn, name=a.name, email=a.email, url=a.url,
                                company=a.company, title=a.title)
    p = person_row(conn, cid)
    recorded = [f"person: {label(p)}"]
    when = _valid_date(a.date) if a.date else TODAY.isoformat()
    if a.interaction:
        conn.execute("""INSERT INTO interactions (connection_id, kind, occurred_on,
            summary, source, source_ref, confidence, created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (cid, a.kind or "note", when, a.interaction, a.source or "manual",
             a.source_ref, 1.0, now()))
        recorded.append(f"interaction ({a.kind or 'note'}, {when}): {a.interaction}")
    if a.note:
        conn.execute("""INSERT INTO notes (connection_id, content, category, created_at)
            VALUES (?,?,?,?)""", (cid, a.note, a.note_category or "context", now()))
        recorded.append(f"note: {a.note}")
    if a.loop:
        conn.execute("""INSERT INTO open_loops (connection_id, description, status,
            due_on, source, created_at) VALUES (?,?, 'open', ?, ?, ?)""",
            (cid, a.loop, a.due, a.source or "manual", now()))
        recorded.append(f"open loop: {a.loop}" + (f" (due {a.due})" if a.due else ""))
    priority = a.priority or ("important" if a.prioritize else None) \
        or ("muted" if a.deprioritize else None)
    if priority or a.mode:
        m = meta_for(conn, cid)
        pri = priority or (m["priority"] if m else "normal")
        mode = a.mode or (m["mode"] if m else None)
        conn.execute("""INSERT INTO person_meta (connection_id, priority, mode, updated_at)
            VALUES (?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET
            priority=excluded.priority, mode=excluded.mode, updated_at=excluded.updated_at""",
            (cid, pri, mode, now()))
        recorded.append(f"priority={pri}" + (f", mode={mode}" if mode else ""))
    if a.follow_up:
        due = parse_follow_up(a.follow_up)
        set_follow_up(conn, cid, due, a.follow_up_reason)
        recorded.append(f"follow up on {due}"
                        + (f" ({a.follow_up_reason})" if a.follow_up_reason else "")
                        + " — radar will surface them then, even if deprioritized")
    elif a.clear_follow_up:
        set_follow_up(conn, cid, None)
        recorded.append("follow-up cleared")
    conn.commit()
    print("Recorded:")
    for r in recorded:
        print("  •", r)


def _direction_of(ev):
    """Explicit ev["direction"] wins; else derive it from the summary the agents
    already write. Anything direction-less ("email exchanged", meetings) is NULL."""
    d = (ev.get("direction") or "").strip().lower()
    if d in ("sent", "received"):
        return d
    summary = (ev.get("summary") or "").strip().lower()
    if summary.startswith("email sent"):
        return "sent"
    if summary.startswith("email received"):
        return "received"
    return None


def _ingest_one(conn, ev, owner_ids=None):
    person = ev.get("person") or {}
    if owner_ids and is_owner_address(person.get("email"), owner_ids):
        return "owner_skipped"
    cid = find_or_create_person(
        conn, name=person.get("name"), email=person.get("email"),
        url=person.get("url"), company=person.get("company"),
        title=person.get("title"), origin=ev.get("source") or "manual")
    src = (ev.get("source") or "agent").strip().lower()
    ref = ev.get("source_ref")
    if ref:  # idempotent: skip if this exact event is already stored
        dup = conn.execute("SELECT 1 FROM interactions WHERE source=? AND source_ref=?",
                           (src, ref)).fetchone()
        if dup:
            return "skipped"
    conn.execute("""INSERT INTO interactions (connection_id, kind, occurred_on, summary,
        source, source_ref, confidence, created_at, direction)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (cid, ev.get("kind", "event"), (ev.get("date") or TODAY.isoformat())[:10],
         ev.get("summary", ""), src, ref, ev.get("confidence", 0.9), now(),
         _direction_of(ev)))
    if ev.get("open_loop"):
        conn.execute("""INSERT INTO open_loops (connection_id, description, status,
            source, source_ref, created_at) VALUES (?,?, 'open', ?,?,?)""",
            (cid, ev["open_loop"], src, ref, now()))
    return "added"


def cmd_ingest(conn, a):
    raw = a.json or (open(a.file, encoding="utf-8").read() if a.file
                     else sys.stdin.read())
    data = json.loads(raw)
    events = data if isinstance(data, list) else [data]
    owner_ids = load_owner_identities(a.db)
    added = skipped = owner_skipped = 0
    for ev in events:
        result = _ingest_one(conn, ev, owner_ids=owner_ids)
        if result == "added":
            added += 1
        elif result == "owner_skipped":
            owner_skipped += 1
        else:
            skipped += 1
    conn.commit()
    msg = f"Ingested {added} interaction(s); skipped {skipped} already-seen."
    if owner_skipped:
        msg += (f" Skipped {owner_skipped} owner/teammate address(es) per "
                "data/owner_identities.json.")
    print(msg)


def _profile(conn, p):
    out = [f"{label(p)}"]
    m = meta_for(conn, p["id"])
    if m and (m["priority"] != "normal" or m["mode"] or m["follow_up_on"]):
        out.append("  " + ", ".join(filter(None, [
            f"priority: {m['priority']}" if m["priority"] != "normal" else "",
            f"mode: {m['mode']}" if m["mode"] else "",
            (f"follow up on {m['follow_up_on']}"
             + (f" ({m['follow_up_reason']})" if m["follow_up_reason"] else ""))
            if m["follow_up_on"] else ""])))
    if p["connected_year"]:
        out.append(f"  connected on LinkedIn: {p['connected_year']}")
    ints = conn.execute("""SELECT * FROM interactions WHERE connection_id=?
        ORDER BY occurred_on DESC, id DESC LIMIT 5""", (p["id"],)).fetchall()
    if ints:
        last = ints[0]
        ds = days_since(last["occurred_on"])
        ago = f" ({ds} days ago)" if ds is not None else ""
        out.append(f"  last touch: {last['occurred_on']}{ago} — {last['summary']} "
                   f"[{last['source']}]")
        if len(ints) > 1:
            out.append(f"  recent history:")
            for i in ints[1:]:
                out.append(f"    · {i['occurred_on']} {i['summary']} [{i['source']}]")
    loops = conn.execute("""SELECT * FROM open_loops WHERE connection_id=? AND status='open'
        ORDER BY created_at""", (p["id"],)).fetchall()
    for lp in loops:
        out.append(f"  ⚠ open loop: {lp['description']}"
                   + (f" (due {lp['due_on']})" if lp["due_on"] else ""))
    ns = conn.execute("SELECT * FROM notes WHERE connection_id=? ORDER BY created_at",
                      (p["id"],)).fetchall()
    for n in ns:
        out.append(f"  note ({n['category']}): {n['content']}")
    if not ints and not loops and not ns:
        out.append("  (no interactions logged yet — from your LinkedIn graph only)")
    return "\n".join(out)


def cmd_recall(conn, a):
    q = (a.query or "").strip()
    if not q:
        raise SystemExit("Give a name, company, or keyword: trellis.py recall \"Maya\"")
    like = f"%{q.lower()}%"
    ids = [r["id"] for r in conn.execute(
        """SELECT DISTINCT c.id FROM connections c
           LEFT JOIN notes n ON n.connection_id=c.id
           LEFT JOIN interactions i ON i.connection_id=c.id
           WHERE (lower(c.full_name) LIKE ? OR lower(c.company) LIKE ?
              OR lower(c.title) LIKE ? OR lower(n.content) LIKE ?
              OR lower(i.summary) LIKE ?)
             AND c.source NOT LIKE 'merged_into_%'
           ORDER BY c.full_name LIMIT 25""",
        (like, like, like, like, like)).fetchall()]
    if not ids:
        print(f"No one found matching \"{q}\".")
        return
    print(f"{len(ids)} match(es) for \"{q}\":\n")
    for cid in ids:
        print(_profile(conn, person_row(conn, cid)))
        print()


def cmd_loops(conn, a):
    rows = conn.execute("""SELECT o.*, c.full_name, c.company FROM open_loops o
        JOIN connections c ON c.id=o.connection_id WHERE o.status='open'
        ORDER BY (o.due_on IS NULL), o.due_on, o.created_at""").fetchall()
    if a.overdue:
        rows = [r for r in rows if (r["due_on"] and r["due_on"] < TODAY.isoformat())
                or (not r["due_on"] and (days_since(r["created_at"]) or 0) > 14)]
    if not rows:
        print("No open loops. You're not leaving anyone hanging.")
        return
    print(f"{len(rows)} open loop(s) — what you owe:\n")
    for r in rows:
        due = ""
        if r["due_on"]:
            overdue = r["due_on"] < TODAY.isoformat()
            due = f"  (due {r['due_on']}{', OVERDUE' if overdue else ''})"
        who = r["full_name"] + (f" · {r['company']}" if r["company"] else "")
        print(f"  • {who}: {r['description']}{due}")


def cmd_radar(conn, a):
    """Suggest reach-outs from real relationships only — never spam the cold list."""
    limit = a.limit
    cands = {}

    def add(cid, kind, score, reason, facts):
        cur = cands.get(cid)
        if not cur or score > cur["score"]:
            cands[cid] = {"cid": cid, "kind": kind, "score": score,
                          "reason": reason, "facts": facts}

    # 0) due follow-ups — a date the user set on purpose. Highest score, listed
    #    first, and deliberately NOT gated on priority: "deprioritize, but check
    #    back in 6 months" must fire even though the person is muted.
    for r in conn.execute("""SELECT connection_id, follow_up_on, follow_up_reason
            FROM person_meta WHERE follow_up_on IS NOT NULL AND follow_up_on <= ?""",
            (TODAY.isoformat(),)).fetchall():
        p = person_row(conn, r["connection_id"])
        if not p or (p["source"] or "").startswith("merged_into_"):
            continue
        facts = [f"you asked to follow up on {r['follow_up_on']}"]
        if r["follow_up_reason"]:
            facts.append(f"reason: {r['follow_up_reason']}")
        add(r["connection_id"], "follow_up", 110,
            f"{p['full_name']} — follow-up you set is due"
            + (f": {r['follow_up_reason']}" if r["follow_up_reason"] else ""),
            facts)

    # 0b) you met, and then nothing. The whole reason to feed calendar data in:
    #     a meeting in the last MEETING_FOLLOW_UP_DAYS with no contact since and
    #     nothing already planned. Silent after the grace period, so it nudges
    #     once while it's still natural to write, not forever.
    for r in conn.execute("""
        SELECT i.connection_id AS cid, MAX(i.occurred_on) AS met_on
        FROM interactions i
        WHERE i.kind = 'meeting' AND i.occurred_on <= ? AND i.occurred_on >= ?
        GROUP BY i.connection_id""",
        (TODAY.isoformat(),
         date.fromordinal(TODAY.toordinal() - MEETING_FOLLOW_UP_DAYS).isoformat())
    ).fetchall():
        cid = r["cid"]
        p = person_row(conn, cid)
        if not p or (p["source"] or "").startswith("merged_into_"):
            continue
        m = meta_for(conn, cid)
        if m and (m["priority"] == "muted" or m["follow_up_on"]):
            continue  # already parked, or already has a date they chose
        # occurred_on is a DATE with no time, so a message on the day of the
        # meeting could be the follow-up you sent that afternoon or the agenda
        # you sent that morning — and nothing stored can tell them apart. Row
        # id is insertion order across independent ingest runs, not clock
        # order, so it can't either. Given the ambiguity, count any same-day
        # message as contact: staying quiet about someone you did write to is
        # a smaller failure than nagging them, which is the whole reason this
        # module would rather say nothing than say something wrong. The cost
        # is that an agenda-only email hides a genuine missed follow-up.
        # COALESCE because kind can be NULL, and NULL <> 'meeting' is NULL.
        since = conn.execute("""SELECT COUNT(*) FROM interactions
            WHERE connection_id=?
              AND COALESCE(kind,'') <> 'meeting'
              AND occurred_on >= ?""", (cid, r["met_on"])).fetchone()[0]
        if since:
            continue  # you've been in touch since; nothing to nudge about
        if conn.execute("""SELECT 1 FROM open_loops WHERE connection_id=?
                AND status='open'""", (cid,)).fetchone():
            continue  # an explicit loop already says what you owe them
        if conn.execute("""SELECT 1 FROM calendar_plans WHERE connection_id=?
                AND planned_on >= ?""", (cid, TODAY.isoformat())).fetchone():
            continue  # you're seeing them again anyway
        days = days_since(r["met_on"])
        if days is None:
            continue  # unparseable date: say nothing rather than "None days ago"
        when = "yesterday" if days == 1 else (
            "today" if days == 0 else f"{days} days ago")
        # Between an overdue loop (100) and a merely-open one (85): something
        # you owe and are late on comes first, but a handful of open loops
        # mustn't push a fresh meeting off the default five-item list.
        add(cid, "met_no_followup", 90,
            f"{p['full_name']} — you met {when} and haven't been in touch since",
            [f"meeting on {r['met_on']}", "no contact since, nothing scheduled"])

    # 1) open loops — highest signal (you owe something concrete)
    for r in conn.execute("""SELECT o.*, c.full_name FROM open_loops o
            JOIN connections c ON c.id=o.connection_id WHERE o.status='open'""").fetchall():
        overdue = r["due_on"] and r["due_on"] < TODAY.isoformat()
        add(r["connection_id"], "open_loop", 100 if overdue else 85,
            f"You owe {r['full_name']}: {r['description']}"
            + (" (overdue)" if overdue else ""),
            [f"open loop: {r['description']}"])

    # 2) overdue by cadence — people you actually interact with, gone quiet
    seen = {r["connection_id"] for r in conn.execute(
        "SELECT DISTINCT connection_id FROM interactions").fetchall()}
    for cid in seen:
        li = last_interaction(conn, cid)
        ds = days_since(li["occurred_on"]) if li else None
        if ds is None:
            continue
        m = meta_for(conn, cid)
        mode = m["mode"] if m else None
        pri = m["priority"] if m else "normal"
        if pri == "muted":
            continue
        cadence = CADENCE.get(mode, 120) * PRIORITY_FACTOR.get(pri, 1.0)
        if ds > cadence:
            p = person_row(conn, cid)
            score = min(80, 40 + (ds - cadence) / cadence * 30)
            add(cid, "overdue", score,
                f"{p['full_name']} — {ds} days since your last contact"
                + (f" ({mode})" if mode else ""),
                [f"last touch {li['occurred_on']}: {li['summary']}"])

    # 3) explicitly flagged to reconnect — an intentional target, surface it even with
    #    no history (a weaker signal than a concrete loop, but it's the user's own intent)
    for r in conn.execute("""SELECT connection_id, priority FROM person_meta
            WHERE priority IN ('important','critical')""").fetchall():
        cid = r["connection_id"]
        if cid in cands:
            continue  # already covered by a stronger signal
        p = person_row(conn, cid)
        if not p:
            continue
        li = last_interaction(conn, cid)
        facts = ["you flagged them to reconnect"]
        if li:
            facts.append(f"last touch {li['occurred_on']}: {li['summary']}")
        add(cid, "flagged", 70 if r["priority"] == "critical" else 60,
            f"{p['full_name']} — you flagged them to reconnect", facts)

    ranked = sorted(cands.values(), key=lambda x: x["score"], reverse=True)[:limit]
    if not ranked:
        print("No strong reach-outs right now. (Trellis stays quiet when there's "
              "nothing real — it won't invent reasons to bother people.)")
        return
    print(f"Worth reaching out to ({len(ranked)}):\n")
    for r in ranked:
        p = person_row(conn, r["cid"])
        print(f"  {p['full_name']} — score {int(r['score'])}")
        print(f"    why: {r['reason']}")
        for f in r["facts"]:
            print(f"    · {f}")
        conn.execute("""INSERT INTO suggestions (connection_id, kind, reason, score,
            facts, created_at) VALUES (?,?,?,?,?,?)""",
            (r["cid"], r["kind"], r["reason"], r["score"], json.dumps(r["facts"]), now()))
        print()
    conn.commit()


def cmd_context(conn, a):
    p = find_person(conn, name=a.name, email=a.email, url=a.url)
    if not p:
        raise SystemExit(f"No one found for '{a.name or a.email or a.url}'.")
    print(f"ALLOWED context for drafting to {label(p)}.")
    print("Use ONLY these facts. Do not invent shared history. Never send — draft only.\n")
    any_fact = False
    for i in conn.execute("""SELECT * FROM interactions WHERE connection_id=?
            ORDER BY occurred_on DESC LIMIT 8""", (p["id"],)).fetchall():
        print(f"  - {i['occurred_on']}: {i['summary']} [{i['source']}]")
        any_fact = True
    for lp in conn.execute("""SELECT * FROM open_loops WHERE connection_id=? AND status='open'""",
                           (p["id"],)).fetchall():
        print(f"  - open loop you owe: {lp['description']}")
        any_fact = True
    for n in conn.execute("SELECT * FROM notes WHERE connection_id=?", (p["id"],)).fetchall():
        print(f"  - note: {n['content']}")
        any_fact = True
    if p["connected_year"]:
        print(f"  - connected on LinkedIn in {p['connected_year']}")
        any_fact = True
    if not any_fact:
        print("  (nothing beyond their name/title/company — keep any draft light and "
              "honest; don't imply a history you don't have.)")


# Letters that are not accented forms of an ASCII letter, so Unicode
# decomposition leaves them whole and the strip below would delete them —
# turning "Þórunn" into "orunn" and losing the letter a prefix index needs most.
_LETTER_EQUIVALENTS = {
    "þ": "th", "ð": "d", "ß": "ss", "ł": "l", "ø": "o", "æ": "ae",
    "œ": "oe", "đ": "d", "ħ": "h", "ı": "i", "ŋ": "ng", "ſ": "s",
}


def _norm(s):
    """Names reduced to a comparable form. Accents are FOLDED, not dropped:
    a sweep mints the ASCII spelling of a name its owner writes with accents
    ("Angel Alvarez" in a From header, "Ángel Álvarez" on LinkedIn), and
    deleting the accented letters instead of folding them made those two
    strings share nothing to match or index on."""
    lowered = (s or "").lower()
    for letter, ascii_form in _LETTER_EQUIVALENTS.items():
        if letter in lowered:
            lowered = lowered.replace(letter, ascii_form)
    folded = unicodedata.normalize("NFKD", lowered)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", "", folded).strip()


def _norm_local(s):
    """An email local part reduced to the same alphabet the names use, so
    'john.smith2' can meet 'john.smith'."""
    return re.sub(r"[^a-z.]", "", (s or "").lower()).strip(".")


NAME_SIMILARITY = 0.82
# Duplicate hunting compares people who share a company. Up to this many, every
# pair is compared — exact, and cheap enough that essentially every real company
# group lands here. Measured on one group, all pairs: 200 people 0.3s, 500 1.8s,
# 600 ~2.6s, 1000 7s, 2000 29s. Above the threshold, fall back to comparing
# alphabetical neighbours, which is bounded but CAN miss a pair that sorts far
# apart ("Jon Smith" and "John Smith" with a dozen colleagues in between). That
# trade is announced when it happens rather than degrading quietly.
DUPE_EXACT_GROUP = 600
DUPE_NEIGHBOURS = 12


def _too_different(a, b, threshold=NAME_SIMILARITY):
    """True when two names cannot possibly reach the similarity threshold.

    SequenceMatcher's ratio is 2·matches / (len(a) + len(b)), and matches can
    never exceed the shorter string, so 2·min(la, lb) / (la + lb) is a hard
    ceiling. Checking that first is arithmetic instead of an alignment, which
    matters because the identity passes compare a lot of pairs: on a
    12,000-person graph this is the difference between minutes and seconds."""
    la, lb = len(a), len(b)
    if not la or not lb:
        return True
    return (2.0 * min(la, lb)) / (la + lb) < threshold


def _dupe_pairs(conn):
    """(a, b, evidence, confidence) tuples for possible duplicate people.
    Proposals only — nothing is merged here."""
    people = conn.execute(
        "SELECT * FROM connections WHERE source NOT LIKE 'merged_into_%'"
    ).fetchall()
    pairs = []
    # deterministic: same email or same url on different ids
    for field in ("email", "url"):
        seen = {}
        for p in people:
            v = (p[field] or "").strip().lower().rstrip("/")
            if not v:
                continue
            if v in seen and seen[v]["id"] != p["id"]:
                pairs.append((seen[v], p, f"same {field}: {v}", "likely"))
            else:
                seen[v] = p
    # fuzzy: very similar names at the same company (review only).
    #
    # Comparing every pair inside a group is quadratic, and one big employer in
    # a large network is enough to make that minutes of work. Near-identical
    # names sort next to each other ("Jon Smith" beside "John Smith"), so sort
    # the group and only compare each person with their nearest neighbours —
    # linear, and it finds the same pairs this heuristic was ever going to find.
    by_company = {}
    for p in people:
        by_company.setdefault(_norm(p["company"]), []).append(p)
    capped = []
    for comp, group in by_company.items():
        if not comp or len(group) < 2:
            continue
        ordered = sorted(group, key=lambda p: _norm(p["full_name"]))
        windowed = len(ordered) > DUPE_EXACT_GROUP
        if windowed:
            capped.append((ordered[0]["company"], len(ordered)))
        for i, a1 in enumerate(ordered):
            na = _norm(a1["full_name"])
            neighbours = (ordered[i + 1:i + 1 + DUPE_NEIGHBOURS] if windowed
                          else ordered[i + 1:])
            for b1 in neighbours:
                nb = _norm(b1["full_name"])
                if _too_different(na, nb):
                    continue
                sim = SequenceMatcher(None, na, nb).ratio()
                if sim >= NAME_SIMILARITY and a1["full_name"] != b1["full_name"]:
                    pairs.append((a1, b1, f"similar names at {a1['company']}", "possible"))
    for company, size in capped:
        print(f"  note: {company} has {size} people — checked each against their "
              f"{DUPE_NEIGHBOURS} closest names by spelling, not every pair. A "
              f"look-alike further down the list could be missed there.")
    return pairs


def cmd_dupes(conn, a):
    pairs = _dupe_pairs(conn)
    if not pairs:
        print("No likely duplicates found.")
        return
    print(f"{len(pairs)} possible duplicate(s) — confirm before merging "
          "(nothing is merged automatically):\n")
    for a1, b1, why, conf in pairs:
        print(f"  [{conf}] #{a1['id']} {label(a1)}")
        print(f"          #{b1['id']} {label(b1)}")
        print(f"          evidence: {why}")
        print(f"          if same: trellis.py merge --from {b1['id']} --into {a1['id']}\n")


def cmd_apply(conn, a):
    """Fold the Observatory map's flags + notes back into Trellis (from the
    'Sync to your agent' panel). People are matched by stable identity, not by the
    map's volatile row index."""
    raw = a.json or (open(a.file, encoding="utf-8").read() if a.file else sys.stdin.read())
    data = json.loads(raw)
    order = {"muted": 0, "normal": 1, "important": 2, "critical": 3}
    nf = nn = 0
    for f in data.get("flags", []):
        cid = find_or_create_person(conn, name=f.get("name"), email=f.get("email"),
                                    url=f.get("url"), company=f.get("company"))
        m = meta_for(conn, cid)
        cur = m["priority"] if m else "normal"
        newpri = "important" if order.get(cur, 1) < 2 else cur  # raise, never downgrade
        conn.execute("""INSERT INTO person_meta (connection_id, priority, mode, updated_at)
            VALUES (?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET
            priority=excluded.priority, updated_at=excluded.updated_at""",
            (cid, newpri, m["mode"] if m else None, now()))
        nf += 1
    for n in data.get("notes", []):
        note = (n.get("note") or "").strip()
        if not note:
            continue
        cid = find_or_create_person(conn, name=n.get("name"), email=n.get("email"),
                                    url=n.get("url"), company=n.get("company"))
        if conn.execute("SELECT 1 FROM notes WHERE connection_id=? AND content=?",
                        (cid, note)).fetchone():
            continue  # don't duplicate a note on repeated sync
        conn.execute("""INSERT INTO notes (connection_id, content, category, created_at)
            VALUES (?,?, 'context', ?)""", (cid, note, now()))
        nn += 1
    # Explicit status choices from a screen (priority / follow-up) set the exact
    # value the user picked — unlike flags above, which only ever raise.
    np = nu = 0
    for s in data.get("priorities", []):
        pri = (s.get("priority") or "").strip().lower()
        if pri not in ("muted", "normal", "important", "critical"):
            print(f"  skipping unknown priority '{s.get('priority')}' "
                  f"for {s.get('name') or s.get('email')}")
            continue
        cid = find_or_create_person(conn, name=s.get("name"), email=s.get("email"),
                                    url=s.get("url"), company=s.get("company"))
        set_priority(conn, cid, pri)
        np += 1
    for s in data.get("follow_ups", []):
        cid = find_or_create_person(conn, name=s.get("name"), email=s.get("email"),
                                    url=s.get("url"), company=s.get("company"))
        on = s.get("on")
        set_follow_up(conn, cid, parse_follow_up(on) if on else None,
                      s.get("reason"))
        nu += 1
    conn.commit()
    bits = [f"{nf} reconnect flag(s)", f"{nn} note(s)"]
    if np:
        bits.append(f"{np} priority change(s)")
    if nu:
        bits.append(f"{nu} follow-up(s)")
    print("Applied " + ", ".join(bits) + " from the map.")


def cmd_merge(conn, a):
    src = person_row(conn, a.src)
    dst = person_row(conn, a.into)
    if not src or not dst:
        raise SystemExit("Both --from and --into must be existing person ids.")
    if a.src == a.into:
        raise SystemExit("--from and --into must be different person ids.")
    if (src["source"] or "").startswith("merged_into_"):
        raise SystemExit(f"#{a.src} is already merged. Run trellis.py merges to inspect it.")
    if (dst["source"] or "").startswith("merged_into_"):
        raise SystemExit(f"#{a.into} is already merged into someone else.")

    stamp = now()
    try:
        cur = conn.execute("""INSERT INTO identity_merges
            (from_connection_id, into_connection_id, from_source, created_at)
            VALUES (?,?,?,?)""", (a.src, a.into, src["source"], stamp))
        merge_id = cur.lastrowid

        for tbl in ("interactions", "open_loops", "notes", "suggestions"):
            row_ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM {tbl} WHERE connection_id=?", (a.src,)).fetchall()]
            conn.executemany("""INSERT INTO identity_merge_items
                (merge_id, table_name, row_id) VALUES (?,?,?)""",
                [(merge_id, tbl, row_id) for row_id in row_ids])
            conn.execute(f"UPDATE {tbl} SET connection_id=? WHERE connection_id=?",
                         (a.into, a.src))

        src_meta = meta_for(conn, a.src)
        dst_meta = meta_for(conn, a.into)
        for cid, meta in ((a.src, src_meta), (a.into, dst_meta)):
            conn.execute("""INSERT INTO identity_merge_meta
                (merge_id, connection_id, existed, priority, mode, updated_at,
                 follow_up_on, follow_up_reason)
                VALUES (?,?,?,?,?,?,?,?)""",
                (merge_id, cid, 1 if meta else 0,
                 meta["priority"] if meta else None,
                 meta["mode"] if meta else None,
                 meta["updated_at"] if meta else None,
                 meta["follow_up_on"] if meta else None,
                 meta["follow_up_reason"] if meta else None))

        if src_meta:
            order = {"muted": 0, "normal": 1, "important": 2, "critical": 3}
            src_priority = src_meta["priority"] or "normal"
            dst_priority = (dst_meta["priority"] if dst_meta else None) or "normal"
            priority = max((src_priority, dst_priority), key=lambda p: order.get(p, 1))
            mode = (dst_meta["mode"] if dst_meta else None) or src_meta["mode"]
            # Keep the follow-up the user set. If both sides carry one, the
            # earlier date wins — a merge must never push a reminder later.
            dates = [m["follow_up_on"] for m in (dst_meta, src_meta)
                     if m and m["follow_up_on"]]
            follow_up_on = min(dates) if dates else None
            follow_up_reason = None
            for m in (dst_meta, src_meta):
                if m and m["follow_up_on"] == follow_up_on:
                    follow_up_reason = m["follow_up_reason"]
                    break
            conn.execute("""INSERT INTO person_meta
                (connection_id, priority, mode, updated_at, follow_up_on,
                 follow_up_reason) VALUES (?,?,?,?,?,?)
                ON CONFLICT(connection_id) DO UPDATE SET priority=excluded.priority,
                mode=excluded.mode, updated_at=excluded.updated_at,
                follow_up_on=excluded.follow_up_on,
                follow_up_reason=excluded.follow_up_reason""",
                (a.into, priority, mode, stamp, follow_up_on, follow_up_reason))
            conn.execute("DELETE FROM person_meta WHERE connection_id=?", (a.src,))

        merged_meta = meta_for(conn, a.into)
        conn.execute("""UPDATE identity_merges SET merged_meta_existed=?,
            merged_priority=?, merged_mode=?, merged_meta_updated_at=? WHERE id=?""",
            (1 if merged_meta else 0,
             merged_meta["priority"] if merged_meta else None,
             merged_meta["mode"] if merged_meta else None,
             merged_meta["updated_at"] if merged_meta else None,
             merge_id))
        conn.execute("UPDATE connections SET source='merged_into_'||?, updated_at=? WHERE id=?",
                     (a.into, stamp, a.src))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    print(f"Merged #{a.src} ({src['full_name']}) into #{a.into} ({dst['full_name']}). "
          f"History moved; the old record is marked, not deleted.")
    print(f"Merge #{merge_id} is reversible: trellis.py unmerge --merge-id {merge_id}")


def cmd_merges(conn, a):
    rows = conn.execute("""SELECT m.*, src.full_name AS from_name,
        dst.full_name AS into_name
        FROM identity_merges m
        JOIN connections src ON src.id=m.from_connection_id
        JOIN connections dst ON dst.id=m.into_connection_id
        ORDER BY m.id DESC""").fetchall()
    if not rows:
        print("No identity merges recorded.")
        return
    print(f"{len(rows)} identity merge(s):\n")
    for r in rows:
        status = f"undone {r['undone_at']}" if r["undone_at"] else "active"
        print(f"  #{r['id']}  person #{r['from_connection_id']} {r['from_name']}"
              f" → person #{r['into_connection_id']} {r['into_name']}  [{status}]")


def cmd_unmerge(conn, a):
    merge = conn.execute(
        "SELECT * FROM identity_merges WHERE id=?", (a.merge_id,)).fetchone()
    if not merge:
        raise SystemExit(f"No identity merge #{a.merge_id}.")
    if merge["undone_at"]:
        raise SystemExit(f"Identity merge #{a.merge_id} was already undone.")

    src = person_row(conn, merge["from_connection_id"])
    dst = person_row(conn, merge["into_connection_id"])
    expected_marker = f"merged_into_{merge['into_connection_id']}"
    if not src or not dst or src["source"] != expected_marker:
        raise SystemExit(
            f"Identity merge #{a.merge_id} cannot be safely undone because the "
            "person records changed after it. Inspect trellis.py merges first.")

    later = conn.execute("""SELECT id FROM identity_merges
        WHERE undone_at IS NULL AND id>? AND
        (from_connection_id IN (?,?) OR into_connection_id IN (?,?))
        ORDER BY id DESC LIMIT 1""",
        (a.merge_id, merge["from_connection_id"], merge["into_connection_id"],
         merge["from_connection_id"], merge["into_connection_id"])).fetchone()
    if later:
        raise SystemExit(
            f"Undo newer identity merge #{later['id']} first; it touches one of "
            "the same people.")

    current_src_meta = meta_for(conn, merge["from_connection_id"])
    current_dst_meta = meta_for(conn, merge["into_connection_id"])
    destination_unchanged = (
        bool(current_dst_meta) == bool(merge["merged_meta_existed"])
        and (not current_dst_meta or (
            current_dst_meta["priority"] == merge["merged_priority"]
            and current_dst_meta["mode"] == merge["merged_mode"]
            and current_dst_meta["updated_at"] == merge["merged_meta_updated_at"]))
    )
    if current_src_meta or not destination_unchanged:
        raise SystemExit(
            f"Identity merge #{a.merge_id} cannot be safely undone because relationship "
            "metadata changed after the merge. Preserve or move that newer metadata, "
            "then retry.")

    try:
        items = conn.execute("""SELECT table_name, row_id
            FROM identity_merge_items WHERE merge_id=?""", (a.merge_id,)).fetchall()
        allowed = {"interactions", "open_loops", "notes", "suggestions"}
        for item in items:
            if item["table_name"] not in allowed:
                raise RuntimeError("Unknown merge journal table; refusing unsafe undo.")
            conn.execute(
                f"UPDATE {item['table_name']} SET connection_id=? WHERE id=?",
                (merge["from_connection_id"], item["row_id"]))

        snapshots = conn.execute("""SELECT * FROM identity_merge_meta
            WHERE merge_id=?""", (a.merge_id,)).fetchall()
        for snapshot in snapshots:
            cid = snapshot["connection_id"]
            conn.execute("DELETE FROM person_meta WHERE connection_id=?", (cid,))
            if snapshot["existed"]:
                conn.execute("""INSERT INTO person_meta
                    (connection_id, priority, mode, updated_at, follow_up_on,
                     follow_up_reason) VALUES (?,?,?,?,?,?)""",
                    (cid, snapshot["priority"], snapshot["mode"],
                     snapshot["updated_at"], snapshot["follow_up_on"],
                     snapshot["follow_up_reason"]))

        conn.execute("UPDATE connections SET source=?, updated_at=? WHERE id=?",
                     (merge["from_source"], now(), merge["from_connection_id"]))
        conn.execute("UPDATE identity_merges SET undone_at=? WHERE id=?",
                     (now(), a.merge_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    print(f"Undid identity merge #{a.merge_id}. #{src['id']} ({src['full_name']}) "
          f"and #{dst['id']} ({dst['full_name']}) are separate again.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Trellis — local relationship memory.")
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="record a person / interaction / loop / note")
    cap.add_argument("--name"); cap.add_argument("--email"); cap.add_argument("--url")
    cap.add_argument("--company"); cap.add_argument("--title")
    cap.add_argument("--interaction", help="what happened (a touchpoint)")
    cap.add_argument("--kind", help="meeting|email|message|event|call|note")
    cap.add_argument("--date", help="YYYY-MM-DD (default today)")
    cap.add_argument("--note"); cap.add_argument("--note-category", dest="note_category")
    cap.add_argument("--loop", help="something you owe them / a follow-up")
    cap.add_argument("--due", help="YYYY-MM-DD for the loop")
    pri_group = cap.add_mutually_exclusive_group()
    pri_group.add_argument("--priority",
                           choices=["muted", "normal", "important", "critical"])
    pri_group.add_argument("--prioritize", action="store_true",
                           help="mark important (shorthand for --priority important)")
    pri_group.add_argument("--deprioritize", action="store_true",
                           help="mute from warmth/radar (reversible; follow-ups still fire)")
    cap.add_argument("--mode", help="collaborator|investor|friend|weak_tie|mentor|prospect")
    fu_group = cap.add_mutually_exclusive_group()
    fu_group.add_argument("--follow-up", dest="follow_up",
                          help="'in 1 week', 'in 6 months', or YYYY-MM-DD")
    fu_group.add_argument("--clear-follow-up", dest="clear_follow_up",
                          action="store_true")
    cap.add_argument("--follow-up-reason", dest="follow_up_reason")
    cap.add_argument("--source"); cap.add_argument("--source-ref", dest="source_ref")

    ing = sub.add_parser("ingest", help="add normalized event(s) as JSON")
    ing.add_argument("--json", help="inline JSON (object or array)")
    ing.add_argument("--file", help="path to a JSON file")

    rec = sub.add_parser("recall", help="who is X / when did we last talk / who at Y")
    rec.add_argument("query")

    lp = sub.add_parser("loops", help="open loops — what you owe")
    lp.add_argument("--overdue", action="store_true")

    rad = sub.add_parser("radar", help="reason-lined reach-out suggestions")
    rad.add_argument("--limit", type=int, default=5)

    wrm = sub.add_parser("warmth", help="who's warm, who's cold, who's unmeasured")
    wrm.add_argument("--name", help="filter to a person / company / email fragment")
    wrm.add_argument("--bucket", choices=["active", "warm", "cooling", "cold", "no data"])
    wrm.add_argument("--stalest", action="store_true", help="stalest first instead of warmest")
    wrm.add_argument("--limit", type=int, default=0, help="0 = all (human mode caps at 25)")
    wrm.add_argument("--include-muted", dest="include_muted", action="store_true")
    wrm.add_argument("--json", action="store_true")

    mut = sub.add_parser("mute", help="hide a non-person sender from warmth and radar")
    mut.add_argument("--name"); mut.add_argument("--id", type=int)

    unmut = sub.add_parser("unmute", help="bring a muted contact back")
    unmut.add_argument("--name"); unmut.add_argument("--id", type=int)

    mtc = sub.add_parser("match", help="propose LinkedIn identities for email/calendar contacts")
    mtc.add_argument("--json", action="store_true")

    ctx = sub.add_parser("context", help="allowed context pack for drafting")
    ctx.add_argument("--name"); ctx.add_argument("--email"); ctx.add_argument("--url")

    app = sub.add_parser("apply", help="fold the map's flags + notes into Trellis")
    app.add_argument("--json"); app.add_argument("--file")

    sub.add_parser("dupes", help="possible duplicate people to confirm")

    mrg = sub.add_parser("merge", help="merge one confirmed duplicate into another")
    mrg.add_argument("--from", dest="src", type=int, required=True)
    mrg.add_argument("--into", type=int, required=True)

    sub.add_parser("merges", help="list identity merges and their status")

    unm = sub.add_parser("unmerge", help="reverse a confirmed identity merge")
    unm.add_argument("--merge-id", type=int, required=True)

    a = ap.parse_args()
    # Creating a DB is only ever right at the default location; anywhere else,
    # a missing file means the path is wrong, not that we should mint one.
    conn = connect(a.db, create=(os.path.abspath(a.db) == os.path.abspath(DEFAULT_DB)))
    {"capture": cmd_capture, "ingest": cmd_ingest, "recall": cmd_recall,
     "loops": cmd_loops, "radar": cmd_radar, "context": cmd_context,
     "dupes": cmd_dupes, "merge": cmd_merge, "merges": cmd_merges,
     "unmerge": cmd_unmerge, "apply": cmd_apply, "warmth": cmd_warmth,
     "mute": cmd_mute, "unmute": cmd_unmute, "match": cmd_match}[a.cmd](conn, a)
    conn.close()


if __name__ == "__main__":
    main()

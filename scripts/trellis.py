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

# The one trusted read over people: live (not merged tombstones), not muted, and
# email-/calendar-minted contacts only count once they carry actual signal.
# Exporters and queries consume this view instead of re-stating the filter.
PEOPLE_V_SQL = """
CREATE VIEW people_v AS
SELECT c.id, c.natural_key, c.first_name, c.last_name, c.full_name,
       c.url, c.email, c.company, c.title, c.func, c.is_founder, c.rank,
       c.connected_year, c.connected_month, c.connected_raw,
       lower(COALESCE(NULLIF(c.source,''),'manual')) AS source,
       c.first_seen_at, c.updated_at,
       COALESCE(m.priority,'normal') AS priority, m.mode,
       m.follow_up_on, m.follow_up_reason
FROM connections c
LEFT JOIN person_meta m ON m.connection_id = c.id
WHERE lower(COALESCE(c.source,'')) NOT LIKE 'merged_into_%'
  AND COALESCE(m.priority,'normal') <> 'muted'
  AND (lower(COALESCE(c.source,'')) NOT IN ('gmail','calendar')
       OR EXISTS (SELECT 1 FROM interactions i WHERE i.connection_id = c.id))
"""

# Columns migrate() must find after running. A DB that fails this check carries a
# different CRM-unify schema (e.g. a divergent hand-applied patch) — stop loudly
# rather than write against it.
_REQUIRED_COLUMNS = {
    "interactions": ("direction",),
    "person_meta": ("follow_up_on", "follow_up_reason"),
    "calendar_plans": ("connection_id", "planned_on", "source", "source_ref"),
}


def _add_column(conn, table, column, decl):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def migrate(conn):
    """Bring any older DB up to the current schema. Additive and idempotent:
    columns are only ever added, the backfill touches only NULLs, and the view is
    recreated so the shipped definition is always the one in effect."""
    try:
        conn.executescript(CALENDAR_PLANS_DDL)
    except sqlite3.OperationalError:
        # A pre-existing table of a different shape makes the indexes fail.
        # Swallow it here so the column assertion below can say what's wrong
        # in plain language instead of surfacing a raw sqlite traceback.
        pass
    _add_column(conn, "interactions", "direction", "TEXT")
    _add_column(conn, "person_meta", "follow_up_on", "TEXT")
    _add_column(conn, "person_meta", "follow_up_reason", "TEXT")
    # One-time backfill from the legacy summary strings; already-set rows and the
    # direction-less "email exchanged" era stay untouched.
    conn.execute("""UPDATE interactions SET direction='sent'
        WHERE direction IS NULL AND lower(summary) LIKE 'email sent%'""")
    conn.execute("""UPDATE interactions SET direction='received'
        WHERE direction IS NULL AND lower(summary) LIKE 'email received%'""")
    conn.execute("DROP VIEW IF EXISTS people_v")
    conn.execute(PEOPLE_V_SQL)
    for table, cols in _REQUIRED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        missing = [c for c in cols if c not in have]
        if missing:
            raise SystemExit(
                f"schema mismatch: table '{table}' is missing column(s) "
                f"{', '.join(missing)} — this DB carries a different CRM-unify "
                f"schema. Do not proceed; run sqlite3 on the DB, capture "
                f"'.schema {table}', and report it.")
    conn.commit()


def connect(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
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
    agg_join = """
        LEFT JOIN (SELECT connection_id, MAX(occurred_on) AS last_on, COUNT(*) AS n
                   FROM interactions GROUP BY connection_id) agg
               ON agg.connection_id = {alias}.id"""
    if include_muted:
        # The only reader that deliberately looks behind the trust filter.
        sql = ("""
        SELECT c.id, c.full_name, c.company, c.title, c.email, c.url,
               lower(COALESCE(NULLIF(c.source,''),'manual')) AS source,
               COALESCE(m.priority,'normal') AS priority, m.mode,
               m.follow_up_on, m.follow_up_reason, agg.last_on, agg.n
        FROM connections c
        LEFT JOIN person_meta m ON m.connection_id = c.id"""
               + agg_join.format(alias="c")
               + " WHERE lower(COALESCE(c.source,'')) NOT LIKE 'merged_into_%'")
    else:
        sql = ("""
        SELECT p.id, p.full_name, p.company, p.title, p.email, p.url, p.source,
               p.priority, p.mode, p.follow_up_on, p.follow_up_reason,
               agg.last_on, agg.n
        FROM people_v p""" + agg_join.format(alias="p"))
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
    by_id = {p["id"]: p for p in rows if p["interactions"]}
    if by_id:
        for r in conn.execute("""
            SELECT i.connection_id AS cid, i.direction FROM interactions i
            JOIN (SELECT connection_id, MAX(occurred_on) AS lo
                  FROM interactions GROUP BY connection_id) x
              ON x.connection_id = i.connection_id AND i.occurred_on = x.lo
            ORDER BY i.id"""):
            if r["cid"] in by_id:
                by_id[r["cid"]]["direction"] = r["direction"]
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

    proposals = []
    for s in strays:
        sname = _norm(s["full_name"])
        local = (s["email"] or "").split("@")[0].lower()
        best = []
        for p in linkedin:
            pname = _norm(p["full_name"])
            reasons = []
            sim = SequenceMatcher(None, sname, pname).ratio()
            if sim >= 0.82 and sname != "":
                reasons.append(f"names {int(sim * 100)}% similar")
            compact = _norm(p["first_name"]) + _norm(p["last_name"])
            dotted = f"{_norm(p['first_name'])}.{_norm(p['last_name'])}"
            if local and local in (compact, dotted,
                                   _norm(p["first_name"]), _norm(p["last_name"])):
                reasons.append(f"email '{local}@…' matches their name")
            if reasons:
                best.append((p, "; ".join(reasons)))
        if not best and " " not in (s["full_name"] or "").strip():
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
    when = a.date or TODAY.isoformat()
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


def _norm(s):
    return re.sub(r"[^a-z ]", "", (s or "").lower()).strip()


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
    # fuzzy: very similar names at the same company (review only)
    by_company = {}
    for p in people:
        by_company.setdefault(_norm(p["company"]), []).append(p)
    for comp, group in by_company.items():
        if not comp or len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a1, b1 = group[i], group[j]
                sim = SequenceMatcher(None, _norm(a1["full_name"]),
                                      _norm(b1["full_name"])).ratio()
                if sim >= 0.82 and a1["full_name"] != b1["full_name"]:
                    pairs.append((a1, b1, f"similar names at {a1['company']}", "possible"))
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
                (merge_id, connection_id, existed, priority, mode, updated_at)
                VALUES (?,?,?,?,?,?)""",
                (merge_id, cid, 1 if meta else 0,
                 meta["priority"] if meta else None,
                 meta["mode"] if meta else None,
                 meta["updated_at"] if meta else None))

        if src_meta:
            order = {"muted": 0, "normal": 1, "important": 2, "critical": 3}
            src_priority = src_meta["priority"] or "normal"
            dst_priority = (dst_meta["priority"] if dst_meta else None) or "normal"
            priority = max((src_priority, dst_priority), key=lambda p: order.get(p, 1))
            mode = (dst_meta["mode"] if dst_meta else None) or src_meta["mode"]
            conn.execute("""INSERT INTO person_meta
                (connection_id, priority, mode, updated_at) VALUES (?,?,?,?)
                ON CONFLICT(connection_id) DO UPDATE SET priority=excluded.priority,
                mode=excluded.mode, updated_at=excluded.updated_at""",
                (a.into, priority, mode, stamp))
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
                    (connection_id, priority, mode, updated_at) VALUES (?,?,?,?)""",
                    (cid, snapshot["priority"], snapshot["mode"],
                     snapshot["updated_at"]))

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
    conn = connect(a.db)
    {"capture": cmd_capture, "ingest": cmd_ingest, "recall": cmd_recall,
     "loops": cmd_loops, "radar": cmd_radar, "context": cmd_context,
     "dupes": cmd_dupes, "merge": cmd_merge, "merges": cmd_merges,
     "unmerge": cmd_unmerge, "apply": cmd_apply, "warmth": cmd_warmth,
     "mute": cmd_mute, "unmute": cmd_unmute, "match": cmd_match}[a.cmd](conn, a)
    conn.close()


if __name__ == "__main__":
    main()

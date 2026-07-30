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
CREATE INDEX IF NOT EXISTS idx_loop_conn ON open_loops(connection_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_int_srcref
    ON interactions(source, source_ref) WHERE source_ref IS NOT NULL;
"""


def connect(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(CONNECTIONS_DDL)
    conn.executescript(TRELLIS_DDL)
    return conn


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
                          company=None, title=None):
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
         1 if founder else 0, infer_rank(title or ""), "manual", now(), now()))
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
    if a.priority or a.mode:
        m = meta_for(conn, cid)
        pri = a.priority or (m["priority"] if m else "normal")
        mode = a.mode or (m["mode"] if m else None)
        conn.execute("""INSERT INTO person_meta (connection_id, priority, mode, updated_at)
            VALUES (?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET
            priority=excluded.priority, mode=excluded.mode, updated_at=excluded.updated_at""",
            (cid, pri, mode, now()))
        recorded.append(f"priority={pri}" + (f", mode={mode}" if mode else ""))
    conn.commit()
    print("Recorded:")
    for r in recorded:
        print("  •", r)


def _ingest_one(conn, ev):
    person = ev.get("person") or {}
    cid = find_or_create_person(
        conn, name=person.get("name"), email=person.get("email"),
        url=person.get("url"), company=person.get("company"),
        title=person.get("title"))
    src, ref = ev.get("source", "agent"), ev.get("source_ref")
    if ref:  # idempotent: skip if this exact event is already stored
        dup = conn.execute("SELECT 1 FROM interactions WHERE source=? AND source_ref=?",
                           (src, ref)).fetchone()
        if dup:
            return "skipped"
    conn.execute("""INSERT INTO interactions (connection_id, kind, occurred_on, summary,
        source, source_ref, confidence, created_at) VALUES (?,?,?,?,?,?,?,?)""",
        (cid, ev.get("kind", "event"), (ev.get("date") or TODAY.isoformat())[:10],
         ev.get("summary", ""), src, ref, ev.get("confidence", 0.9), now()))
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
    added = skipped = 0
    for ev in events:
        if _ingest_one(conn, ev) == "added":
            added += 1
        else:
            skipped += 1
    conn.commit()
    print(f"Ingested {added} interaction(s); skipped {skipped} already-seen.")


def _profile(conn, p):
    out = [f"{label(p)}"]
    m = meta_for(conn, p["id"])
    if m and (m["priority"] != "normal" or m["mode"]):
        out.append("  " + ", ".join(filter(None, [
            f"priority: {m['priority']}" if m["priority"] != "normal" else "",
            f"mode: {m['mode']}" if m["mode"] else ""])))
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


def cmd_dupes(conn, a):
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
    conn.commit()
    print(f"Applied {nf} reconnect flag(s) and {nn} note(s) from the map.")


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
    cap.add_argument("--priority", choices=["muted", "normal", "important", "critical"])
    cap.add_argument("--mode", help="collaborator|investor|friend|weak_tie|mentor|prospect")
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
     "unmerge": cmd_unmerge, "apply": cmd_apply}[a.cmd](conn, a)
    conn.close()


if __name__ == "__main__":
    main()

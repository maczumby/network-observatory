#!/usr/bin/env python3
"""
warmth_export.py — render contact warmth into a single inspectable table.

The companion to the Observatory map: where the map shows who you know, this
shows how alive each relationship is, with the receipts. Reads the same
data/linkedin.db, aggregates Trellis interactions per contact, and bakes
everything into dashboard/warmth.html — one self-contained file, no network
requests, served (and password-protected) by serve.py exactly like the map.

Honesty is the design constraint: a contact with no interactions is shown as
"no data", never "cold". Coverage (how many messages, over what date range)
is printed at the top of the page, because a warmth label from a partial
sweep is only as good as the sweep.

Usage:
    python3 warmth_export.py [--db PATH] [--out PATH] [--open]

Standard library only.
"""

import argparse
import json
import os
import sqlite3
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "linkedin.db")
TEMPLATE = os.path.join(HERE, "observatory", "warmth_template.html")
DEFAULT_OUT = os.path.join(REPO_ROOT, "dashboard", "warmth.html")

sys.path.insert(0, HERE)
from trellis import warmth_bucket, days_since  # noqa: E402


def build_payload(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(
            f"DB not found: {db_path}\n"
            "Run this first:  python3 scripts/linkedin_import.py")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    has_interactions = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='interactions'"
    ).fetchone() is not None

    if not has_interactions:
        total = conn.execute(
            "SELECT COUNT(*) FROM connections").fetchone()[0]
        conn.close()
        return {"people": [], "receipts": {}, "coverage": {
            "contacts": total, "contacts_with_signal": 0,
            "contacts_unmeasured": total, "muted_hidden": 0, "interactions": 0,
            "earliest": None, "latest": None, "by_source": {}}}

    people = []
    muted = 0
    for r in conn.execute("""
        SELECT c.id, c.full_name, c.company, c.title, c.email, c.url, c.source,
               m.priority, agg.last_on, agg.n
        FROM connections c
        LEFT JOIN person_meta m ON m.connection_id = c.id
        LEFT JOIN (SELECT connection_id, MAX(occurred_on) AS last_on, COUNT(*) AS n
                   FROM interactions GROUP BY connection_id) agg
               ON agg.connection_id = c.id
        WHERE c.source NOT LIKE 'merged_into_%'"""):
        if (r["priority"] or "normal") == "muted":
            muted += 1
            continue
        ds = days_since(r["last_on"])
        people.append({
            "id": r["id"], "name": r["full_name"] or "(unnamed)",
            "company": r["company"] or "", "title": r["title"] or "",
            "url": r["url"] or "",
            "origin": r["source"] or "manual",
            "flag": 1 if (r["priority"] or "normal") in ("important", "critical") else 0,
            "last": r["last_on"], "days": ds,
            "n": r["n"] or 0, "bucket": warmth_bucket(ds),
        })

    # Receipts: every interaction for everyone with signal. This is who/when
    # provenance only — summaries here are things like "email received", never
    # message content, because ingest never stores content.
    receipts = {}
    for r in conn.execute("""
        SELECT connection_id AS cid, occurred_on, kind, source, summary
        FROM interactions ORDER BY occurred_on DESC, id DESC"""):
        receipts.setdefault(str(r["cid"]), []).append({
            "on": r["occurred_on"], "kind": r["kind"] or "event",
            "source": r["source"] or "", "summary": r["summary"] or "",
        })

    span = conn.execute(
        "SELECT COUNT(*) AS n, MIN(occurred_on) AS lo, MAX(occurred_on) AS hi"
        " FROM interactions").fetchone()
    by_source = {row["source"] or "unknown": row["n"] for row in conn.execute(
        "SELECT source, COUNT(*) AS n FROM interactions GROUP BY source")}
    conn.close()

    measured = sum(1 for p in people if p["n"])
    return {
        "people": people,
        "receipts": receipts,
        "coverage": {
            "contacts": len(people),
            "contacts_with_signal": measured,
            "contacts_unmeasured": len(people) - measured,
            "muted_hidden": muted,
            "interactions": span["n"],
            "earliest": span["lo"], "latest": span["hi"],
            "by_source": by_source,
        },
    }


def render(payload, out_path):
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read()
    if "__WARMTH_DATA__" not in template:
        raise SystemExit(f"Template is missing the __WARMTH_DATA__ token: {TEMPLATE}")
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(template.replace("__WARMTH_DATA__", blob))


def main():
    ap = argparse.ArgumentParser(description="Bake the warmth table from the memory DB.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--open", action="store_true", help="open the result in a browser")
    args = ap.parse_args()

    payload = build_payload(args.db)
    render(payload, args.out)
    cov = payload["coverage"]
    print(f"Wrote {args.out}")
    print(f"  {cov['contacts']} contacts; {cov['contacts_with_signal']} with contact "
          f"signal, {cov['contacts_unmeasured']} unmeasured; "
          f"{cov['interactions']} interactions"
          + (f" ({cov['earliest']} to {cov['latest']})" if cov["interactions"] else ""))
    if args.open:
        webbrowser.open("file://" + os.path.abspath(args.out))


if __name__ == "__main__":
    main()

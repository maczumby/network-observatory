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
import os
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "linkedin.db")
TEMPLATE = os.path.join(HERE, "observatory", "warmth_template.html")
DEFAULT_OUT = os.path.join(REPO_ROOT, "dashboard", "warmth.html")

sys.path.insert(0, HERE)
import trellis  # noqa: E402
from observatory import common  # noqa: E402


def build_payload(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(
            f"DB not found: {db_path}\n"
            "Run this first:  python3 scripts/linkedin_import.py")
    conn = trellis.connect(db_path)  # migrates; people_v is always present

    people = []
    for p in common.load_people(conn):
        people.append({
            "id": p["id"], "name": p["name"],
            "company": p["company"], "title": p["title"],
            "url": p["url"],
            "origin": p["origin"],
            "flag": p["flag"],
            "due": 1 if p["follow_up_due"] else 0,
            "follow_up_on": p["follow_up_on"],
            "last": p["last_on"], "days": p["days"],
            "n": p["n"], "bucket": p["bucket"],
        })
    hidden = common.hidden_counts(conn)

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
            "muted_hidden": hidden["muted"],
            "unreconciled_hidden": hidden["no_signal"],
            "interactions": span["n"],
            "earliest": span["lo"], "latest": span["hi"],
            "by_source": by_source,
        },
    }


def render(payload, out_path):
    common.render_page(TEMPLATE, "__WARMTH_DATA__", payload,
                       out_path, active="warmth")


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

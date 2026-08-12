#!/usr/bin/env python3
"""
reconcile.py — regenerate the identity/quality review queues from the DB.

The graph accumulates integrity debt as sweeps run: email-minted contacts that
are really LinkedIn people you already know, addresses that aren't people at
all, and your own/teammates' addresses counted as "contacts". This script
turns all of that into reviewable JSON queues under data/ — reproducibly, from
code, so the queues are never mystery artifacts.

Default run is PROPOSE-ONLY: it writes the queue files and prints a summary,
touching nothing else. `--apply` performs only the safe, reversible subset:
muting owner/teammate addresses and confident non-people (undo any time with
`trellis.py unmute --id N`). Identity merges are never applied here — each one
is a human decision, confirmed via the `merge_command` shown in the queue and
reversible through the merge journal (`trellis.py unmerge`).

Outputs (in data/, beside the DB — gitignored, they contain your contacts):
  linkedin_identity_review_queue.json  email/calendar contacts <-> LinkedIn proposals
  contact_quality_review.json          person / non_person / ambiguous verdicts

Standard library only.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import trellis  # noqa: E402
from contact_quality import classify_contact  # noqa: E402

# TODO(pre-flight): priority_email_candidates.json is still generated only by
# the original agent-run workflow. Its shape must be confirmed against a real
# sample before this script grows a generator for it — guessing a shape an
# agent already consumes would be worse than the loud gap.


def identity_queue(conn):
    proposals = trellis._match_candidates(conn)
    for a1, b1, why, conf in trellis._dupe_pairs(conn):
        proposals.append({
            "stray_id": b1["id"], "stray_name": b1["full_name"],
            "stray_email": b1["email"] or "",
            "linkedin_id": a1["id"], "linkedin_name": a1["full_name"],
            "linkedin_company": a1["company"] or "",
            "why": f"[{conf}] {why}",
            "merge_command": f"trellis.py merge --from {b1['id']} --into {a1['id']}",
        })
    # One entry per (stray, candidate); stable order, NULL-name safe.
    seen = set()
    unique = []
    for p in proposals:
        key = (p["stray_id"], p["linkedin_id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    unique.sort(key=lambda p: ((p["stray_name"] or "").lower(), p["stray_id"],
                               p["linkedin_id"]))
    return unique


def quality_queue(conn, owner_ids):
    """Classify every live non-LinkedIn contact. Ambiguous first — those are
    the ones that need a human eye."""
    rows = conn.execute("""
        SELECT c.id, c.full_name, c.email,
               lower(COALESCE(NULLIF(c.source,''),'manual')) AS source,
               COALESCE(m.priority,'normal') AS priority,
               (SELECT COUNT(*) FROM interactions i WHERE i.connection_id=c.id) AS n
        FROM connections c
        LEFT JOIN person_meta m ON m.connection_id = c.id
        WHERE lower(COALESCE(c.source,'')) NOT LIKE 'merged_into_%'
          AND lower(COALESCE(c.source,'')) NOT IN ('linkedin')""").fetchall()
    out = []
    for r in rows:
        entry = classify_contact(r["full_name"], r["email"])
        if trellis.is_owner_address(r["email"], owner_ids):
            entry = {"verdict": "owner",
                     "signals": ["your own / a teammate's address "
                                 "(data/owner_identities.json)"]}
        if entry["verdict"] == "person":
            continue  # nothing to review
        out.append({
            "id": r["id"], "name": r["full_name"], "email": r["email"] or "",
            "origin": r["source"], "interactions": r["n"],
            "muted": r["priority"] == "muted",
            "verdict": entry["verdict"], "signals": entry["signals"],
            "mute_command": f"trellis.py mute --id {r['id']}",
        })
    order = {"ambiguous": 0, "non_person": 1, "owner": 2}
    out.sort(key=lambda e: (order.get(e["verdict"], 9),
                            (e["name"] or "").lower(), e["id"]))
    return out


def write_queue(data_dir, name, entries):
    path = os.path.join(data_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def apply_safe_mutes(conn, quality):
    """Reversible only: mute owner addresses + confident non-people that are
    not already muted. Ambiguous entries are never touched."""
    muted = []
    for e in quality:
        if e["muted"] or e["verdict"] not in ("non_person", "owner"):
            continue
        trellis.set_priority(conn, e["id"], "muted")
        muted.append(e)
    conn.commit()
    return muted


def main():
    ap = argparse.ArgumentParser(
        description="Regenerate identity/quality review queues (propose-only by default).")
    ap.add_argument("--db", default=trellis.DEFAULT_DB)
    ap.add_argument("--apply", action="store_true",
                    help="mute owner addresses + confident non-people (reversible)")
    ap.add_argument("--json", action="store_true", help="print the queues too")
    a = ap.parse_args()

    conn = trellis.connect(a.db)
    data_dir = os.path.dirname(os.path.abspath(a.db))
    owner_ids = trellis.load_owner_identities(a.db)

    identity = identity_queue(conn)
    quality = quality_queue(conn, owner_ids)
    p1 = write_queue(data_dir, "linkedin_identity_review_queue.json", identity)
    p2 = write_queue(data_dir, "contact_quality_review.json", quality)

    ambiguous = sum(1 for e in quality if e["verdict"] == "ambiguous")
    confident = sum(1 for e in quality
                    if e["verdict"] in ("non_person", "owner") and not e["muted"])
    print(f"Identity queue: {len(identity)} proposal(s) -> {p1}")
    print(f"Quality queue:  {len(quality)} entr(ies) ({ambiguous} need your eye, "
          f"{confident} confidently mutable) -> {p2}")

    if a.apply:
        muted = apply_safe_mutes(conn, quality)
        if muted:
            print(f"\nMuted {len(muted)} (reversible with trellis.py unmute --id N):")
            for e in muted:
                print(f"  #{e['id']} {e['name']} <{e['email']}> — {e['signals'][0]}")
        else:
            print("\nNothing to mute — owner addresses and confident non-people "
                  "are already handled.")
    elif confident:
        print("\nNothing changed (propose-only). Re-run with --apply to mute the "
              "confident non-people; identity merges always stay per-decision.")

    if a.json:
        print(json.dumps({"identity": identity, "quality": quality}, indent=2,
                         ensure_ascii=False))
    conn.close()


if __name__ == "__main__":
    main()

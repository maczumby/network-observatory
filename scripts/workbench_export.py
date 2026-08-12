#!/usr/bin/env python3
"""
workbench_export.py — the People screen: your contacts as a working list.

Where the map shows the shape of your network and Warmth shows the signal,
the Workbench is where you act on it: prioritize or deprioritize someone,
snooze them to a date, and confirm "this email contact is that LinkedIn
person". Served live (serve.py --rw) the controls write straight to Trellis;
opened as a file they queue locally and emit a paste-block for your agent —
the same contract as the map's sync panel.

Counts are computed with correlated subqueries on purpose: joining
interactions × open_loops × calendar_plans in one query multiplies the rows
(3 touches × 2 loops × 2 plans would read as 12/8/8).

Usage:
    python3 workbench_export.py [--db PATH] [--out PATH] [--open]

Standard library only.
"""

import argparse
import json
import os
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "linkedin.db")
TEMPLATE = os.path.join(HERE, "observatory", "workbench_template.html")
DEFAULT_OUT = os.path.join(REPO_ROOT, "dashboard", "workbench.html")

sys.path.insert(0, HERE)
import trellis  # noqa: E402
from observatory import common  # noqa: E402


def build_payload(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(
            f"DB not found: {db_path}\n"
            "Run this first:  python3 scripts/linkedin_import.py")
    conn = trellis.connect(db_path)  # migrates; people_v is always present
    today = trellis.TODAY.isoformat()

    people = []
    for r in conn.execute("""
        SELECT p.*,
          (SELECT COUNT(*) FROM interactions i WHERE i.connection_id = p.id) AS n,
          (SELECT MAX(occurred_on) FROM interactions i WHERE i.connection_id = p.id) AS last_on,
          (SELECT kind FROM interactions i WHERE i.connection_id = p.id
           ORDER BY occurred_on DESC, id DESC LIMIT 1) AS last_kind,
          (SELECT COUNT(*) FROM open_loops o
           WHERE o.connection_id = p.id AND o.status = 'open') AS loops,
          (SELECT COUNT(*) FROM calendar_plans cp
           WHERE cp.connection_id = p.id AND cp.planned_on >= ?) AS plans
        FROM people_v p
        ORDER BY p.full_name COLLATE NOCASE""", (today,)):
        ds = trellis.days_since(r["last_on"])
        status = common.status_of(r["priority"], r["follow_up_on"], today)
        people.append({
            "id": r["id"],
            "key": common.person_key(r["url"], r["full_name"], r["company"]),
            "name": r["full_name"] or "(unnamed)",
            "company": r["company"] or "", "title": r["title"] or "",
            "url": r["url"] or "", "email": r["email"] or "",
            "origin": r["source"] or "manual",
            "priority": r["priority"] or "normal",
            "flag": status["flag"],
            "due": status["due"],
            "follow_up_on": r["follow_up_on"],
            "follow_up_reason": r["follow_up_reason"],
            "last": r["last_on"], "last_kind": r["last_kind"],
            "days": ds, "n": r["n"], "loops": r["loops"], "plans": r["plans"],
            "bucket": trellis.warmth_bucket(ds),
        })

    conn.close()

    # Identity proposals, grouped by the email-/calendar-minted contact so the
    # working row can offer "same person as…?" with the evidence.
    #
    # Read from the queue reconcile.py writes rather than recomputing it here:
    # matching is a whole-graph pass, and rebuilding a page shouldn't pay for
    # it. It also means the screen offers exactly the proposals the agent is
    # looking at, instead of a second opinion computed a moment later.
    # The file is data we didn't necessarily write — another agent's reconcile
    # may use a different shape, and a half-written one is always possible. A
    # page build must survive anything in it, so validate rather than assume:
    # unreadable input means "no proposals", never a traceback.
    candidates = {}
    queue_path = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                              "linkedin_identity_review_queue.json")
    unreadable = False
    proposals = []
    if os.path.exists(queue_path):
        try:
            with open(queue_path, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, list):
            proposals = [p for p in loaded if isinstance(p, dict)]
            unreadable = len(proposals) != len(loaded)
        else:
            unreadable = True

    # Only offer a merge between two people the user can actually see. Widening
    # this to every non-tombstoned row looked like a fix — it rescued a
    # proposal whose target had been deprioritized — but merging INTO a hidden
    # person moves the visible one's history onto a row nobody can see, so both
    # disappear from every screen. A proposal you can't act on is a smaller
    # problem than a button that makes two people vanish.
    live = {p["id"] for p in people}
    dropped = 0
    for prop in proposals:
        try:
            entry = {
                "linkedin_id": int(prop["linkedin_id"]),
                "linkedin_name": prop["linkedin_name"],
                "linkedin_company": prop.get("linkedin_company", ""),
                "why": prop.get("why", ""),
                "merge_command": prop.get("merge_command", ""),
            }
            stray_id = int(prop["stray_id"])
        except (KeyError, TypeError, ValueError):
            unreadable = True
            continue
        # A proposal for someone who has since been merged away (or muted) can
        # only fail when clicked. Build the entry first so a bad one can't leave
        # an empty group behind.
        if stray_id not in live or entry["linkedin_id"] not in live:
            dropped += 1
            continue
        candidates.setdefault(str(stray_id), []).append(entry)

    return {"people": people, "candidates": candidates, "today": today,
            "candidates_generated": os.path.exists(queue_path) and not unreadable,
            "candidates_unreadable": unreadable,
            "candidates_dropped": dropped}


def render(payload, out_path):
    return common.render_page(TEMPLATE, "__WORKBENCH_DATA__", payload,
                              out_path, active="workbench")


def main():
    ap = argparse.ArgumentParser(description="Bake the People workbench from the memory DB.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--open", action="store_true", help="open the result in a browser")
    args = ap.parse_args()

    payload = build_payload(args.db)
    render(payload, args.out)
    flagged = sum(1 for p in payload["people"] if p["flag"])
    due = sum(1 for p in payload["people"] if p["due"])
    print(f"Wrote {args.out}")
    print(f"  {len(payload['people'])} people; {flagged} prioritized, {due} follow-ups due, "
          f"{len(payload['candidates'])} with identity proposals")
    if not payload["candidates"]:
        print("  (no identity proposals loaded — run scripts/reconcile.py to "
              "generate them, then rebuild)")
    if args.open:
        webbrowser.open("file://" + os.path.abspath(args.out))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
calendar_crm.py — fold calendar events into the relationship graph.

One meeting, one model: an event that already happened becomes an interaction
(kind=meeting, "meeting held") on each external attendee, exactly like the
published calendar rules teach agents; an event still ahead becomes a row in
`calendar_plans`, so "you're seeing X on Thursday" and "you met X last week —
follow up?" both come from the same ingest.

Privacy contract (same as the skill pack): who and when ONLY. Titles,
descriptions, and locations are never stored. Events with more than
MAX_ATTENDEES external attendees are skipped — big invites mint acquaintances,
not relationships. Owner/teammate addresses (data/owner_identities.json) and
non-person addresses (rooms, no-reply calendars) are skipped on planned AND
held events alike.

Input: JSON (array or object) of normalized events the agent fetched:
  {"event_id": "abc123", "date": "2026-08-14",
   "attendees": [{"name": "Ada Lovelace", "email": "ada@example.com"}, ...]}

Idempotent: held events dedupe on interactions(source, source_ref); plans
upsert on calendar_plans(source, source_ref). source_ref is
"<event_id>:<attendee-email>". Stale plan rows (a planned date now past) are
left in place — readers filter on planned_on >= today; nothing is deleted.

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

MAX_ATTENDEES = 10


def _attendee_ref(event_id, email):
    return f"{event_id}:{(email or '').strip().lower()}"


def ingest_records(conn, records, owner_ids=None, today=None):
    """Returns a counters dict; commits once at the end."""
    today = today or trellis.TODAY.isoformat()
    owner_ids = owner_ids or {"owner_emails": [], "owner_domains": []}
    counts = {"held": 0, "planned": 0, "skipped_dup": 0, "skipped_owner": 0,
              "skipped_nonperson": 0, "skipped_big": 0, "skipped_bad": 0}

    for ev in records:
        event_id = (ev.get("event_id") or "").strip()
        date = (ev.get("date") or "").strip()[:10]
        attendees = ev.get("attendees") or []
        if not event_id or not date:
            counts["skipped_bad"] += 1
            continue

        external = []
        for att in attendees:
            email = (att.get("email") or "").strip().lower()
            name = (att.get("name") or "").strip()
            if not email and not name:
                continue
            if trellis.is_owner_address(email, owner_ids):
                counts["skipped_owner"] += 1
                continue
            if classify_contact(name, email)["verdict"] == "non_person":
                counts["skipped_nonperson"] += 1
                continue
            external.append({"name": name, "email": email})

        if len(external) > MAX_ATTENDEES:
            counts["skipped_big"] += 1
            continue

        for att in external:
            ref = _attendee_ref(event_id, att["email"] or att["name"])
            if date > today:
                cid = trellis.find_or_create_person(
                    conn, name=att["name"] or None, email=att["email"] or None,
                    origin="calendar")
                conn.execute("""INSERT INTO calendar_plans
                    (connection_id, planned_on, source, source_ref, created_at)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(source, source_ref) WHERE source_ref IS NOT NULL
                    DO UPDATE SET planned_on=excluded.planned_on""",
                    (cid, date, "calendar", ref, trellis.now()))
                counts["planned"] += 1
            else:
                result = trellis._ingest_one(conn, {
                    "person": {"name": att["name"] or None,
                               "email": att["email"] or None},
                    "kind": "meeting", "date": date, "summary": "meeting held",
                    "source": "calendar", "source_ref": ref,
                }, owner_ids=owner_ids)
                if result == "added":
                    counts["held"] += 1
                elif result == "owner_skipped":
                    counts["skipped_owner"] += 1
                else:
                    counts["skipped_dup"] += 1
    conn.commit()
    return counts


def main():
    ap = argparse.ArgumentParser(
        description="Ingest normalized calendar events (who/when only).")
    ap.add_argument("--db", default=trellis.DEFAULT_DB)
    ap.add_argument("--json", help="inline JSON (object or array)")
    ap.add_argument("--file", help="path to a JSON file")
    a = ap.parse_args()

    raw = a.json or (open(a.file, encoding="utf-8").read() if a.file
                     else sys.stdin.read())
    data = json.loads(raw)
    records = data if isinstance(data, list) else [data]

    conn = trellis.connect(
        a.db, create=(os.path.abspath(a.db) == os.path.abspath(trellis.DEFAULT_DB)))
    counts = ingest_records(conn, records,
                            owner_ids=trellis.load_owner_identities(a.db))
    conn.close()

    print(f"Calendar: {counts['held']} held meeting(s) recorded, "
          f"{counts['planned']} upcoming plan(s) upserted.")
    skips = {k: v for k, v in counts.items() if k.startswith("skipped_") and v}
    if skips:
        labels = {"skipped_dup": "already seen", "skipped_owner": "owner/teammate",
                  "skipped_nonperson": "non-person address",
                  "skipped_big": f"more than {MAX_ATTENDEES} attendees",
                  "skipped_bad": "missing event_id/date"}
        print("Skipped: " + ", ".join(f"{v} {labels[k]}" for k, v in skips.items()))


if __name__ == "__main__":
    main()

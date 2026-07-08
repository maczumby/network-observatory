#!/usr/bin/env python3
"""
observatory_export.py — render the LinkedIn memory DB into the Observatory.

Powers the /observatory skill. Reads <repo>/data/linkedin.db (written by
linkedin_import.py), shapes each connection into the record the visual needs,
and bakes it into a single self-contained HTML file you can double-click.

The visual is a standalone port of the Claude Design "Network memory visual
explorer" (template.html in this folder). Everything the design inferred
(function, seniority) was computed at import time and just passed through here.

Usage:
    python3 observatory_export.py [--db PATH] [--out PATH] [--open]

No third-party packages — standard library only (Python 3.8+).
"""

import argparse
import json
import os
import sqlite3
import webbrowser
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "linkedin.db")
TEMPLATE = os.path.join(HERE, "observatory", "template.html")
DEFAULT_OUT = os.path.join(REPO_ROOT, "dashboard", "observatory.html")


def load_people(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(
            f"DB not found: {db_path}\n"
            "Run this first:  python3 scripts/linkedin_import.py")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Trellis tables may not exist yet (observatory can run before trellis is used).
    has_trellis = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='person_meta'"
    ).fetchone() is not None
    if has_trellis:
        rows = conn.execute("""
            SELECT c.*, pm.priority AS tr_priority,
                   (SELECT content FROM notes n WHERE n.connection_id=c.id
                    ORDER BY n.created_at DESC LIMIT 1) AS tr_note
            FROM connections c LEFT JOIN person_meta pm ON pm.connection_id=c.id
            WHERE c.source NOT LIKE 'merged_into_%'
            ORDER BY c.connected_year DESC, c.id DESC""").fetchall()
    else:
        rows = conn.execute("""
            SELECT *, NULL AS tr_priority, NULL AS tr_note
            FROM connections ORDER BY connected_year DESC, id DESC""").fetchall()
    conn.close()
    people = []
    for r in rows:
        people.append({
            "name": r["full_name"],
            "first": r["first_name"] or "",
            "last": r["last_name"] or "",
            "company": r["company"] or "",
            "title": r["title"] or "",
            "func": r["func"] or "Other",
            "is_founder": int(r["is_founder"] or 0),
            "rank": r["rank"] or 2,
            "year": r["connected_year"],
            "month": r["connected_month"],
            "url": r["url"] or "",
            "email": r["email"] or "",
            # Trellis state baked in so the map reflects your memory:
            "flag": 1 if (r["tr_priority"] in ("important", "critical")) else 0,
            "note": r["tr_note"] or "",
        })
    return people


# Recognizable brands worth surfacing as a "you know a crowd here" insight.
# Purely for the reading's first card; nothing here changes the data.
NOTABLE_BRANDS = {
    "Google", "Alphabet", "Meta", "Facebook", "Apple", "Amazon", "Microsoft",
    "Netflix", "Nvidia", "OpenAI", "Anthropic", "Stripe", "Airbnb", "Uber",
    "Lyft", "Salesforce", "Adobe", "LinkedIn", "Twitter", "X", "Coinbase",
    "Databricks", "Figma", "Notion", "Snowflake", "Tesla", "SpaceX", "Spotify",
    "Goldman Sachs", "McKinsey & Company", "Bain & Company", "Deloitte",
    "Sequoia Capital", "Andreessen Horowitz",
}


def pick_notable(companies):
    """The recognizable brand the person knows the most people at (or '')."""
    present = [(c, n) for c, n in companies.items() if c in NOTABLE_BRANDS]
    return max(present, key=lambda t: t[1])[0] if present else ""


def build_payload(people):
    years = [p["year"] for p in people if p["year"]]
    y_min, y_max = (min(years), max(years)) if years else (None, None)
    span = f"’{str(y_min)[2:]}–’{str(y_max)[2:]}" if y_min else ""
    companies = Counter(p["company"] for p in people if p["company"])
    notable = pick_notable(companies)
    return {
        "people": people,
        "stats": {"people": len(people),
                  "companies": len(companies),
                  "span": span},
        "insights": {"notableCo": notable},
        "config": {"defaultLayout": "field", "defaultDimension": "company",
                   "glowIntensity": 0.9, "pointSize": 1},
    }


def render(payload, out_path):
    tpl = open(TEMPLATE, encoding="utf-8").read()
    # compact JSON, safe for embedding inside a <script> block
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = (data.replace("</", "<\\/")
                .replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029"))
    if "__OBSERVATORY_DATA__" not in tpl:
        raise SystemExit("Template is missing the __OBSERVATORY_DATA__ placeholder.")
    html = tpl.replace("__OBSERVATORY_DATA__", data)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return len(html)


def build_summary(people, payload):
    """A short, plain-language readout an agent can paste into a chat."""
    n = len(people)
    span = payload["stats"]["span"]
    companies = Counter(p["company"] for p in people if p["company"])
    top = companies.most_common(5)
    years = [p["year"] for p in people if p["year"]]
    ymax = max(years) if years else None
    reconn = sum(1 for p in people
                 if p["year"] and p["year"] <= 2016 and (p["rank"] or 0) >= 6)
    solo = sum(1 for _, k in companies.items() if k == 1)
    senior = sum(1 for p in people if (p["rank"] or 0) >= 6)
    notable = payload["insights"]["notableCo"]

    lines = [f"Your network map is ready — {n:,} connections spanning {span}."]
    if top:
        lines.append("Biggest circles: "
                     + ", ".join(f"{c} ({k})" for c, k in top) + ".")
    bits = []
    if notable and companies.get(notable):
        bits.append(f"{companies[notable]} now at {notable}")
    if reconn:
        bits.append(f"{reconn} from your early days who are senior now "
                    "(worth reconnecting)")
    if solo:
        bits.append(f"{solo} companies where you're the only one you know")
    if senior:
        bits.append(f"{senior} now director-level or above")
    if bits:
        lines.append("Worth a look: " + "; ".join(bits) + ".")
    lines.append(
        "Explore it three ways — Groups (clusters that share an attribute, with "
        "constellation lines when you hover one), Timeline (you at the center, time "
        "as distance), and Ranked (columns by how many people you know). Color by "
        "company, role, seniority, or era. Search anyone, and click a star for "
        "details, a private note, or to flag a reconnect.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--open", action="store_true", help="open in browser when done")
    args = ap.parse_args()

    people = load_people(args.db)
    payload = build_payload(people)
    size = render(payload, args.out)

    s = payload["stats"]
    print(f"  Connections   : {s['people']}")
    print(f"  Companies      : {s['companies']}")
    print(f"  Span           : {s['span']}")
    print(f"  Notable pick   : {payload['insights']['notableCo']}")
    print(f"  Wrote          : {args.out}  ({size//1024} KB)")

    # Plain readout to hand the user (paste into chat, or read aloud).
    print("\n----- share this with the user -----")
    print(build_summary(people, payload))
    print("------------------------------------")
    print(f"\nSend the user this file to open: {args.out}")
    if args.open:
        webbrowser.open("file://" + os.path.abspath(args.out))


if __name__ == "__main__":
    main()

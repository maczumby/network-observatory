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
    rows = conn.execute("""
        SELECT full_name, first_name, last_name, company, title, func,
               is_founder, rank, connected_year, connected_month, url, email
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
    if args.open:
        webbrowser.open("file://" + os.path.abspath(args.out))


if __name__ == "__main__":
    main()

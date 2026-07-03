#!/usr/bin/env python3
"""
linkedin_import.py — turn a LinkedIn data export into a local SQLite memory DB.

Powers the /linkedin-import skill. Reads Connections.csv out of a LinkedIn
"Basic" export (a .zip, an unzipped folder, or a direct Connections.csv path),
infers a function + seniority rank from each job title, and upserts everything
into a single portable SQLite file.

Design notes:
  - SQLite, one file, no server. Portable, re-runnable, easy to hand to the
    Observatory exporter (scripts/observatory_export.py).
  - Idempotent: keyed on the LinkedIn profile URL (falls back to name+company
    when the URL is missing). Re-running with a fresh export updates in place
    rather than duplicating.
  - Inference is deterministic and stored at import time so the visual layer
    never has to re-derive it. Everything inferred is labelled "inferred" in
    the UI — we never present a guess as fact.

Usage:
    python3 linkedin_import.py [EXPORT_PATH] [--db PATH] [--dry-run]

    EXPORT_PATH  a .zip, a folder, or a Connections.csv. If omitted, the script
                 looks for an export you dropped in the repo (the exports/ folder
                 or the repo root), then in the current folder and ~/Downloads.
    --db         output SQLite path (default: <repo>/data/linkedin.db)
    --dry-run    parse + report, write nothing.

No third-party packages — standard library only (Python 3.8+).
"""

import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from glob import glob

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "linkedin.db")

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

# ---------------------------------------------------------------------------
# Inference — job title -> (function, is_founder, seniority rank 1..8)
# Shared brain between the two skills. Kept deterministic and order-sensitive.
# ---------------------------------------------------------------------------

# rank 1 Entry · 2 IC · 3 Senior · 4 Staff/Lead · 5 Manager · 6 Director · 7 VP · 8 Executive
RANK_LABELS = {1: "Entry", 2: "Individual contributor", 3: "Senior IC",
               4: "Staff / Lead", 5: "Manager", 6: "Director",
               7: "VP", 8: "Executive"}

FOUNDER_RE = re.compile(r"\b(founder|co[-\s]?founder|founding)\b", re.I)


def infer_founder(title: str) -> bool:
    return bool(title) and bool(FOUNDER_RE.search(title))


def infer_rank(title: str) -> int:
    """First match wins; order encodes precedence (Sr. Director -> Director)."""
    t = (title or "").lower()
    if not t.strip():
        return 2
    # Executive / C-suite / owner-operator
    if re.search(r"\bchief\b", t) or re.search(r"\bc[efotmpis]o\b", t):
        return 8
    if re.search(r"\bpresident\b", t) and "vice" not in t:
        return 8
    # law / VC / equity partner (but not HR "business partner" style roles)
    if re.search(r"\bpartner\b", t) and not re.search(
            r"business partner|administrative|people partner|talent partner|hr\b", t):
        return 8
    # VP
    if re.search(r"\bvp\b|vice president|\bsvp\b|\bevp\b", t):
        return 7
    # Director / Head of
    if re.search(r"\bdirector\b|\bhead of\b|\bhead,|\bglobal head\b", t):
        return 6
    # Manager (a "Senior Manager" is manager-level, so this beats "senior")
    if re.search(r"\bmanager\b|\bmgr\b|\blead\b|\bhead\b", t):
        return 5
    # Staff / Principal
    if re.search(r"\bprincipal\b|\bstaff\b", t):
        return 4
    # Senior IC
    if re.search(r"\bsenior\b|\bsr\.?\b", t):
        return 3
    # Entry-level markers (bare "assistant" excluded — too noisy)
    if re.search(r"\bintern\b|\bjunior\b|\bjr\.?\b|\bcoordinator\b|"
                 r"\btrainee\b|\bclerk\b|\bapprentice\b|\bassociate\b|\bfellow\b|\bresident\b", t):
        return 1
    return 2


FUNC_CHECKS = [
    ("Engineering", r"engineer|developer|\bswe\b|\bsre\b|devops|programmer|software|"
                    r"infrastructure|\bplatform\b|\bcloud\b|security engineer|technical lead"),
    ("Data & ML",   r"\bdata\b|machine learning|\bml\b|\bai\b|analytics|data scien|"
                    r"\bscientist\b|research scien"),
    ("Product",     r"product manager|product owner|\bpm\b|product lead|head of product|"
                    r"product management|product,|of product"),
    ("Design",      r"\bdesign|\bux\b|\bui\b|creative director|graphic|brand design|ux research"),
    ("Sales",       r"\bsales\b|account executive|\bae\b|revenue|business development|"
                    r"\bbd\b|account manager|customer success|go.to.market|\bgtm\b|partnerships"),
    ("Marketing",   r"marketing|growth|\bbrand\b|content|communications|\bpr\b|"
                    r"social media|demand gen|editor|journalist"),
    ("Finance",     r"finance|financial|accounting|controller|fp&a|investor|venture|"
                    r"\bcapital\b|private equity|banker|\bvc\b|investment|bookkeep"),
    ("Legal",       r"legal|counsel|attorney|lawyer|\besq\b|paralegal|litigation|"
                    r"district attorney|law\b"),
    ("People & Talent", r"recruit|talent|people ops|human resources|\bhr\b|"
                        r"people team|head of people|people &"),
    ("Operations",  r"operations|\bops\b|chief of staff|program manager|project manager|"
                    r"logistics|supply chain|\bpmo\b"),
    ("Healthcare",  r"physician|surgeon|nurse|doctor|\bmd\b|clinical|medical|"
                    r"health\b|therapist|psycholog|dentist|pharmac"),
    ("Education",   r"professor|teacher|lecturer|instructor|educat|\bdean\b|"
                    r"academic|principal investigator"),
    ("Real Estate", r"real estate|realtor|broker|property"),
    ("Science & Research", r"researcher|research fellow|postdoc|scientist|laboratory|"
                           r"\br&d\b|biologist|chemist|physicist|research associate"),
    ("Skilled Trades", r"electrician|plumber|carpenter|contractor|mechanic|technician|"
                       r"welder|machinist|hvac|construction|foreman|installer|driver"),
    ("Government & Public", r"police|officer\b|firefighter|military|\barmy\b|navy|"
                           r"veteran|government|public sector|policy|city of|state of|"
                           r"county|federal|mayor|senator|diplomat"),
    ("Nonprofit & Social", r"nonprofit|non-profit|\bngo\b|foundation|charity|"
                           r"social work|volunteer|humanitarian|community organizer"),
    ("Hospitality & Retail", r"\bchef\b|server|bartender|restaurant|hospitality|"
                             r"retail|store manager|barista|hotel|culinary"),
    ("Arts & Media",  r"artist|musician|actor|writer|author|photographer|filmmaker|"
                      r"producer|\bdj\b|painter|dancer|curator"),
    ("Consulting",  r"consultant|consulting|\bcoach\b|coaching|advisory"),
    # Last resort before "Other": bare leadership/governance titles with no
    # clear domain (a lone "CEO", board seat, managing partner, owner).
    ("Leadership",  r"\bchief\b|\bc[efotmpis]o\b|\bceo\b|president|\bchair\b|chairperson|"
                    r"board member|board director|managing director|\bowner\b|proprietor|"
                    r"\badvisor\b|\bpartner\b|general manager"),
]
FUNC_RES = [(name, re.compile(pat, re.I)) for name, pat in FUNC_CHECKS]


def infer_func(title: str, is_founder: bool) -> str:
    if is_founder:
        return "Founder"
    t = title or ""
    for name, rgx in FUNC_RES:
        if rgx.search(t):
            return name
    return "Other"


# ---------------------------------------------------------------------------
# Export loading
# ---------------------------------------------------------------------------

def _zip_has_connections(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return any(os.path.basename(n) == "Connections.csv" for n in z.namelist())
    except Exception:
        return False


def _export_in_dir(d: str):
    """Best export in one folder: bare CSV, then a subfolder, then a real zip."""
    fp = os.path.join(d, "Connections.csv")
    if os.path.exists(fp):
        return fp
    for sub in sorted(glob(os.path.join(d, "*/"))):
        if os.path.exists(os.path.join(sub, "Connections.csv")):
            return sub.rstrip("/")
    zips = sorted((z for z in glob(os.path.join(d, "*.zip")) if _zip_has_connections(z)),
                  key=os.path.getmtime, reverse=True)
    return zips[0] if zips else None


def find_default_export() -> str:
    """Look where a first-time user would plausibly have dropped the export.

    Folder order is the priority: what you put in exports/ wins over stray
    archives elsewhere on the machine.
    """
    search_dirs = [
        os.path.join(REPO_ROOT, "exports"),
        REPO_ROOT,
        os.getcwd(),
        os.path.expanduser("~/Downloads"),
    ]
    for d in search_dirs:
        found = _export_in_dir(d)
        if found:
            return found
    raise SystemExit(
        "Couldn't find a LinkedIn export. Drop the .zip (or the unzipped folder,\n"
        "or just Connections.csv) into the exports/ folder and run again — or pass\n"
        "the path explicitly:  python3 scripts/linkedin_import.py <path>")


def read_connections_csv(path: str) -> str:
    """Return the raw text of Connections.csv from a zip / dir / file path."""
    # Check isdir before the .zip suffix — a folder can be named "export.zip".
    if os.path.isdir(path):
        fp = os.path.join(path, "Connections.csv")
        if not os.path.exists(fp):
            raise SystemExit(f"No Connections.csv in folder {path}")
        return open(fp, encoding="utf-8", errors="replace").read()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist()
                         if os.path.basename(n) == "Connections.csv"), None)
            if not name:
                raise SystemExit(f"No Connections.csv inside {path}")
            return z.read(name).decode("utf-8", errors="replace")
    if os.path.basename(path) == "Connections.csv" or path.lower().endswith(".csv"):
        return open(path, encoding="utf-8", errors="replace").read()
    raise SystemExit(f"Don't know how to read connections from: {path}")


def parse_connections(text: str) -> list:
    """Parse connections, locale-independently.

    LinkedIn localizes the CSV — headers and month names differ by account
    language — but the column ORDER is stable everywhere:
      First, Last, URL, Email, Company, Position, Connected On.
    So instead of matching English header text, we anchor on the URL column
    (the cell containing a linkedin.com profile link) and read the others by
    their fixed offset from it. Works for any export language.
    """
    rows = list(csv.reader(io.StringIO(text)))
    # header row = first non-preamble row with enough short, url-free cells
    header_idx = None
    for i, row in enumerate(rows):
        if (len(row) >= 6
                and not any("linkedin.com" in c.lower() for c in row)
                and all(len(c) < 45 for c in row if c)):
            header_idx = i
            break
    if header_idx is None:
        raise SystemExit(
            "Could not find the connections table in this CSV. Make sure it's the\n"
            "Connections.csv from a LinkedIn data export.")

    data = [r for r in rows[header_idx + 1:] if any(c.strip() for c in r)]
    # find the URL column from the data itself, fall back to LinkedIn's order (2)
    url_col = 2
    for r in data:
        hit = next((j for j, c in enumerate(r) if "linkedin.com" in c.lower()), None)
        if hit is not None:
            url_col = hit
            break

    def cell(r, idx):
        return r[idx].strip() if 0 <= idx < len(r) else ""

    people = []
    for r in data:
        first = cell(r, url_col - 2)
        last = cell(r, url_col - 1)
        if not first and not last:
            continue
        title = cell(r, url_col + 3)
        company = cell(r, url_col + 2)
        raw_date = cell(r, url_col + 4)
        year, month = parse_date(raw_date)
        founder = infer_founder(title)
        people.append({
            "first_name": first,
            "last_name": last,
            "full_name": (first + " " + last).strip(),
            "url": cell(r, url_col),
            "email": cell(r, url_col + 1),
            "company": company,
            "title": title,
            "func": infer_func(title, founder),
            "is_founder": 1 if founder else 0,
            "rank": infer_rank(title),
            "connected_year": year,
            "connected_month": month,
            "connected_raw": raw_date,
        })
    return people


def parse_date(raw: str):
    """Return (year, month) from LinkedIn's date cell across locales.

    Handles '01 Jul 2026', 'Jul 1, 2026', '2026-07-01', and non-English month
    names (falling back to just the year, which is all the era view needs).
    """
    if not raw:
        return None, None
    # ISO: 2026-07-01
    m = re.match(r"(\d{4})-(\d{1,2})-\d{1,2}", raw)
    if m:
        return int(m.group(1)), int(m.group(2))
    # English month name, either order: "01 Jul 2026" / "Jul 1, 2026"
    m = re.search(r"([A-Za-z]{3,})\w*\s+\d{1,2},?\s+(\d{4})", raw)  # Mon DD YYYY
    if m:
        return int(m.group(2)), MONTHS.get(m.group(1)[:3].title())
    m = re.search(r"\d{1,2}\s+([A-Za-z]{3,})\w*\s+(\d{4})", raw)    # DD Mon YYYY
    if m:
        return int(m.group(2)), MONTHS.get(m.group(1)[:3].title())
    # last resort: any 4-digit year (non-English months land here — year only)
    m = re.search(r"(19|20)\d{2}", raw)
    return (int(m.group(0)), None) if m else (None, None)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    natural_key    TEXT UNIQUE,
    first_name     TEXT,
    last_name      TEXT,
    full_name      TEXT,
    url            TEXT,
    email          TEXT,
    company        TEXT,
    title          TEXT,
    func           TEXT,
    is_founder     INTEGER DEFAULT 0,
    rank           INTEGER,
    connected_year INTEGER,
    connected_month INTEGER,
    connected_raw  TEXT,
    source         TEXT DEFAULT 'linkedin',
    first_seen_at  TEXT,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_conn_company ON connections(company);
CREATE INDEX IF NOT EXISTS idx_conn_func    ON connections(func);
CREATE INDEX IF NOT EXISTS idx_conn_year    ON connections(connected_year);

CREATE TABLE IF NOT EXISTS import_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at      TEXT,
    source_path TEXT,
    total_rows  INTEGER,
    inserted    INTEGER,
    updated     INTEGER
);
"""


def natural_key(p: dict) -> str:
    if p["url"]:
        return "url:" + p["url"].rstrip("/").lower()
    return "nc:" + (p["full_name"] + "|" + p["company"]).lower()


def write_db(people: list, db_path: str, source_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inserted = updated = 0
    for p in people:
        key = natural_key(p)
        row = conn.execute(
            "SELECT id FROM connections WHERE natural_key=?", (key,)).fetchone()
        if row:
            conn.execute("""
                UPDATE connections SET first_name=?, last_name=?, full_name=?,
                    url=?, email=?, company=?, title=?, func=?, is_founder=?,
                    rank=?, connected_year=?, connected_month=?, connected_raw=?,
                    updated_at=? WHERE id=?""",
                (p["first_name"], p["last_name"], p["full_name"], p["url"],
                 p["email"], p["company"], p["title"], p["func"], p["is_founder"],
                 p["rank"], p["connected_year"], p["connected_month"],
                 p["connected_raw"], now, row[0]))
            updated += 1
        else:
            conn.execute("""
                INSERT INTO connections (natural_key, first_name, last_name,
                    full_name, url, email, company, title, func, is_founder, rank,
                    connected_year, connected_month, connected_raw, source,
                    first_seen_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, p["first_name"], p["last_name"], p["full_name"], p["url"],
                 p["email"], p["company"], p["title"], p["func"], p["is_founder"],
                 p["rank"], p["connected_year"], p["connected_month"],
                 p["connected_raw"], "linkedin", now, now))
            inserted += 1
    conn.execute("""INSERT INTO import_runs
        (ran_at, source_path, total_rows, inserted, updated) VALUES (?,?,?,?,?)""",
        (now, source_path, len(people), inserted, updated))
    conn.commit()
    conn.close()
    return inserted, updated


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(people: list):
    from collections import Counter
    n = len(people)
    years = [p["connected_year"] for p in people if p["connected_year"]]
    span = f"{min(years)}–{max(years)}" if years else "unknown"
    companies = Counter(p["company"] for p in people if p["company"])
    funcs = Counter(p["func"] for p in people)
    other = funcs.get("Other", 0)
    no_title = sum(1 for p in people if not p["title"])
    no_company = sum(1 for p in people if not p["company"])
    founders = sum(p["is_founder"] for p in people)
    senior = sum(1 for p in people if p["rank"] >= 6)

    print(f"\n  Connections parsed : {n}")
    print(f"  Date span          : {span}")
    print(f"  Distinct companies : {len(companies)}")
    print(f"  Founders           : {founders}")
    print(f"  Director+ (rank>=6): {senior}")
    print(f"  Blank titles       : {no_title}   Blank companies: {no_company}")
    print(f"  Bucketed as 'Other': {other} ({other*100//max(n,1)}%)")
    print("\n  Top companies:")
    for c, k in companies.most_common(10):
        print(f"    {k:4}  {c}")
    print("\n  Function mix:")
    for f, k in funcs.most_common():
        print(f"    {k:4}  {f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export", nargs="?", help="zip / folder / Connections.csv")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = args.export or find_default_export()
    print(f"Reading export: {src}")
    people = parse_connections(read_connections_csv(src))
    report(people)

    if args.dry_run:
        print("\n  [dry-run] nothing written.")
        return
    ins, upd = write_db(people, args.db, src)
    print(f"\n  Wrote {args.db}")
    print(f"  Inserted {ins}, updated {upd}.")


if __name__ == "__main__":
    main()

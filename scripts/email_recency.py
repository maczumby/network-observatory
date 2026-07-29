#!/usr/bin/env python3
"""
email_recency.py — when did I last exchange email with this person?

Part of network-observatory. Trellis remembers what you tell it; this script
asks Gmail directly, holding the most limited credential Google offers. It
authenticates with the gmail.metadata OAuth scope, which means Google's own
servers will hand it message headers (From, To, Cc, Date, Subject) and will
refuse — at the server, regardless of what this code asks — to hand it any
message body or attachment.

It is the one component of this repo that holds a token. The token lives in
data/gmail_token.json on this machine, readable only by your user, and can be
revoked any time at https://myaccount.google.com/permissions

Because the metadata scope disallows Gmail search, the script pulls the header
index of your N most recent messages once (default 500) and checks every
requested contact against that single sweep. So a batch of 50 contacts costs
the same as one, and "nothing found" means "nothing in the last N messages",
not "never".

Usage:
    python3 scripts/email_recency.py "sam@example.com"
    python3 scripts/email_recency.py --batch contacts.txt --json
    python3 scripts/email_recency.py --from-db            # LinkedIn connections with emails
    python3 scripts/email_recency.py "sam@example.com" --limit 2000 --subjects
"""

import argparse
import base64
import concurrent.futures
import hashlib
import http.server
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLIENT_FILE = Path(os.environ.get("GMAIL_OAUTH_CLIENT", REPO / "data" / "gmail_oauth_client.json"))
TOKEN_FILE = REPO / "data" / "gmail_token.json"
DB_FILE = REPO / "data" / "linkedin.db"

SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"

WANTED = ("From", "To", "Cc", "Date")
DEFAULT_LIMIT = 500
WORKERS = 12

# days-since thresholds for the recency label
BUCKETS = ((14, "active"), (60, "warm"), (180, "cooling"))


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- OAuth ----

def client_config():
    if not CLIENT_FILE.exists():
        die(
            f"No OAuth client at {CLIENT_FILE}.\n"
            "Drop in the client JSON for this app (ask Mari for the Filament\n"
            "Email Recency client), or create your own: console.cloud.google.com\n"
            "> new project > enable Gmail API > OAuth client, type 'Desktop app'\n"
            "> download the JSON to that path."
        )
    raw = json.loads(CLIENT_FILE.read_text())
    cfg = raw.get("installed") or raw.get("web") or raw
    if "client_id" not in cfg:
        die(f"{CLIENT_FILE} doesn't look like a Google OAuth client JSON.")
    return cfg


class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    code = None

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CodeCatcher.code = (params.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<p>Authorized. You can close this tab and go back to the terminal.</p>")

    def log_message(self, *_):
        pass


def _post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def authorize():
    cfg = client_config()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    server = http.server.HTTPServer(("127.0.0.1", 0), _CodeCatcher)
    redirect = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.handle_request, daemon=True).start()

    url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("Opening your browser. Google will ask to share your email message")
    print("metadata (headers only). It will not, and cannot, share message bodies.")
    print(f"If no browser opens, visit:\n  {url}\n")
    webbrowser.open(url)

    deadline = time.time() + 300
    while _CodeCatcher.code is None and time.time() < deadline:
        time.sleep(0.2)
    server.server_close()
    if _CodeCatcher.code is None:
        die("Timed out waiting for the browser authorization (5 minutes).")

    tok = _post_form(TOKEN_ENDPOINT, {
        "client_id": cfg["client_id"],
        "client_secret": cfg.get("client_secret", ""),
        "code": _CodeCatcher.code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect,
    })

    granted = set(tok.get("scope", "").split())
    if granted - {SCOPE}:
        die(f"Google granted broader access than requested ({sorted(granted)}). Refusing to save it.")

    tok["expires_at"] = time.time() + int(tok.get("expires_in", 0))
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tok))
    TOKEN_FILE.chmod(0o600)
    print(f"Saved {TOKEN_FILE} (this machine only, your user only).")
    return tok


def access_token():
    if not TOKEN_FILE.exists():
        tok = authorize()
    else:
        tok = json.loads(TOKEN_FILE.read_text())
    if time.time() > tok.get("expires_at", 0) - 60:
        cfg = client_config()
        try:
            fresh = _post_form(TOKEN_ENDPOINT, {
                "client_id": cfg["client_id"],
                "client_secret": cfg.get("client_secret", ""),
                "refresh_token": tok.get("refresh_token", ""),
                "grant_type": "refresh_token",
            })
        except urllib.error.HTTPError:
            die(
                "Your Google authorization expired or was revoked. One browser\n"
                f"click to fix: delete {TOKEN_FILE} and re-run this command."
            )
        tok["access_token"] = fresh["access_token"]
        tok["expires_at"] = time.time() + int(fresh.get("expires_in", 0))
        TOKEN_FILE.write_text(json.dumps(tok))
        TOKEN_FILE.chmod(0o600)
    return tok["access_token"]


# ------------------------------------------------------------- Gmail API ----

def api_get(token, path, params=None):
    qs = "?" + urllib.parse.urlencode(params, doseq=True) if params else ""
    req = urllib.request.Request(API + path + qs, headers={"Authorization": f"Bearer {token}"})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503):
                time.sleep(1.5 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(1.5 ** attempt)
    raise last


def recent_ids(token, limit):
    ids, page = [], None
    while len(ids) < limit:
        params = {"maxResults": min(100, limit - len(ids))}
        if page:
            params["pageToken"] = page
        resp = api_get(token, "/messages", params)
        ids += [m["id"] for m in resp.get("messages", [])]
        page = resp.get("nextPageToken")
        if not page or not resp.get("messages"):
            break
    return ids[:limit]


def header_index(token, ids, with_subjects):
    """One header record per readable message, newest first by the Date header.

    Gmail lists by arrival time; the sender's Date header can disagree, and
    "who wrote last" has to follow the latter, so we sort ourselves. Fetch
    failures and unparseable dates are counted, never dropped silently — a
    partial sweep can only understate contact, and that must be visible.
    """
    fields = list(WANTED) + (["Subject"] if with_subjects else [])
    params = [("format", "metadata")] + [("metadataHeaders", f) for f in fields]

    def grab(mid):
        return api_get(token, f"/messages/{mid}", params)

    rows, failed = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for fut in concurrent.futures.as_completed({pool.submit(grab, m) for m in ids}):
            try:
                rows.append(fut.result())
            except Exception:
                failed += 1

    index, undated = [], 0
    for msg in rows:
        h = {x["name"].title(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
        try:
            when = parsedate_to_datetime(h.get("Date"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            undated += 1
            continue
        index.append({
            "when": when,
            "sender": h.get("From", "").lower(),
            "recipients": (h.get("To", "") + " " + h.get("Cc", "")).lower(),
            "subject": h.get("Subject") if with_subjects else None,
        })
    index.sort(key=lambda r: r["when"], reverse=True)
    return index, {"listed": len(ids), "fetched": len(rows), "failed": failed, "undated": undated}


# --------------------------------------------------------------- Lookup ----

def label(days):
    for cutoff, name in BUCKETS:
        if days <= cutoff:
            return name
    return "cold"


def first_address(text, needle):
    for addr in re.findall(r"[\w.+-]+@[\w.-]+", text):
        if needle in addr.lower() or needle in text:
            return addr
    return None


def check(index, scanned, me, contact, display=None):
    needle = contact.lower().strip()
    hits = [r for r in index if needle in r["sender"] or needle in r["recipients"]]
    out = {
        "contact": display or contact,
        "scanned": scanned,
        "matches": len(hits),
        "last_contact": None,
        "days_ago": None,
        "direction": None,
        "recency": "none",
        "matched_address": None,
    }
    if not hits:
        return out
    top = hits[0]
    days = (datetime.now(timezone.utc) - top["when"]).days
    out.update({
        "last_contact": top["when"].isoformat(),
        "days_ago": days,
        "direction": "sent" if me in top["sender"] else "received",
        "recency": label(days),
        "matched_address": first_address(top["sender"] + " " + top["recipients"], needle),
    })
    if top.get("subject") is not None:
        out["recent_subjects"] = [h["subject"] for h in hits[:3] if h.get("subject")]
    return out


def db_contacts(path):
    if not Path(path).exists():
        die(f"No database at {path}. Run scripts/linkedin_import.py first.")
    rows = sqlite3.connect(path).execute(
        "SELECT full_name, email FROM connections WHERE email IS NOT NULL AND TRIM(email) != ''"
    ).fetchall()
    if not rows:
        die("No connections in the database shared an email address with LinkedIn.")
    return [(name, email) for name, email in rows]


# ------------------------------------------------------------------ CLI ----

def main():
    ap = argparse.ArgumentParser(description="When did I last exchange email with this person?")
    ap.add_argument("contact", nargs="?", help="an email address, or a name fragment")
    ap.add_argument("--batch", metavar="FILE", help="file of contacts, one per line, # comments ok")
    ap.add_argument("--from-db", nargs="?", const=str(DB_FILE), metavar="DB",
                    help="check every LinkedIn connection that shared an email address")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"how many recent messages to sweep (default {DEFAULT_LIMIT})")
    ap.add_argument("--subjects", action="store_true",
                    help="include recent subject lines (the one content-carrying field; off by default)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    pairs = []  # (display, needle)
    if args.contact:
        pairs.append((None, args.contact))
    if args.batch:
        for line in Path(args.batch).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pairs.append((None, line))
    if args.from_db:
        pairs += [(f"{name} <{email}>", email) for name, email in db_contacts(args.from_db)]
    if not pairs:
        ap.error("give a contact, --batch, or --from-db")

    token = access_token()
    me = api_get(token, "/profile").get("emailAddress", "").lower()
    index, sweep = header_index(token, recent_ids(token, args.limit), args.subjects)

    results = [check(index, sweep["listed"], me, needle, display) for display, needle in pairs]
    results.sort(key=lambda r: (r["days_ago"] is None, r["days_ago"] or 0), reverse=True)

    if sweep["failed"] or sweep["undated"]:
        print(
            f"note: sweep incomplete ({sweep['failed']} unfetchable, {sweep['undated']} "
            "with unreadable dates). A partial sweep can only understate contact.",
            file=sys.stderr,
        )

    if args.json:
        print(json.dumps({"sweep": sweep, "results": results}, indent=2))
        return

    for r in results:
        if not r["matches"]:
            print(f"{r['contact']}: nothing in the last {r['scanned']} messages")
            continue
        who = f" (matched {r['matched_address']})" if r["matched_address"] else ""
        print(
            f"{r['contact']}: {r['days_ago']}d ago, {r['direction']}, {r['recency']}; "
            f"{r['matches']} of the last {r['scanned']} messages{who}"
        )
        for s in r.get("recent_subjects", []):
            print(f"    - {s}")


if __name__ == "__main__":
    main()

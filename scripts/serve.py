#!/usr/bin/env python3
"""Serve the built Observatory so it can be exposed on a public port.

The Observatory itself is self-contained HTML. To hand someone a link
(instead of the file), it has to be reachable over HTTP first. This serves
the `dashboard/` directory so an agent37 exposed port can point at it.

Protection is a real login page, not a browser popup: set a password once
and every restart stays locked, no flags to remember.

    python3 scripts/serve.py --set-password "something long"   # once
    python3 scripts/serve.py                                   # locked serving
    python3 scripts/serve.py --open                            # explicitly public
    python3 scripts/serve.py --clear-password                  # remove the lock

The password is stored only as a salted PBKDF2 hash in data/serve_auth.json
(gitignored, chmod 600) together with a random cookie-signing secret.
Visitors get a normal web form (paste and password managers work), and a
successful login sets a signed, HttpOnly session cookie for 30 days. No
credential material ever appears in served pages.

Why not "Sign in with Google": Google OAuth requires the exact redirect URL
to be registered in advance, and agent37 exposed-port URLs carry rotating
per-instance hashes. When these pages get a stable domain, add a Web OAuth
client to the operator's existing Google Cloud project and revisit.

Standard library only, to match the rest of the tool (no pip installs).
"""

import argparse
import functools
import hashlib
import hmac
import html
import http.server
import json
import os
import secrets
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DASHBOARD_DIR = os.path.join(REPO_ROOT, "dashboard")
AUTH_FILE = os.path.join(REPO_ROOT, "data", "serve_auth.json")
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "linkedin.db")
VERSION_FILE = os.path.join(REPO_ROOT, "VERSION")
DEFAULT_PORT = 8766
PBKDF2_ITERATIONS = 200_000
SESSION_SECONDS = 30 * 24 * 3600
ATTEMPT_LIMIT = 20
ATTEMPT_WINDOW = 15 * 60
API_BODY_LIMIT = 16 * 1024
WRITE_LIMIT = 120           # writes per window per client IP
WRITE_WINDOW = 15 * 60

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Network Observatory</title>
<style>
  /* Palette mirrors scripts/observatory/tokens.css (this page is generated
     server-side, so the values are inlined). */
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#0b0e18; color:#e9ecf6;
         font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  form {{ width:min(360px, calc(100vw - 48px)); padding:30px;
          background:#131728; border:1px solid rgba(166,178,212,0.14);
          border-radius:14px; }}
  h1 {{ margin:0 0 6px; font-size:20px; letter-spacing:-0.02em; }}
  p {{ margin:0 0 18px; color:#a8b0ca; font-size:14px; }}
  input[type=password] {{ width:100%; box-sizing:border-box; padding:11px 12px;
          border-radius:9px; border:1px solid rgba(166,178,212,0.30);
          background:#0b0e18; color:#e9ecf6; font-size:16px; }}
  button {{ width:100%; margin-top:14px; padding:11px; border:none;
          border-radius:9px; background:#9db2ff; color:#0a0d1a;
          font-size:15px; font-weight:700; cursor:pointer; }}
  .err {{ margin:0 0 14px; padding:9px 11px; border-radius:8px;
          background:rgba(240,144,125,0.14); color:#f0907d; font-size:14px; }}
</style></head><body>
<form method="post" action="/login">
  <h1>Network Observatory</h1>
  <p>This map is private. Enter the password you were given.</p>
  {error}
  <input type="password" name="password" autofocus required
         autocomplete="current-password" placeholder="Password">
  <input type="hidden" name="next" value="{next_path}">
  <button type="submit">Open the Observatory</button>
</form>
</body></html>"""


# ---------------------------------------------------------------- auth store

def save_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "salt": salt.hex(),
            "hash": digest.hex(),
            "iterations": PBKDF2_ITERATIONS,
            "cookie_secret": secrets.token_hex(32),
            "created_at": int(time.time()),
        }, f)
    os.chmod(AUTH_FILE, 0o600)


def load_auth():
    if not os.path.exists(AUTH_FILE):
        return None
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            auth = json.load(f)
        bytes.fromhex(auth["salt"]); bytes.fromhex(auth["hash"])
        assert auth.get("cookie_secret")
        return auth
    except Exception:
        sys.exit(f"{AUTH_FILE} is unreadable or corrupt. Re-run with --set-password.")


def check_password(auth, password):
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(auth["salt"]),
        int(auth.get("iterations", PBKDF2_ITERATIONS)))
    return hmac.compare_digest(digest.hex(), auth["hash"])


def make_session(auth):
    expiry = str(int(time.time()) + SESSION_SECONDS)
    sig = hmac.new(auth["cookie_secret"].encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def session_valid(auth, value):
    if not value or "." not in value:
        return False
    expiry, sig = value.split(".", 1)
    if not expiry.isdigit() or int(expiry) < time.time():
        return False
    want = hmac.new(auth["cookie_secret"].encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)


def safe_next(path):
    # Only same-site absolute paths; anything odd falls back to the map.
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/observatory.html"


# ------------------------------------------------------------------ write API
#
# Opt-in with --rw, and only on top of a saved password. Threat notes:
#   - SameSite=Lax is NOT enough on localhost (ports don't factor into
#     "site", so another local app could ride the cookie). Defense in depth:
#     writes must be Content-Type: application/json (an HTML form can't send
#     that cross-origin without a CORS preflight we never grant), an Origin
#     header, when present, must match the Host, and no CORS headers are
#     ever emitted, so cross-origin pages can't read responses either.
#   - A rebound DNS origin never holds the signed cookie, so --rw without a
#     password would be the actual foot-gun; that combination refuses to run.
#   - Anyone WITH the viewing password can write unless --rw-local-only.
#     serve.py says so at startup; docs/THREAT_MODEL.md carries the details.

def read_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "(unknown)"


def _trellis():
    sys.path.insert(0, HERE)
    import trellis
    return trellis


VALID_PRIORITIES = ("muted", "normal", "important", "critical")


def api_person(db_path, body):
    """One explicit user action -> one Trellis write. Returns (status, dict)."""
    trellis = _trellis()
    action = body.get("action")
    conn = trellis.connect(db_path)
    try:
        cid = None
        if isinstance(body.get("id"), int):
            row = conn.execute("SELECT * FROM connections WHERE id=?",
                               (body["id"],)).fetchone()
            if row:
                row = trellis._canonical_person(conn, row)
                cid = row["id"] if row else None
        if cid is None:
            if not any(body.get(k) for k in ("name", "email", "url")):
                return 400, {"ok": False, "error": "no person identified"}
            cid = trellis.find_or_create_person(
                conn, name=body.get("name"), email=body.get("email"),
                url=body.get("url"), company=body.get("company"))

        if action == "priority":
            value = body.get("value")
            if value not in VALID_PRIORITIES:
                return 400, {"ok": False, "error": f"priority must be one of {VALID_PRIORITIES}"}
            trellis.set_priority(conn, cid, value)
        elif action == "follow_up":
            on = body.get("on")
            if on is not None:
                try:
                    on = trellis.parse_follow_up(str(on))
                except SystemExit as e:
                    return 400, {"ok": False, "error": str(e)}
            trellis.set_follow_up(conn, cid, on, body.get("reason"))
        elif action == "note":
            note = (body.get("note") or "").strip()
            if not note:
                return 400, {"ok": False, "error": "empty note"}
            if not conn.execute("SELECT 1 FROM notes WHERE connection_id=? AND content=?",
                                (cid, note)).fetchone():
                conn.execute("""INSERT INTO notes (connection_id, content, category,
                    created_at) VALUES (?,?, 'context', ?)""",
                    (cid, note, trellis.now()))
        elif action == "flag":
            # legacy map action: raise to important, never downgrade
            m = trellis.meta_for(conn, cid)
            cur = (m["priority"] if m else None) or "normal"
            if cur in ("normal", "muted"):
                trellis.set_priority(conn, cid, "important")
        else:
            return 400, {"ok": False, "error": f"unknown action {action!r}"}
        conn.commit()
        return 200, {"ok": True, "id": cid, "action": action}
    finally:
        conn.close()


def api_merge(db_path, body):
    trellis = _trellis()
    src, into = body.get("from_id"), body.get("into_id")
    if not isinstance(src, int) or not isinstance(into, int):
        return 400, {"ok": False, "error": "from_id and into_id must be integers"}
    conn = trellis.connect(db_path)
    try:
        ns = argparse.Namespace(src=src, into=into)
        try:
            trellis.cmd_merge(conn, ns)  # journaled; reversible via unmerge
        except SystemExit as e:
            return 400, {"ok": False, "error": str(e)}
        return 200, {"ok": True, "from_id": src, "into_id": into}
    finally:
        conn.close()


# ------------------------------------------------------------------- handler

class _AuthHandler(http.server.SimpleHTTPRequestHandler):
    auth = None  # dict from load_auth(), or None for open access
    attempts = {}  # ip -> [window_start, count]
    rw = False               # write API enabled (--rw; requires a password)
    rw_local_only = False    # accept writes from loopback only (--rw-local-only)
    db_path = DEFAULT_DB
    writes = {}              # ip -> [window_start, count] for the write cap

    # -- helpers
    def _cookie_session(self):
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "obs_session":
                return value
        return None

    def _send_html(self, body, status=200, extra_headers=None):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _login_page(self, next_path, error=None, status=200):
        body = LOGIN_PAGE.format(
            error=f'<p class="err">{html.escape(error)}</p>' if error else "",
            next_path=html.escape(safe_next(next_path), quote=True),
        )
        self._send_html(body, status=status)

    def _over_attempt_limit(self):
        # Behind a tunnel every visitor shares one client IP, so this bucket
        # is effectively global. Only FAILED attempts may count toward it —
        # if successes counted, the 9th person opening a shared map would be
        # locked out with the right password.
        ip = self.client_address[0]
        now = time.time()
        window, count = self.attempts.get(ip, (now, 0))
        if now - window > ATTEMPT_WINDOW:
            self.attempts[ip] = (now, 0)
            return False
        return count >= ATTEMPT_LIMIT

    def _record_failure(self):
        ip = self.client_address[0]
        now = time.time()
        window, count = self.attempts.get(ip, (now, 0))
        if now - window > ATTEMPT_WINDOW:
            window, count = now, 0
        self.attempts[ip] = (window, count + 1)

    def _authorized(self):
        if not self.auth:
            return True
        return session_valid(self.auth, self._cookie_session())

    def _send_json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        # Deliberately no CORS headers: cross-origin pages get opaque failures.
        self.end_headers()
        self.wfile.write(raw)

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True  # non-browser client or same-origin GET-form nav
        host = self.headers.get("Host", "")
        return urllib.parse.urlparse(origin).netloc == host

    def _over_write_limit(self):
        ip = self.client_address[0]
        now = time.time()
        window, count = self.writes.get(ip, (now, 0))
        if now - window > WRITE_WINDOW:
            self.writes[ip] = (now, 1)
            return False
        self.writes[ip] = (window, count + 1)
        return count + 1 > WRITE_LIMIT

    def _handle_api_post(self, path):
        if not self.rw:
            self._send_json(404, {"ok": False, "error": "write API not enabled"})
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "not signed in"})
            return
        if self.rw_local_only and self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send_json(403, {"ok": False, "error": "writes are local-only"})
            return
        if not self._origin_ok():
            self._send_json(403, {"ok": False, "error": "cross-origin write refused"})
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send_json(415, {"ok": False, "error": "writes must be application/json"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > API_BODY_LIMIT:
            self._send_json(413, {"ok": False, "error": "body missing or too large"})
            return
        if self._over_write_limit():
            self._send_json(429, {"ok": False, "error": "too many writes; slow down"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            self._send_json(400, {"ok": False, "error": "invalid JSON object"})
            return
        try:
            if path == "/api/person":
                status, payload = api_person(self.db_path, body)
            elif path == "/api/merge":
                status, payload = api_merge(self.db_path, body)
            else:
                status, payload = 404, {"ok": False, "error": "unknown endpoint"}
        except Exception as e:  # a write must never take the server down
            status, payload = 500, {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self._send_json(status, payload)

    # -- verbs
    def do_GET(self):  # noqa: N802 (http.server naming)
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/observatory.html")
            self.end_headers()
            return
        if path == "/api/status":
            # Pages probe this to tell live / read-only apart. Auth-gated like
            # everything else so an unauthenticated probe learns nothing.
            if not self._authorized():
                self._send_json(401, {"ok": False, "error": "not signed in"})
                return
            self._send_json(200, {"ok": True, "rw": bool(self.rw),
                                  "version": read_version()})
            return
        if not self._authorized():
            self._login_page(path)
            return
        super().do_GET()

    def do_HEAD(self):  # noqa: N802
        if not self._authorized():
            self.send_response(401)
            self.end_headers()
            return
        super().do_HEAD()

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            self._handle_api_post(path)
            return
        if path != "/login" or not self.auth:
            self.send_response(404)
            self.end_headers()
            return
        if self._over_attempt_limit():
            self._send_html("Too many attempts. Wait a few minutes.", status=429)
            return
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        password = (form.get("password") or [""])[0]
        next_path = safe_next((form.get("next") or [""])[0])
        if not check_password(self.auth, password):
            self._record_failure()
            self._login_page(next_path, error="That password didn't match. Try again.")
            return
        cookie = (
            f"obs_session={make_session(self.auth)}; Path=/; HttpOnly; "
            f"SameSite=Lax; Max-Age={SESSION_SECONDS}"
        )
        self.send_response(303)
        self.send_header("Set-Cookie", cookie)
        self.send_header("Location", next_path)
        self.end_headers()

    def log_message(self, *args):  # quieter; the agent narrates instead
        pass


# ---------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="Serve the Observatory dashboard over HTTP.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to serve on (default {DEFAULT_PORT}).")
    parser.add_argument("--set-password", metavar="PASS",
                        help="Save (or replace) the password, then serve locked.")
    parser.add_argument("--password", default=os.environ.get("OBSERVATORY_PASSWORD"),
                        help="Back-compat alias for --set-password. Accepts the old "
                             '"user:pass" form; the username part is ignored now.')
    parser.add_argument("--open", action="store_true",
                        help="Serve without any password, even if one is saved.")
    parser.add_argument("--clear-password", action="store_true",
                        help="Delete the saved password and exit.")
    parser.add_argument("--dir", default=DASHBOARD_DIR,
                        help="Directory to serve (default: dashboard/).")
    parser.add_argument("--rw", action="store_true",
                        help="Enable the write API (/api/person, /api/merge) so the "
                             "screens can mark priority / set follow-ups / confirm "
                             "merges. Requires a saved password.")
    parser.add_argument("--rw-local-only", dest="rw_local_only", action="store_true",
                        help="With --rw: accept writes from this machine only, so "
                             "people you share the viewing password with can look "
                             "but not change anything.")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="Trellis DB the write API writes to (default: data/linkedin.db).")
    args = parser.parse_args()

    if args.clear_password:
        if os.path.exists(AUTH_FILE):
            os.remove(AUTH_FILE)
            print("Password removed. Serving is OPEN until you --set-password again.")
        else:
            print("No saved password to remove.")
        return

    new_password = args.set_password or args.password
    if new_password:
        if ":" in new_password and args.password and not args.set_password:
            new_password = new_password.split(":", 1)[1]  # old user:pass form
        save_password(new_password)
        print(f"Password saved (hashed) to {AUTH_FILE}. Restarts stay locked automatically.")

    if not os.path.isdir(args.dir):
        sys.exit(f"Nothing to serve: {args.dir} doesn't exist. Build the pages first "
                 f"(python3 scripts/observatory_export.py).")
    pages = [p for p in ("observatory.html", "warmth.html", "workbench.html")
             if os.path.exists(os.path.join(args.dir, p))]
    if not pages:
        sys.exit(f"No built pages in {args.dir}. Build them first: "
                 f"python3 scripts/observatory_export.py (then warmth_export.py, "
                 f"workbench_export.py).")

    _AuthHandler.auth = None if args.open else load_auth()
    if args.rw and not _AuthHandler.auth:
        sys.exit("--rw needs a saved password (run --set-password first). "
                 "An open server with a write API would let any visitor edit "
                 "your relationship memory.")
    _AuthHandler.rw = args.rw
    _AuthHandler.rw_local_only = args.rw_local_only
    _AuthHandler.db_path = args.db
    handler = functools.partial(_AuthHandler, directory=args.dir)

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    server = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), handler)

    if _AuthHandler.auth:
        lock = "locked — visitors get a login page"
    elif args.open:
        lock = "OPEN by --open flag: anyone with the link can see everything"
    else:
        lock = "open (no password saved). Run with --set-password to lock it"
    print(f"Serving {args.dir} on port {args.port} — {lock}.")
    print("Built pages: " + ", ".join(pages)
          + ("" if len(pages) == 3 else "  (others not built yet)"))
    if args.rw:
        scope = "from this machine only" if args.rw_local_only else \
            "for ANYONE with the viewing password"
        print(f"Write API ON — priority/follow-up/merge changes accepted {scope}.")
    print(f"Local check: http://127.0.0.1:{args.port}/{pages[0]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()

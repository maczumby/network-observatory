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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_DIR = os.path.join(REPO_ROOT, "dashboard")
AUTH_FILE = os.path.join(REPO_ROOT, "data", "serve_auth.json")
DEFAULT_PORT = 8766
PBKDF2_ITERATIONS = 200_000
SESSION_SECONDS = 30 * 24 * 3600
ATTEMPT_LIMIT = 8
ATTEMPT_WINDOW = 15 * 60

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Network Observatory</title>
<style>
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#14160f; color:#fcf6dc;
         font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  form {{ width:min(360px, calc(100vw - 48px)); padding:30px;
          background:#1e2119; border:1px solid rgba(252,246,220,0.12);
          border-radius:14px; }}
  h1 {{ margin:0 0 6px; font-size:20px; letter-spacing:-0.02em; }}
  p {{ margin:0 0 18px; color:rgba(252,246,220,0.62); font-size:14px; }}
  input[type=password] {{ width:100%; box-sizing:border-box; padding:11px 12px;
          border-radius:9px; border:1px solid rgba(252,246,220,0.2);
          background:#14160f; color:#fcf6dc; font-size:16px; }}
  button {{ width:100%; margin-top:14px; padding:11px; border:none;
          border-radius:9px; background:#f19779; color:#14160f;
          font-size:15px; font-weight:700; cursor:pointer; }}
  .err {{ margin:0 0 14px; padding:9px 11px; border-radius:8px;
          background:rgba(241,151,121,0.14); color:#f19779; font-size:14px; }}
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


# ------------------------------------------------------------------- handler

class _AuthHandler(http.server.SimpleHTTPRequestHandler):
    auth = None  # dict from load_auth(), or None for open access
    attempts = {}  # ip -> [window_start, count]

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

    def _rate_limited(self):
        ip = self.client_address[0]
        now = time.time()
        window, count = self.attempts.get(ip, (now, 0))
        if now - window > ATTEMPT_WINDOW:
            window, count = now, 0
        count += 1
        self.attempts[ip] = (window, count)
        return count > ATTEMPT_LIMIT

    def _authorized(self):
        if not self.auth:
            return True
        return session_valid(self.auth, self._cookie_session())

    # -- verbs
    def do_GET(self):  # noqa: N802 (http.server naming)
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/observatory.html")
            self.end_headers()
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
        if path != "/login" or not self.auth:
            self.send_response(404)
            self.end_headers()
            return
        if self._rate_limited():
            self._send_html("Too many attempts. Wait a few minutes.", status=429)
            return
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        password = (form.get("password") or [""])[0]
        next_path = safe_next((form.get("next") or [""])[0])
        if not check_password(self.auth, password):
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
        sys.exit(f"Nothing to serve: {args.dir} doesn't exist. Build the map first "
                 f"(python3 scripts/observatory_export.py).")
    if not os.path.exists(os.path.join(args.dir, "observatory.html")):
        sys.exit(f"No observatory.html in {args.dir}. Build the map first "
                 f"(python3 scripts/observatory_export.py).")

    _AuthHandler.auth = None if args.open else load_auth()
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
    print(f"Local check: http://127.0.0.1:{args.port}/observatory.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()

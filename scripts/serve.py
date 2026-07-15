#!/usr/bin/env python3
"""Serve the built Observatory so it can be exposed on a public port.

The Observatory itself is a single self-contained HTML file. To hand someone a
link (instead of the file), it has to be reachable over HTTP first. This serves
the `dashboard/` directory so an agent37 exposed port can point at it.

Two modes:

    python3 scripts/serve.py                     # open: anyone with the link opens it
    python3 scripts/serve.py --password me:secret # locked: the link prompts for a password

By default the exposed port is public and unauthenticated (agent37 exposes it
"anyone with the URL reaches it"), so `--password` is the real, optional lock:
it adds HTTP Basic Auth, and the public URL then prompts before it shows the map.

Standard library only, to match the rest of the tool (no pip installs).
"""

import argparse
import base64
import functools
import http.server
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_DIR = os.path.join(REPO_ROOT, "dashboard")
DEFAULT_PORT = 8766


class _AuthHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that requires HTTP Basic Auth when a credential
    is configured. Set the expected value on the class before serving."""

    expected_auth = None  # "Basic <base64(user:pass)>" or None for open access

    def _challenge(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Network Observatory"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authentication required.\n")

    def _authorized(self) -> bool:
        if not self.expected_auth:
            return True
        return self.headers.get("Authorization") == self.expected_auth

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if not self._authorized():
            self._challenge()
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._authorized():
            self._challenge()
            return
        super().do_HEAD()

    def log_message(self, *args) -> None:  # quieter; the agent narrates instead
        pass


def _expected_auth(password_arg):
    """Turn a --password value into the exact Authorization header to match.

    Returns the expected "Basic <token>" header string, or None for open access.

    Accepts "user:pass" or a bare "pass" (username defaults to "observatory").
    """
    if not password_arg:
        return None
    if ":" in password_arg:
        creds = password_arg
    else:
        creds = f"observatory:{password_arg}"
    token = base64.b64encode(creds.encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Observatory dashboard over HTTP.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to serve on (default {DEFAULT_PORT}).")
    parser.add_argument(
        "--password",
        default=os.environ.get("OBSERVATORY_PASSWORD"),
        help='Lock the link behind HTTP Basic Auth. "user:pass" or a bare "pass" '
        '(username defaults to "observatory"). Can also be set via OBSERVATORY_PASSWORD.',
    )
    parser.add_argument("--dir", default=DASHBOARD_DIR, help="Directory to serve (default: dashboard/).")
    args = parser.parse_args()

    if not os.path.isdir(args.dir):
        sys.exit(f"Nothing to serve: {args.dir} doesn't exist. Build the map first "
                 f"(python3 scripts/observatory_export.py).")
    if not os.path.exists(os.path.join(args.dir, "observatory.html")):
        sys.exit(f"No observatory.html in {args.dir}. Build the map first "
                 f"(python3 scripts/observatory_export.py).")

    _AuthHandler.expected_auth = _expected_auth(args.password)
    handler = functools.partial(_AuthHandler, directory=args.dir)

    # allow_reuse_address avoids "address already in use" when re-serving
    # (e.g. after toggling a password) on the same port.
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    server = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), handler)

    lock = "password-locked" if _AuthHandler.expected_auth else "open (no password)"
    print(f"Serving {args.dir} on port {args.port} — {lock}.")
    print(f"Local check: http://127.0.0.1:{args.port}/observatory.html")
    if _AuthHandler.expected_auth:
        print("The public link will prompt for the username and password before showing the map.")
    else:
        print("Anyone with the public link can open the map. Add --password to lock it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()

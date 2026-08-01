"""Tests for serve.py's login-page auth: the lock must be real, survive
restarts with no flags, and never leak the protected content pre-login."""

import http.client
import importlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
from functools import partial
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

MAP_MARKER = "WINDOW-OBS-DATA-SENTINEL"
TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "observatory", "warmth_template.html")


class WarmthTemplateTest(unittest.TestCase):
    def test_rows_are_deep_linkable(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            tpl = f.read()
        self.assertIn('id="p\' + p.id', tpl)      # per-person anchors
        self.assertIn("hashchange", tpl)          # #p<id> handler wired
        self.assertIn("scrollIntoView", tpl)


class ServeAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.dashboard = os.path.join(cls.tmp, "dashboard")
        os.makedirs(cls.dashboard)
        for name in ("observatory.html", "warmth.html"):
            with open(os.path.join(cls.dashboard, name), "w") as f:
                f.write(f"<html><body>{MAP_MARKER} {name}</body></html>")

        import serve
        importlib.reload(serve)
        serve.AUTH_FILE = os.path.join(cls.tmp, "serve_auth.json")
        cls.serve = serve
        serve.save_password("correct horse")

        serve._AuthHandler.auth = serve.load_auth()
        handler = partial(serve._AuthHandler, directory=cls.dashboard)
        ThreadingHTTPServer.allow_reuse_address = True
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read().decode()
        resp.body_text = data
        conn.close()
        return resp

    def login(self, password):
        body = urllib.parse.urlencode({"password": password, "next": "/observatory.html"})
        return self.request("POST", "/login", body=body,
                            headers={"Content-Type": "application/x-www-form-urlencoded"})

    def setUp(self):
        self.serve._AuthHandler.attempts = {}

    def test_unauthenticated_get_shows_login_not_content(self):
        resp = self.request("GET", "/observatory.html")
        self.assertEqual(resp.status, 200)
        self.assertIn("This map is private", resp.body_text)
        self.assertNotIn(MAP_MARKER, resp.body_text)

    def test_warmth_page_is_equally_gated(self):
        resp = self.request("GET", "/warmth.html")
        self.assertNotIn(MAP_MARKER, resp.body_text)

    def test_root_redirects_to_map(self):
        resp = self.request("GET", "/")
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.getheader("Location"), "/observatory.html")

    def test_wrong_password_reprompts(self):
        resp = self.login("wrong")
        self.assertEqual(resp.status, 200)
        self.assertIn("match. Try again", resp.body_text)
        self.assertIsNone(resp.getheader("Set-Cookie"))

    def test_right_password_sets_cookie_and_content_flows(self):
        resp = self.login("correct horse")
        self.assertEqual(resp.status, 303)
        cookie = resp.getheader("Set-Cookie")
        self.assertIn("obs_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        session = cookie.split(";")[0]
        page = self.request("GET", "/observatory.html", headers={"Cookie": session})
        self.assertEqual(page.status, 200)
        self.assertIn(MAP_MARKER, page.body_text)

    def test_tampered_cookie_is_rejected(self):
        resp = self.login("correct horse")
        session = resp.getheader("Set-Cookie").split(";")[0]
        name, value = session.split("=", 1)
        flipped = value[:-1] + ("0" if value[-1] != "0" else "1")
        page = self.request("GET", "/observatory.html",
                            headers={"Cookie": f"{name}={flipped}"})
        self.assertNotIn(MAP_MARKER, page.body_text)
        self.assertIn("This map is private", page.body_text)

    def test_next_path_cannot_redirect_offsite(self):
        body = urllib.parse.urlencode({"password": "correct horse",
                                       "next": "//evil.example/phish"})
        resp = self.request("POST", "/login", body=body,
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(resp.getheader("Location"), "/observatory.html")

    def test_rate_limit_trips(self):
        last = None
        for _ in range(self.serve.ATTEMPT_LIMIT + 1):
            last = self.login("wrong")
        self.assertEqual(last.status, 429)

    def test_auth_survives_reload(self):
        # A "restart" is a fresh load_auth() from disk; no flags involved.
        auth = self.serve.load_auth()
        self.assertTrue(self.serve.check_password(auth, "correct horse"))
        self.assertFalse(self.serve.check_password(auth, "something else"))


if __name__ == "__main__":
    unittest.main()

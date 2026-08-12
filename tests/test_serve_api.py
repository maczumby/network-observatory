"""The write API is the one place a web request can change your relationship
memory. These tests hold its gates shut: off by default, signed-in only,
same-origin JSON only, capped, and reversible."""

import http.client
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
from functools import partial
from http.server import ThreadingHTTPServer

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import trellis  # noqa: E402

PASSWORD = "correct horse battery staple"


class _ServerCase(unittest.TestCase):
    rw = True
    rw_local_only = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.dashboard = os.path.join(cls.tmp, "dashboard")
        os.makedirs(cls.dashboard)
        for name in ("observatory.html", "warmth.html", "workbench.html"):
            with open(os.path.join(cls.dashboard, name), "w") as f:
                f.write(f"<html><body>{name}</body></html>")

        cls.db_path = os.path.join(cls.tmp, "data", "linkedin.db")
        conn = trellis.connect(cls.db_path)
        cls.ada = trellis.find_or_create_person(
            conn, name="Ada Lovelace", email="ada@x.test", origin="linkedin")
        cls.dup = trellis.find_or_create_person(
            conn, name="ada", email="ada.l@x.test", origin="gmail")
        conn.commit()
        conn.close()

        import serve
        importlib.reload(serve)
        serve.AUTH_FILE = os.path.join(cls.tmp, "serve_auth.json")
        cls.serve = serve
        serve.save_password(PASSWORD)
        serve._AuthHandler.auth = serve.load_auth()
        serve._AuthHandler.rw = cls.rw
        serve._AuthHandler.rw_local_only = cls.rw_local_only
        serve._AuthHandler.db_path = cls.db_path

        handler = partial(serve._AuthHandler, directory=cls.dashboard)
        ThreadingHTTPServer.allow_reuse_address = True
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp)
        cls.serve._AuthHandler.rw = False
        cls.serve._AuthHandler.rw_local_only = False

    def setUp(self):
        self.serve._AuthHandler.attempts = {}
        self.serve._AuthHandler.writes = {}
        self.cookie = self._login()

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        resp.body_text = resp.read().decode()
        conn.close()
        return resp

    def _login(self):
        body = urllib.parse.urlencode({"password": PASSWORD, "next": "/"})
        resp = self._request("POST", "/login", body=body,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
        return resp.getheader("Set-Cookie", "").split(";")[0]

    def api(self, path, payload, cookie=True, ctype="application/json",
            origin=None, raw_body=None):
        headers = {"Content-Type": ctype}
        if cookie:
            headers["Cookie"] = self.cookie
        if origin:
            headers["Origin"] = origin
        body = raw_body if raw_body is not None else json.dumps(payload)
        resp = self._request("POST", path, body=body, headers=headers)
        try:
            resp.json = json.loads(resp.body_text)
        except ValueError:
            resp.json = {}
        return resp

    def meta(self, cid):
        conn = trellis.connect(self.db_path)
        try:
            row = trellis.meta_for(conn, cid)
            return dict(row) if row else {}
        finally:
            conn.close()


class WriteApiTest(_ServerCase):
    def test_status_reports_read_write_mode(self):
        resp = self._request("GET", "/api/status", headers={"Cookie": self.cookie})
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body_text)
        self.assertTrue(body["rw"])
        self.assertIn("version", body)

    def test_status_requires_a_session(self):
        resp = self._request("GET", "/api/status")
        self.assertEqual(resp.status, 401)

    def test_writes_require_a_session(self):
        before = self.meta(self.ada).get("priority")
        resp = self.api("/api/person",
                        {"action": "priority", "id": self.ada, "value": "critical"},
                        cookie=False)
        self.assertEqual(resp.status, 401)
        self.assertEqual(self.meta(self.ada).get("priority"), before,
                         "an unauthenticated request changed the database")

    def test_priority_round_trips_to_the_database(self):
        resp = self.api("/api/person",
                        {"action": "priority", "id": self.ada, "value": "important"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.meta(self.ada)["priority"], "important")
        # and it can be taken back — nothing here is one-way
        self.api("/api/person", {"action": "priority", "id": self.ada, "value": "normal"})
        self.assertEqual(self.meta(self.ada)["priority"], "normal")

    def test_follow_up_accepts_natural_language_and_clears(self):
        resp = self.api("/api/person", {"action": "follow_up", "id": self.ada,
                                        "on": "in 1 week", "reason": "after the demo"})
        self.assertEqual(resp.status, 200)
        row = self.meta(self.ada)
        self.assertRegex(row["follow_up_on"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(row["follow_up_reason"], "after the demo")
        self.api("/api/person", {"action": "follow_up", "id": self.ada, "on": None})
        self.assertIsNone(self.meta(self.ada)["follow_up_on"])

    def test_absurd_follow_up_is_rejected_not_crashed(self):
        resp = self.api("/api/person", {"action": "follow_up", "id": self.ada,
                                        "on": "in 999999999 months"})
        self.assertEqual(resp.status, 400)
        self.assertFalse(resp.json["ok"])

    def test_unknown_action_and_bad_priority_are_refused(self):
        self.assertEqual(self.api("/api/person",
                                  {"action": "teleport", "id": self.ada}).status, 400)
        self.assertEqual(self.api("/api/person", {"action": "priority", "id": self.ada,
                                                  "value": "vip"}).status, 400)

    def test_note_is_stored_once(self):
        for _ in range(2):
            self.api("/api/person", {"action": "note", "id": self.ada,
                                     "note": "met at the museum"})
        conn = trellis.connect(self.db_path)
        n = conn.execute("SELECT COUNT(*) FROM notes WHERE connection_id=?",
                         (self.ada,)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_person_can_be_identified_by_content_key_alone(self):
        """The map knows a person by name/url, not always by DB id."""
        resp = self.api("/api/person", {"action": "flag", "name": "Someone New",
                                        "email": "new@x.test"})
        self.assertEqual(resp.status, 200)
        conn = trellis.connect(self.db_path)
        row = conn.execute("""SELECT m.priority FROM person_meta m
            JOIN connections c ON c.id=m.connection_id
            WHERE c.full_name='Someone New'""").fetchone()
        conn.close()
        self.assertEqual(row["priority"], "important")

    def test_merge_is_journaled_and_reversible(self):
        resp = self.api("/api/merge", {"from_id": self.dup, "into_id": self.ada})
        self.assertEqual(resp.status, 200, resp.body_text)
        conn = trellis.connect(self.db_path)
        try:
            merge = conn.execute("SELECT * FROM identity_merges ORDER BY id DESC").fetchone()
            self.assertEqual(merge["from_connection_id"], self.dup)
            self.assertIsNone(merge["undone_at"])
            import argparse
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                trellis.cmd_unmerge(conn, argparse.Namespace(merge_id=merge["id"]))
            restored = conn.execute("SELECT source FROM connections WHERE id=?",
                                    (self.dup,)).fetchone()
            self.assertEqual(restored["source"], "gmail")
        finally:
            conn.close()

    def test_merge_refuses_non_integer_ids(self):
        self.assertEqual(self.api("/api/merge",
                                  {"from_id": "1", "into_id": 2}).status, 400)

    # --- the gates ---------------------------------------------------------
    def test_cross_origin_write_is_refused(self):
        resp = self.api("/api/person",
                        {"action": "priority", "id": self.ada, "value": "critical"},
                        origin="http://localhost:3000")
        self.assertEqual(resp.status, 403)
        self.assertNotEqual(self.meta(self.ada).get("priority"), "critical")

    def test_same_origin_header_is_accepted(self):
        resp = self.api("/api/person",
                        {"action": "priority", "id": self.ada, "value": "normal"},
                        origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(resp.status, 200)

    def test_form_content_type_is_refused(self):
        resp = self.api("/api/person", None,
                        ctype="application/x-www-form-urlencoded",
                        raw_body="action=priority&id=1&value=critical")
        self.assertEqual(resp.status, 415)

    def test_oversized_body_is_refused(self):
        resp = self.api("/api/person", None,
                        raw_body=json.dumps({"action": "note", "id": self.ada,
                                             "note": "x" * 20000}))
        self.assertEqual(resp.status, 413)

    def test_malformed_json_is_refused(self):
        self.assertEqual(self.api("/api/person", None, raw_body="{nope").status, 400)
        self.assertEqual(self.api("/api/person", None, raw_body='["a list"]').status, 400)

    def test_unknown_endpoint_is_not_found(self):
        self.assertEqual(self.api("/api/wipe", {"everything": True}).status, 404)

    def test_write_rate_is_capped(self):
        limit = self.serve.WRITE_LIMIT
        last = None
        for _ in range(limit + 2):
            last = self.api("/api/person", {"action": "priority", "id": self.ada,
                                            "value": "normal"})
        self.assertEqual(last.status, 429)

    def test_no_cors_headers_are_ever_emitted(self):
        resp = self.api("/api/person", {"action": "priority", "id": self.ada,
                                        "value": "normal"})
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))


class ReadOnlyServerTest(_ServerCase):
    """Default posture: the API simply isn't there."""
    rw = False

    def test_status_reports_read_only(self):
        resp = self._request("GET", "/api/status", headers={"Cookie": self.cookie})
        self.assertEqual(resp.status, 200)
        self.assertFalse(json.loads(resp.body_text)["rw"])

    def test_writes_are_not_found(self):
        resp = self.api("/api/person",
                        {"action": "priority", "id": self.ada, "value": "important"})
        self.assertEqual(resp.status, 404)
        self.assertEqual(self.meta(self.ada), {})

    def test_login_still_works(self):
        self.assertTrue(self.cookie.startswith("obs_session="))


class LocalOnlyWriteTest(_ServerCase):
    """--rw-local-only: shared viewing password, local-only editing."""
    rw_local_only = True

    def test_loopback_client_may_still_write(self):
        resp = self.api("/api/person",
                        {"action": "priority", "id": self.ada, "value": "important"})
        self.assertEqual(resp.status, 200)

    def test_remote_client_is_refused(self):
        real = self.serve._AuthHandler.client_address if hasattr(
            self.serve._AuthHandler, "client_address") else None
        original = self.serve._AuthHandler._handle_api_post

        def pretend_remote(handler, path):
            handler.client_address = ("203.0.113.7", 54321)
            return original(handler, path)

        self.serve._AuthHandler._handle_api_post = pretend_remote
        try:
            resp = self.api("/api/person",
                            {"action": "priority", "id": self.ada, "value": "muted"})
            self.assertEqual(resp.status, 403)
        finally:
            self.serve._AuthHandler._handle_api_post = original
        self.assertNotEqual(self.meta(self.ada).get("priority"), "muted")
        del real


if __name__ == "__main__":
    unittest.main()

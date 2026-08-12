"""The three screens as one product: shared assets inline, the nav links all
three, counts don't fan out, and every generated page stays offline-capable."""

import json
import os
import re
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
OBSERVATORY = os.path.join(SCRIPTS, "observatory")
sys.path.insert(0, SCRIPTS)

import trellis  # noqa: E402
import observatory_export  # noqa: E402
import warmth_export  # noqa: E402
import workbench_export  # noqa: E402
from observatory import common  # noqa: E402


class SharedHelpersTest(unittest.TestCase):
    def test_esc_escapes_every_html_metacharacter(self):
        self.assertEqual(common.esc('<a href="x">&\'</a>'),
                         "&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;")
        self.assertEqual(common.esc(None), "")

    def test_json_for_script_is_safe_to_embed(self):
        blob = common.json_for_script({"x": "</script><b>", "ls": "a b"})
        self.assertNotIn("</script>", blob)
        self.assertIn("<\\/script>", blob)
        self.assertNotIn(" ", blob)
        self.assertEqual(json.loads(blob.replace("<\\/", "</"))["x"], "</script><b>")

    def test_person_key_matches_the_template_expression(self):
        """The map keys localStorage notes/flags by this string. If the Python
        and JS derivations drift, every saved note orphans — so assert both
        the values AND that the template still computes it the same way."""
        self.assertEqual(
            common.person_key("https://www.linkedin.com/in/Ada/", "Ada", "Acme"),
            "u:https://www.linkedin.com/in/ada")
        self.assertEqual(common.person_key("", "Ada Lovelace", "Acme"),
                         "n:ada lovelace|acme")
        self.assertEqual(common.person_key("", "Ada", ""),
                         "n:ada|independent / unknown")
        with open(os.path.join(OBSERVATORY, "template.html"), encoding="utf-8") as f:
            tpl = f.read()
        self.assertIn("('u:'+d.url.trim().toLowerCase().replace(/\\/+$/,''))", tpl)
        self.assertIn("('n:'+(d.name||'').toLowerCase()+'|'+company.toLowerCase())", tpl)
        self.assertIn("'Independent / unknown'", tpl)

    def test_nav_links_all_three_screens_and_marks_the_active_one(self):
        html = common.nav_html("warmth")
        for href in ("observatory.html", "warmth.html", "workbench.html"):
            self.assertIn(f'href="{href}"', html)
        self.assertIn('href="warmth.html" aria-current="page"', html)
        self.assertIn("obs-nav--overlay", common.nav_html("map", variant="overlay"))

    def test_inline_assets_refuses_a_template_missing_a_marker(self):
        with self.assertRaises(SystemExit):
            common.inline_assets("<html><style></style></html>", "map")

    def test_status_of_reads_priority_and_due_date(self):
        self.assertEqual(common.status_of({"priority": "important"}),
                         {"star": True, "due": False})
        self.assertTrue(common.status_of({"follow_up_on": "2000-01-01"})["due"])
        self.assertFalse(common.status_of({"follow_up_on": "2999-01-01"})["due"])


class ExportedPagesTest(unittest.TestCase):
    """Build all three from one DB and check them as a set."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "data", "linkedin.db")
        conn = trellis.connect(cls.db_path)

        cls.ada = trellis.find_or_create_person(
            conn, name="Ada Lovelace", url="https://www.linkedin.com/in/ada",
            company="Analytical Engines", title="Founder", origin="linkedin")
        cls.grace = trellis.find_or_create_person(
            conn, name="Grace Hopper", email="grace@navy.test",
            company="Navy", origin="linkedin")
        cls.brock = trellis.find_or_create_person(
            conn, name="Brock Kelly", url="https://www.linkedin.com/in/brock",
            company="Corp", origin="linkedin")
        # the same human, minted from an email sweep — the reconciliation case
        cls.stray = trellis.find_or_create_person(
            conn, name="brock", email="brock@corp.test", origin="gmail")

        # Ada: 3 interactions x 2 open loops x 2 plans — the fanout trap.
        for i in range(3):
            conn.execute("""INSERT INTO interactions
                (connection_id, kind, occurred_on, summary, source, source_ref,
                 created_at, direction) VALUES (?,?,?,?,?,?,?,?)""",
                (cls.ada, "email", f"2026-0{i + 4}-01", "email sent", "gmail",
                 f"ada:{i}", trellis.now(), "sent"))
        for i in range(2):
            conn.execute("""INSERT INTO open_loops
                (connection_id, description, status, source, created_at)
                VALUES (?,?, 'open', 'manual', ?)""",
                (cls.ada, f"owe her thing {i}", trellis.now()))
            conn.execute("""INSERT INTO calendar_plans
                (connection_id, planned_on, source, source_ref, created_at)
                VALUES (?,?,?,?,?)""",
                (cls.ada, "2099-01-0%d" % (i + 1), "calendar", f"plan:{i}",
                 trellis.now()))
        conn.execute("""INSERT INTO interactions
            (connection_id, kind, occurred_on, summary, source, source_ref,
             created_at, direction) VALUES (?,?,?,?,?,?,?,?)""",
            (cls.stray, "email", "2026-06-01", "email received", "gmail",
             "brock:1", trellis.now(), "received"))
        trellis.set_priority(conn, cls.grace, "important")
        trellis.set_follow_up(conn, cls.grace, "2020-01-01", "long overdue")
        conn.commit()
        conn.close()

        cls.out = os.path.join(cls.tmp.name, "dashboard")
        observatory_export.render(
            observatory_export.build_payload(observatory_export.load_people(cls.db_path)),
            os.path.join(cls.out, "observatory.html"))
        warmth_export.render(warmth_export.build_payload(cls.db_path),
                             os.path.join(cls.out, "warmth.html"))
        cls.workbench_payload = workbench_export.build_payload(cls.db_path)
        workbench_export.render(cls.workbench_payload,
                                os.path.join(cls.out, "workbench.html"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def page(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return f.read()

    def test_every_page_is_self_contained(self):
        for name in ("observatory.html", "warmth.html", "workbench.html"):
            html = self.page(name)
            with self.subTest(page=name):
                self.assertEqual(html.count("@font-face"), 8, "embedded fonts missing")
                self.assertNotIn("/*__OBS_FONTS__*/", html)
                self.assertNotIn("/*__OBS_TOKENS__*/", html)
                self.assertNotIn("<!--__OBS_NAV__-->", html)
                # No request may leave the page: no external src/href, no CDN.
                externals = re.findall(r'(?:src|href)\s*=\s*"(https?:)?//[^"]+', html)
                self.assertEqual(
                    [e for e in externals if "linkedin.com/in/" not in e], [],
                    "page would make an external request")

    def test_every_page_carries_the_shared_nav_and_tokens(self):
        for name, active in (("observatory.html", "observatory.html"),
                             ("warmth.html", "warmth.html"),
                             ("workbench.html", "workbench.html")):
            html = self.page(name)
            with self.subTest(page=name):
                self.assertIn("data-obs-nav", html)
                self.assertIn("--obs-accent:", html)
                self.assertIn(f'href="{active}" aria-current="page"', html)
                for href in ("observatory.html", "warmth.html", "workbench.html"):
                    self.assertIn(f'href="{href}"', html)

    def test_workbench_counts_do_not_fan_out(self):
        ada = [p for p in self.workbench_payload["people"] if p["id"] == self.ada][0]
        self.assertEqual((ada["n"], ada["loops"], ada["plans"]), (3, 2, 2),
                         "counts multiplied — the joins fanned out")

    def test_workbench_shows_status(self):
        grace = [p for p in self.workbench_payload["people"] if p["id"] == self.grace][0]
        self.assertEqual(grace["flag"], 1)
        self.assertEqual(grace["due"], 1)

    def test_workbench_offers_no_proposals_until_reconcile_has_run(self):
        """Matching is a whole-graph pass; a page build must not pay for it.
        With no queue file yet, the screen simply has nothing to offer."""
        self.assertEqual(self.workbench_payload["candidates"], {})
        self.assertFalse(self.workbench_payload["candidates_generated"])

    def test_workbench_reads_the_queue_reconcile_writes(self):
        import reconcile
        conn = trellis.connect(self.db_path)
        try:
            reconcile.write_queue(os.path.dirname(self.db_path),
                                  "linkedin_identity_review_queue.json",
                                  reconcile.identity_queue(conn))
        finally:
            conn.close()
        payload = workbench_export.build_payload(self.db_path)
        proposals = payload["candidates"]
        self.assertIn(str(self.stray), proposals, "email-only contact got no proposal")
        self.assertIn("--from", proposals[str(self.stray)][0]["merge_command"])
        self.assertTrue(payload["candidates_generated"])
        os.remove(os.path.join(os.path.dirname(self.db_path),
                               "linkedin_identity_review_queue.json"))

    def test_a_corrupt_queue_file_does_not_break_the_build(self):
        path = os.path.join(os.path.dirname(self.db_path),
                            "linkedin_identity_review_queue.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        try:
            payload = workbench_export.build_payload(self.db_path)
            self.assertEqual(payload["candidates"], {})
        finally:
            os.remove(path)

    def test_map_payload_carries_both_identities(self):
        people = observatory_export.load_people(self.db_path)
        ada = [p for p in people if p["name"] == "Ada Lovelace"][0]
        self.assertEqual(ada["id"], self.ada)
        self.assertEqual(ada["key"], "u:https://www.linkedin.com/in/ada")

    def test_muted_people_are_absent_from_every_screen(self):
        conn = trellis.connect(self.db_path)
        trellis.set_priority(conn, self.grace, "muted")
        conn.commit()
        conn.close()
        try:
            self.assertNotIn(
                self.grace, [p["id"] for p in
                             workbench_export.build_payload(self.db_path)["people"]])
            self.assertNotIn(
                self.grace, [p["id"] for p in
                             warmth_export.build_payload(self.db_path)["people"]])
            self.assertNotIn(
                "Grace Hopper",
                [p["name"] for p in observatory_export.load_people(self.db_path)])
        finally:
            conn = trellis.connect(self.db_path)
            trellis.set_priority(conn, self.grace, "important")
            conn.commit()
            conn.close()

    def test_warmth_reports_hidden_muted_contacts_honestly(self):
        conn = trellis.connect(self.db_path)
        trellis.set_priority(conn, self.grace, "muted")
        conn.commit()
        conn.close()
        try:
            payload = warmth_export.build_payload(self.db_path)
            self.assertEqual(payload["coverage"]["muted_hidden"], 1)
        finally:
            conn = trellis.connect(self.db_path)
            trellis.set_priority(conn, self.grace, "important")
            conn.commit()
            conn.close()


if __name__ == "__main__":
    unittest.main()

"""Regressions for defects found in review. Each one either destroyed data a
user had set, or made the tool lie about someone — so each gets a test."""

import argparse
import json
import os
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import trellis  # noqa: E402
import reconcile  # noqa: E402
import calendar_crm  # noqa: E402
from contact_quality import classify_contact  # noqa: E402


class ConsumerMailDomainsTest(unittest.TestCase):
    """'mail' as a subdomain is a sending host; as the domain it's one of the
    biggest consumer mail providers on earth, and its users are people."""

    def test_real_providers_are_not_machines(self):
        for name, email in (("Ivan Petrov", "ivan.petrov@mail.ru"),
                            ("Anna Weber", "anna.weber@mail.com"),
                            ("Marco Rossi", "marco.rossi@email.it"),
                            ("Sam Carter", "sam@email.com")):
            verdict = classify_contact(name, email)["verdict"]
            with self.subTest(email=email):
                self.assertNotEqual(
                    verdict, "non_person",
                    f"{email} would be muted out of the graph")

    def test_machine_subdomains_are_still_caught(self):
        for name, email in (("Notion", "calendar-invite@em.notion.so"),
                            ("Someone", "hi@mail.notifications.example.com"),
                            ("Thing", "x@bounce.sendgrid.example.org")):
            with self.subTest(email=email):
                self.assertEqual(classify_contact(name, email)["verdict"],
                                 "non_person")


class PrioritizedPeopleSurviveReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "data", "db.sqlite")
        self.conn = trellis.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_apply_never_overwrites_a_priority_the_user_set(self):
        cid = trellis.find_or_create_person(
            self.conn, name="Support Desk", email="support@partner.test",
            origin="gmail")
        self.conn.execute("""INSERT INTO interactions
            (connection_id, kind, occurred_on, summary, source, created_at)
            VALUES (?,?,?,?,?,?)""",
            (cid, "email", "2026-06-01", "email sent", "gmail", trellis.now()))
        trellis.set_priority(self.conn, cid, "important")
        self.conn.commit()

        owner_ids = trellis.load_owner_identities(self.db)
        muted, protected = reconcile.apply_safe_mutes(
            self.conn, reconcile.quality_queue(self.conn, owner_ids))

        self.assertEqual([], [e for e in muted if e["id"] == cid])
        self.assertEqual([cid], [e["id"] for e in protected])
        self.assertEqual(trellis.meta_for(self.conn, cid)["priority"], "important")


class MergePreservesFollowUpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = trellis.connect(os.path.join(self.tmp.name, "data", "db.sqlite"))
        self.keep = trellis.find_or_create_person(
            self.conn, name="Ada Lovelace", url="https://x.test/ada", origin="linkedin")
        self.dup = trellis.find_or_create_person(
            self.conn, name="ada", email="ada@x.test", origin="gmail")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def merge(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            trellis.cmd_merge(self.conn, argparse.Namespace(src=self.dup, into=self.keep))

    def unmerge(self, merge_id=1):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            trellis.cmd_unmerge(self.conn, argparse.Namespace(merge_id=merge_id))

    def test_a_follow_up_survives_the_merge(self):
        trellis.set_follow_up(self.conn, self.dup, "2026-12-01", "after their launch")
        self.conn.commit()
        self.merge()
        meta = trellis.meta_for(self.conn, self.keep)
        self.assertEqual(meta["follow_up_on"], "2026-12-01")
        self.assertEqual(meta["follow_up_reason"], "after their launch")

    def test_the_earlier_date_wins_so_a_merge_never_delays_a_reminder(self):
        trellis.set_follow_up(self.conn, self.keep, "2027-01-01", "later")
        trellis.set_follow_up(self.conn, self.dup, "2026-09-01", "sooner")
        self.conn.commit()
        self.merge()
        meta = trellis.meta_for(self.conn, self.keep)
        self.assertEqual(meta["follow_up_on"], "2026-09-01")
        self.assertEqual(meta["follow_up_reason"], "sooner")

    def test_unmerge_puts_both_follow_ups_back(self):
        trellis.set_follow_up(self.conn, self.keep, "2027-01-01", "later")
        trellis.set_follow_up(self.conn, self.dup, "2026-09-01", "sooner")
        self.conn.commit()
        self.merge()
        self.unmerge()
        self.assertEqual(trellis.meta_for(self.conn, self.keep)["follow_up_on"],
                         "2027-01-01")
        self.assertEqual(trellis.meta_for(self.conn, self.dup)["follow_up_on"],
                         "2026-09-01")
        self.assertEqual(trellis.meta_for(self.conn, self.dup)["follow_up_reason"],
                         "sooner")


class UpcomingMeetingIsVisibleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "data", "db.sqlite")
        self.conn = trellis.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_someone_you_are_about_to_meet_shows_up(self):
        """Before this fix, a calendar contact with only a future meeting was
        filtered out of every screen until after you'd met them."""
        calendar_crm.ingest_records(self.conn, [{
            "event_id": "ev-future", "date": "2099-03-01",
            "attendees": [{"name": "Grace Hopper", "email": "grace@navy.test"}]}],
            today="2026-08-11")
        names = {r["full_name"] for r in
                 self.conn.execute("SELECT full_name FROM people_v")}
        self.assertIn("Grace Hopper", names)

    def test_a_bare_address_with_no_signal_at_all_stays_hidden(self):
        trellis.find_or_create_person(self.conn, name="Silent Stray",
                                      email="silent@x.test", origin="gmail")
        self.conn.commit()
        names = {r["full_name"] for r in
                 self.conn.execute("SELECT full_name FROM people_v")}
        self.assertNotIn("Silent Stray", names)


class DirectionIsHonestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = trellis.connect(os.path.join(self.tmp.name, "data", "db.sqlite"))
        self.cid = trellis.find_or_create_person(
            self.conn, name="Both Ways", email="bw@x.test", origin="gmail")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, date, direction, ref):
        self.conn.execute("""INSERT INTO interactions
            (connection_id, kind, occurred_on, summary, source, source_ref,
             created_at, direction) VALUES (?,?,?,?,?,?,?,?)""",
            (self.cid, "email", date, "email", "gmail", ref, trellis.now(), direction))
        self.conn.commit()

    def row(self):
        return [r for r in trellis.warmth_rows(self.conn) if r["id"] == self.cid][0]

    def test_a_mixed_day_reports_no_direction_either_way(self):
        """You wrote and they wrote on the same day: 'who wrote last' has no
        answer, and inventing one from insertion order is a small lie."""
        self.add("2026-06-01", "sent", "a")
        self.add("2026-06-01", "received", "b")
        self.assertIsNone(self.row()["direction"])

    def test_direction_does_not_depend_on_ingest_order(self):
        self.add("2026-06-01", "received", "later-inserted-but-same-day")
        first = self.row()["direction"]
        self.add("2026-06-01", "received", "another")
        self.assertEqual(first, self.row()["direction"])
        self.assertEqual(first, "received")

    def test_an_agreeing_day_still_reports_its_direction(self):
        self.add("2026-06-01", "sent", "a")
        self.add("2026-06-01", "sent", "b")
        self.assertEqual(self.row()["direction"], "sent")


class BackfillMatchesTheOldDisplayTest(unittest.TestCase):
    """The backfill runs once per install and only on NULLs, so anything it
    misses is missed permanently. It must be at least as wide as the summary
    scanning it replaced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "data", "db.sqlite")
        self.conn = trellis.connect(self.db)
        self.cid = trellis.find_or_create_person(
            self.conn, name="Legacy Person", email="lp@x.test", origin="gmail")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_legacy_phrasings_still_get_a_direction(self):
        legacy = {
            "email sent": "sent",
            "Email Sent to them": "sent",
            "reply sent after the call": "sent",
            "email received": "received",
            "Email received from them": "received",
            "email exchanged": None,
            "meeting held": None,
        }
        for i, summary in enumerate(legacy):
            self.conn.execute("""INSERT INTO interactions
                (connection_id, kind, occurred_on, summary, source, source_ref,
                 created_at) VALUES (?,?,?,?,?,?,?)""",
                (self.cid, "email", "2026-01-01", summary, "gmail", f"l{i}",
                 trellis.now()))
        self.conn.execute("UPDATE interactions SET direction=NULL")
        self.conn.commit()

        trellis.migrate(self.conn)

        got = {r["summary"]: r["direction"] for r in
               self.conn.execute("SELECT summary, direction FROM interactions")}
        self.assertEqual(got, legacy)


if __name__ == "__main__":
    unittest.main()

"""The CRM-unify data contract: migration, the people_v view, and the
two-layer status model. These are the tests that protect other people's
databases — a change that breaks one of these breaks an installed copy."""

import argparse
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import trellis  # noqa: E402


def make_person(conn, name, source="linkedin", email="", company="Example", url=""):
    parts = name.split(" ", 1)
    cur = conn.execute(
        """INSERT INTO connections
           (natural_key, first_name, last_name, full_name, url, email,
            company, title, func, rank, source, first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"t:{name.lower()}:{source}", parts[0], parts[1] if len(parts) > 1 else "",
         name, url, email, company, "", "Other", 2, source,
         trellis.now(), trellis.now()))
    conn.commit()
    return cur.lastrowid


class MigrationTest(unittest.TestCase):
    """migrate() must be additive, idempotent, and loud about real mismatches."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "data", "linkedin.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _legacy_db(self):
        """A v1.11.1-shaped DB: no direction column, no follow-up columns,
        no calendar_plans, no view — exactly what an existing install has."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(trellis.CONNECTIONS_DDL)
        conn.executescript("""
        CREATE TABLE interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id INTEGER NOT NULL REFERENCES connections(id),
            kind TEXT, occurred_on TEXT, summary TEXT,
            source TEXT, source_ref TEXT, confidence REAL DEFAULT 1.0, created_at TEXT);
        CREATE TABLE person_meta (
            connection_id INTEGER PRIMARY KEY REFERENCES connections(id),
            priority TEXT DEFAULT 'normal', mode TEXT, updated_at TEXT);
        """)
        return conn

    def test_migrate_backfills_direction_from_legacy_summaries(self):
        conn = self._legacy_db()
        cid = make_person(conn, "Legacy Person")
        for summary in ("email sent", "email received", "email exchanged",
                        "Email Sent to them", "meeting held"):
            conn.execute("""INSERT INTO interactions
                (connection_id, kind, occurred_on, summary, source, created_at)
                VALUES (?,?,?,?,?,?)""",
                (cid, "email", "2026-01-01", summary, "gmail", trellis.now()))
        conn.commit()

        trellis.migrate(conn)

        got = [(r["summary"], r["direction"]) for r in
               conn.execute("SELECT summary, direction FROM interactions ORDER BY id")]
        self.assertEqual(got, [
            ("email sent", "sent"),
            ("email received", "received"),
            ("email exchanged", None),      # the direction-less legacy era
            ("Email Sent to them", "sent"),  # case-insensitive prefix match
            ("meeting held", None),
        ])
        conn.close()

    def test_migrate_is_idempotent_and_preserves_rows(self):
        conn = self._legacy_db()
        cid = make_person(conn, "Steady Person")
        conn.execute("""INSERT INTO interactions
            (connection_id, kind, occurred_on, summary, source, created_at)
            VALUES (?,?,?,?,?,?)""",
            (cid, "email", "2026-01-01", "email sent", "gmail", trellis.now()))
        conn.commit()
        before = (conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0])

        trellis.migrate(conn)
        # A hand-edit after the first migration must not be clobbered by the second.
        conn.execute("UPDATE interactions SET direction='received'")
        conn.commit()
        trellis.migrate(conn)
        trellis.migrate(conn)

        after = (conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0],
                 conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0])
        self.assertEqual(before, after)
        self.assertEqual(
            conn.execute("SELECT direction FROM interactions").fetchone()[0],
            "received", "backfill must only touch NULLs")
        conn.close()

    def test_migrate_over_an_already_patched_db_is_a_no_op(self):
        """The Hermes case: columns and view already exist. Adopting the
        shipped code must not fail or rewrite data."""
        conn = trellis.connect(self.db_path)  # already migrated
        cid = make_person(conn, "Patched Person")
        trellis.set_follow_up(conn, cid, "2026-09-01", "keep me")
        trellis.migrate(conn)
        row = trellis.meta_for(conn, cid)
        self.assertEqual(row["follow_up_on"], "2026-09-01")
        self.assertEqual(row["follow_up_reason"], "keep me")
        conn.close()

    def test_migrate_recreates_a_stale_view_definition(self):
        conn = trellis.connect(self.db_path)
        make_person(conn, "Visible Person")
        conn.execute("DROP VIEW people_v")
        conn.execute("CREATE VIEW people_v AS SELECT * FROM connections WHERE 0")
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM people_v").fetchone()[0], 0)
        trellis.migrate(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM people_v").fetchone()[0], 1)
        conn.close()

    def test_schema_tripwire_fires_on_a_divergent_table(self):
        conn = trellis.connect(self.db_path)
        conn.execute("DROP TABLE calendar_plans")
        conn.execute("CREATE TABLE calendar_plans (id INTEGER PRIMARY KEY, whenever TEXT)")
        conn.commit()
        with self.assertRaises(SystemExit) as ctx:
            trellis.migrate(conn)
        self.assertIn("schema mismatch", str(ctx.exception))
        self.assertIn("calendar_plans", str(ctx.exception))
        conn.close()


class PeopleViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = trellis.connect(os.path.join(self.tmp.name, "data", "db.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def names(self):
        return {r["full_name"] for r in self.conn.execute("SELECT full_name FROM people_v")}

    def add_interaction(self, cid):
        self.conn.execute("""INSERT INTO interactions
            (connection_id, kind, occurred_on, summary, source, created_at)
            VALUES (?,?,?,?,?,?)""",
            (cid, "email", "2026-06-01", "email sent", "gmail", trellis.now()))
        self.conn.commit()

    def test_view_drops_merged_tombstones_and_muted_people(self):
        keep = make_person(self.conn, "Kept Person")
        gone = make_person(self.conn, "Merged Person")
        quiet = make_person(self.conn, "Muted Person")
        self.conn.execute("UPDATE connections SET source=? WHERE id=?",
                          (f"merged_into_{keep}", gone))
        trellis.set_priority(self.conn, quiet, "muted")
        self.conn.commit()
        self.assertEqual(self.names(), {"Kept Person"})

    def test_gmail_contacts_appear_only_once_they_carry_signal(self):
        silent = make_person(self.conn, "Silent Stray", source="gmail",
                             email="silent@x.test")
        talker = make_person(self.conn, "Talking Stray", source="gmail",
                             email="talks@x.test")
        self.add_interaction(talker)
        self.assertEqual(self.names(), {"Talking Stray"})
        self.assertNotIn("Silent Stray", self.names())
        self.add_interaction(silent)
        self.assertIn("Silent Stray", self.names())

    def test_source_gate_is_case_insensitive(self):
        """A 'Gmail' row must be gated exactly like a 'gmail' row — otherwise
        a mixed-case sweep leaks unreconciled contacts onto every screen."""
        make_person(self.conn, "Capital Stray", source="Gmail", email="cap@x.test")
        make_person(self.conn, "Capital Calendar", source="CALENDAR", email="cal@x.test")
        self.assertEqual(self.names(), set())

    def test_view_reports_normalized_lowercase_source(self):
        cid = make_person(self.conn, "Mixed Case", source="LinkedIn")
        self.add_interaction(cid)
        row = self.conn.execute(
            "SELECT source FROM people_v WHERE full_name='Mixed Case'").fetchone()
        self.assertEqual(row["source"], "linkedin")

    def test_find_or_create_person_writes_lowercase_source(self):
        cid = trellis.find_or_create_person(self.conn, name="New Contact",
                                            email="new@x.test", origin="Gmail")
        row = self.conn.execute("SELECT source FROM connections WHERE id=?",
                                (cid,)).fetchone()
        self.assertEqual(row["source"], "gmail")


class DirectionAndWarmthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "data", "db.sqlite")
        self.conn = trellis.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_ingest_writes_the_direction_column(self):
        trellis._ingest_one(self.conn, {
            "person": {"name": "Direction Person", "email": "d@x.test"},
            "kind": "email", "date": "2026-06-01", "summary": "email received",
            "source": "gmail", "source_ref": "m1"})
        trellis._ingest_one(self.conn, {
            "person": {"name": "Explicit Person", "email": "e@x.test"},
            "kind": "email", "date": "2026-06-01", "summary": "swapped notes",
            "direction": "sent", "source": "gmail", "source_ref": "m2"})
        self.conn.commit()
        got = {r["source_ref"]: r["direction"] for r in
               self.conn.execute("SELECT source_ref, direction FROM interactions")}
        self.assertEqual(got, {"m1": "received", "m2": "sent"})

    def test_warmth_prefers_a_newer_directionless_touch(self):
        """If the most recent contact was a meeting, we no longer know who
        wrote last — reporting the older email's direction would be a lie."""
        cid = trellis.find_or_create_person(self.conn, name="Meeting Last",
                                            email="ml@x.test", origin="gmail")
        self.conn.execute("""INSERT INTO interactions
            (connection_id, kind, occurred_on, summary, source, created_at, direction)
            VALUES (?,?,?,?,?,?,?)""",
            (cid, "email", "2026-05-01", "email sent", "gmail", trellis.now(), "sent"))
        self.conn.execute("""INSERT INTO interactions
            (connection_id, kind, occurred_on, summary, source, created_at, direction)
            VALUES (?,?,?,?,?,?,?)""",
            (cid, "meeting", "2026-06-01", "meeting held", "calendar",
             trellis.now(), None))
        self.conn.commit()
        row = [r for r in trellis.warmth_rows(self.conn) if r["id"] == cid][0]
        self.assertIsNone(row["direction"])

    def test_warmth_reads_the_column_not_the_summary(self):
        cid = trellis.find_or_create_person(self.conn, name="Column Truth",
                                            email="ct@x.test", origin="gmail")
        self.conn.execute("""INSERT INTO interactions
            (connection_id, kind, occurred_on, summary, source, created_at, direction)
            VALUES (?,?,?,?,?,?,?)""",
            (cid, "email", "2026-06-01", "a summary that says nothing", "gmail",
             trellis.now(), "received"))
        self.conn.commit()
        row = [r for r in trellis.warmth_rows(self.conn) if r["id"] == cid][0]
        self.assertEqual(row["direction"], "received")


class OwnerExclusionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "db.sqlite")
        with open(os.path.join(self.data_dir, "owner_identities.json"), "w",
                  encoding="utf-8") as f:
            f.write('{"owner_emails": ["me@personal.test"], '
                    '"owner_domains": ["myco.test"]}')
        self.conn = trellis.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_owner_identities_load_and_match(self):
        ids = trellis.load_owner_identities(self.db_path)
        self.assertTrue(trellis.is_owner_address("me@personal.test", ids))
        self.assertTrue(trellis.is_owner_address("Teammate@MyCo.test", ids))
        self.assertFalse(trellis.is_owner_address("someone@else.test", ids))
        self.assertFalse(trellis.is_owner_address("", ids))
        self.assertFalse(trellis.is_owner_address(None, ids))

    def test_missing_owner_file_is_not_an_error(self):
        other = os.path.join(self.tmp.name, "elsewhere", "db.sqlite")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        ids = trellis.load_owner_identities(other)
        self.assertEqual(ids, {"owner_emails": [], "owner_domains": []})

    def test_ingest_skips_owner_and_teammate_addresses(self):
        ids = trellis.load_owner_identities(self.db_path)
        self.assertEqual(trellis._ingest_one(self.conn, {
            "person": {"name": "Me", "email": "me@personal.test"},
            "summary": "email sent", "source": "gmail", "source_ref": "o1"},
            owner_ids=ids), "owner_skipped")
        self.assertEqual(trellis._ingest_one(self.conn, {
            "person": {"name": "Colleague", "email": "jess@myco.test"},
            "summary": "email sent", "source": "gmail", "source_ref": "o2"},
            owner_ids=ids), "owner_skipped")
        self.assertEqual(trellis._ingest_one(self.conn, {
            "person": {"name": "Real Contact", "email": "real@outside.test"},
            "summary": "email sent", "source": "gmail", "source_ref": "o3"},
            owner_ids=ids), "added")
        self.conn.commit()
        names = {r["full_name"] for r in
                 self.conn.execute("SELECT full_name FROM connections")}
        self.assertEqual(names, {"Real Contact"})


class FollowUpParsingTest(unittest.TestCase):
    def test_relative_and_absolute_forms(self):
        today = date(2026, 8, 11)
        self.assertEqual(trellis.parse_follow_up("in 1 week", today), "2026-08-18")
        self.assertEqual(trellis.parse_follow_up("in 6 months", today), "2027-02-11")
        self.assertEqual(trellis.parse_follow_up("3 days", today), "2026-08-14")
        self.assertEqual(trellis.parse_follow_up("in 1 year", today), "2027-08-11")
        self.assertEqual(trellis.parse_follow_up("2026-12-25", today), "2026-12-25")

    def test_month_arithmetic_clamps_short_months(self):
        self.assertEqual(trellis.parse_follow_up("in 1 month", date(2026, 1, 31)),
                         "2026-02-28")

    def test_garbage_exits_loudly(self):
        with self.assertRaises(SystemExit):
            trellis.parse_follow_up("whenever")
        with self.assertRaises(SystemExit):
            trellis.parse_follow_up("2026-13-45")

    def test_absurd_offsets_exit_instead_of_overflowing(self):
        for text in ("in 999999999 months", "in 100000000 days", "in 5000 years"):
            with self.assertRaises(SystemExit):
                trellis.parse_follow_up(text, date(2026, 8, 11))


class StatusModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "data", "db.sqlite")
        self.conn = trellis.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def capture(self, **kw):
        args = dict(name=None, email=None, url=None, company=None, title=None,
                    interaction=None, kind=None, date=None, note=None,
                    note_category=None, loop=None, due=None, priority=None,
                    prioritize=False, deprioritize=False, mode=None,
                    follow_up=None, follow_up_reason=None, clear_follow_up=False,
                    source=None, source_ref=None)
        args.update(kw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            trellis.cmd_capture(self.conn, argparse.Namespace(**args))
        return out.getvalue()

    def radar(self, limit=10):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            trellis.cmd_radar(self.conn, argparse.Namespace(limit=limit))
        return out.getvalue()

    def test_prioritize_and_deprioritize_map_onto_priority(self):
        self.capture(name="Up Person", prioritize=True)
        self.capture(name="Down Person", deprioritize=True)
        got = {r["full_name"]: r["priority"] for r in self.conn.execute(
            "SELECT c.full_name, m.priority FROM person_meta m "
            "JOIN connections c ON c.id=m.connection_id")}
        self.assertEqual(got, {"Up Person": "important", "Down Person": "muted"})

    def test_deprioritized_person_still_fires_a_due_follow_up(self):
        """The whole point of the two layers: 'not now, but check back'."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.capture(name="Snoozed Person", deprioritize=True,
                     follow_up=yesterday, follow_up_reason="after their launch")
        text = self.radar()
        self.assertIn("Snoozed Person", text)
        self.assertIn("after their launch", text)

    def test_future_follow_up_stays_quiet(self):
        later = (date.today() + timedelta(days=30)).isoformat()
        self.capture(name="Later Person", follow_up=later)
        self.assertNotIn("Later Person", self.radar())

    def test_due_follow_up_outranks_an_overdue_loop(self):
        self.capture(name="Loop Person", loop="send the deck", due="2020-01-01")
        self.capture(name="Due Person",
                     follow_up=(date.today() - timedelta(days=2)).isoformat())
        text = self.radar()
        self.assertLess(text.index("Due Person"), text.index("Loop Person"))

    def test_clearing_a_follow_up_silences_radar(self):
        self.capture(name="Cleared Person",
                     follow_up=(date.today() - timedelta(days=1)).isoformat())
        self.assertIn("Cleared Person", self.radar())
        self.capture(name="Cleared Person", clear_follow_up=True)
        self.assertNotIn("Cleared Person", self.radar())
        row = self.conn.execute(
            "SELECT follow_up_on, follow_up_reason FROM person_meta").fetchone()
        self.assertIsNone(row["follow_up_on"])
        self.assertIsNone(row["follow_up_reason"])

    def test_muted_person_without_a_follow_up_stays_out_of_radar(self):
        cid = trellis.find_or_create_person(self.conn, name="Quiet Person",
                                            email="q@x.test", origin="gmail")
        self.conn.execute("""INSERT INTO interactions
            (connection_id, kind, occurred_on, summary, source, created_at)
            VALUES (?,?,?,?,?,?)""",
            (cid, "email", "2020-01-01", "email sent", "gmail", trellis.now()))
        trellis.set_priority(self.conn, cid, "muted")
        self.conn.commit()
        self.assertNotIn("Quiet Person", self.radar())

    def test_capture_preserves_critical_when_setting_a_follow_up(self):
        self.capture(name="Critical Person", priority="critical")
        self.capture(name="Critical Person", follow_up="in 1 week")
        row = self.conn.execute("SELECT priority, follow_up_on FROM person_meta").fetchone()
        self.assertEqual(row["priority"], "critical")
        self.assertIsNotNone(row["follow_up_on"])


class ApplyStatusTest(unittest.TestCase):
    """The paste-block round-trip the screens rely on in static mode."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = trellis.connect(os.path.join(self.tmp.name, "data", "db.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def apply(self, payload):
        import json
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            trellis.cmd_apply(self.conn, argparse.Namespace(
                json=json.dumps(payload), file=None))
        return out.getvalue()

    def test_priorities_and_follow_ups_apply(self):
        self.apply({
            "priorities": [{"name": "Synced Person", "email": "s@x.test",
                            "priority": "muted"}],
            "follow_ups": [{"name": "Synced Person", "email": "s@x.test",
                            "on": "2026-12-01", "reason": "after the holidays"}]})
        row = self.conn.execute("""SELECT m.priority, m.follow_up_on, m.follow_up_reason
            FROM person_meta m JOIN connections c ON c.id=m.connection_id
            WHERE c.full_name='Synced Person'""").fetchone()
        self.assertEqual((row["priority"], row["follow_up_on"], row["follow_up_reason"]),
                         ("muted", "2026-12-01", "after the holidays"))

    def test_explicit_priority_may_downgrade_unlike_a_flag(self):
        self.apply({"priorities": [{"name": "Down Person", "priority": "important"}]})
        self.apply({"priorities": [{"name": "Down Person", "priority": "normal"}]})
        row = self.conn.execute("SELECT priority FROM person_meta").fetchone()
        self.assertEqual(row["priority"], "normal")

    def test_flag_still_only_raises(self):
        self.apply({"priorities": [{"name": "Flagged Person", "priority": "critical"}]})
        self.apply({"flags": [{"name": "Flagged Person"}]})
        row = self.conn.execute("SELECT priority FROM person_meta").fetchone()
        self.assertEqual(row["priority"], "critical")

    def test_unknown_priority_is_skipped_not_written(self):
        text = self.apply({"priorities": [{"name": "Odd Person", "priority": "vip"}]})
        self.assertIn("skipping unknown priority", text)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM person_meta").fetchone())

    def test_legacy_flags_and_notes_payload_still_works(self):
        self.apply({"flags": [{"name": "Legacy Person", "company": "Acme"}],
                    "notes": [{"name": "Legacy Person", "note": "met at a talk"}]})
        row = self.conn.execute("""SELECT m.priority, n.content
            FROM person_meta m JOIN connections c ON c.id=m.connection_id
            LEFT JOIN notes n ON n.connection_id=c.id
            WHERE c.full_name='Legacy Person'""").fetchone()
        self.assertEqual(row["priority"], "important")
        self.assertEqual(row["content"], "met at a talk")


if __name__ == "__main__":
    unittest.main()

"""reconcile.py: the review queues are proposals, and only the reversible,
confident subset is ever applied.

The DB lives in a temp data/ dir so owner_identities.json can sit beside it,
exactly as trellis.load_owner_identities expects.
"""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest


SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import reconcile
import trellis


class ReconcileTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "linkedin.db")
        with open(os.path.join(self.data_dir, "owner_identities.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"owner_emails": ["mari@filament.dm"],
                       "owner_domains": ["myco.test"]}, f)
        self.conn = trellis.connect(self.db_path)
        self.owner_ids = trellis.load_owner_identities(self.db_path)
        self.seed()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # -- fixtures -----------------------------------------------------------

    def add_person(self, full_name, source, email="", company="",
                   first=None, last=None, key=None):
        parts = (full_name or "").split(" ", 1)
        cur = self.conn.execute(
            """INSERT INTO connections
               (natural_key, first_name, last_name, full_name, url, email,
                company, title, func, rank, source, first_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key or f"test:{(full_name or email or 'blank').lower()}",
             first if first is not None else (parts[0] if parts else ""),
             last if last is not None else (parts[1] if len(parts) > 1 else ""),
             full_name, "", email, company, "", "Other", 2, source,
             trellis.now(), trellis.now()))
        self.conn.commit()
        return cur.lastrowid

    def add_interaction(self, cid, ref):
        self.conn.execute(
            """INSERT INTO interactions
               (connection_id, kind, occurred_on, summary, source, source_ref,
                created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (cid, "email", "2026-07-20", "email received", "gmail", ref,
             trellis.now()))
        self.conn.commit()

    def seed(self):
        self.linkedin_id = self.add_person(
            "Brock Kelly", "linkedin", company="Filament",
            first="Brock", last="Kelly")
        self.stray_id = self.add_person(
            "brock", "gmail", email="brock@corp.test", key="test:stray-brock")
        self.add_interaction(self.stray_id, "msg-brock-1")

        self.nameless_id = self.add_person(
            None, "gmail", email="ghost@corp.test", key="test:nameless")
        self.add_interaction(self.nameless_id, "msg-ghost-1")

        self.nonperson_id = self.add_person(
            "Transactions", "gmail", email="transactions@bank.test")
        self.add_interaction(self.nonperson_id, "msg-txn-1")

        self.owner_id = self.add_person(
            "Teammate Person", "gmail", email="someone@myco.test")
        self.add_interaction(self.owner_id, "msg-owner-1")

        self.person_id = self.add_person(
            "Ada Lovelace", "gmail", email="ada.lovelace@example.com")
        self.add_interaction(self.person_id, "msg-ada-1")

    def quality(self):
        return reconcile.quality_queue(self.conn, self.owner_ids)

    def meta_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM person_meta").fetchone()[0]

    def priority(self, cid):
        row = trellis.meta_for(self.conn, cid)
        return row["priority"] if row else None

    # -- identity queue -----------------------------------------------------

    def test_identity_queue_proposes_the_stray_against_linkedin(self):
        proposals = reconcile.identity_queue(self.conn)
        match = [p for p in proposals
                 if p["stray_id"] == self.stray_id
                 and p["linkedin_id"] == self.linkedin_id]
        self.assertEqual(1, len(match), proposals)
        entry = match[0]
        self.assertEqual("brock", entry["stray_name"])
        self.assertEqual("Brock Kelly", entry["linkedin_name"])
        self.assertIn("--from", entry["merge_command"])
        self.assertIn("--into", entry["merge_command"])
        self.assertIn(str(self.stray_id), entry["merge_command"])
        self.assertIn(str(self.linkedin_id), entry["merge_command"])

    def test_identity_queue_survives_a_null_full_name(self):
        proposals = reconcile.identity_queue(self.conn)
        self.assertNotIn(self.nameless_id, [p["stray_id"] for p in proposals])

    def test_identity_queue_is_deduped_and_ordered(self):
        proposals = reconcile.identity_queue(self.conn)
        keys = [(p["stray_id"], p["linkedin_id"]) for p in proposals]
        self.assertEqual(len(keys), len(set(keys)))
        names = [(p["stray_name"] or "").lower() for p in proposals]
        self.assertEqual(names, sorted(names))

    # -- quality queue ------------------------------------------------------

    def test_quality_queue_verdicts(self):
        by_id = {e["id"]: e for e in self.quality()}
        self.assertEqual("non_person", by_id[self.nonperson_id]["verdict"])
        self.assertEqual("owner", by_id[self.owner_id]["verdict"])
        self.assertEqual("ambiguous", by_id[self.stray_id]["verdict"])
        for entry in by_id.values():
            self.assertTrue(entry["signals"])

    def test_person_shaped_contact_is_absent_from_the_queue(self):
        self.assertNotIn(self.person_id, [e["id"] for e in self.quality()])

    def test_linkedin_contacts_are_never_queued(self):
        self.assertNotIn(self.linkedin_id, [e["id"] for e in self.quality()])

    # -- queue files --------------------------------------------------------

    def test_write_queue_writes_valid_json(self):
        p1 = reconcile.write_queue(
            self.data_dir, "linkedin_identity_review_queue.json",
            reconcile.identity_queue(self.conn))
        p2 = reconcile.write_queue(
            self.data_dir, "contact_quality_review.json", self.quality())
        for path in (p1, p2):
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIsInstance(data, list)
        with open(p2, encoding="utf-8") as f:
            quality = json.load(f)
        self.assertTrue(all("verdict" in e and "signals" in e for e in quality))

    # -- apply --------------------------------------------------------------

    def test_propose_only_writes_no_metadata(self):
        before = self.meta_count()
        reconcile.identity_queue(self.conn)
        self.quality()
        self.assertEqual(0, before)
        self.assertEqual(0, self.meta_count())

    def test_apply_safe_mutes_only_touches_confident_rows(self):
        muted, protected = reconcile.apply_safe_mutes(self.conn, self.quality())
        self.assertEqual({self.nonperson_id, self.owner_id},
                         {e["id"] for e in muted})
        self.assertEqual([], protected)
        self.assertEqual("muted", self.priority(self.nonperson_id))
        self.assertEqual("muted", self.priority(self.owner_id))
        self.assertIsNone(self.priority(self.stray_id))
        self.assertIsNone(self.priority(self.nameless_id))
        self.assertIsNone(self.priority(self.person_id))

    def test_apply_safe_mutes_twice_is_a_no_op(self):
        reconcile.apply_safe_mutes(self.conn, self.quality())
        again, protected = reconcile.apply_safe_mutes(self.conn, self.quality())
        self.assertEqual([], again)
        self.assertEqual([], protected)
        self.assertEqual(2, self.meta_count())

    def test_a_person_you_prioritized_is_never_muted_by_heuristic(self):
        """Your own judgement outranks the classifier — and since `unmute`
        can only restore 'normal', muting here would destroy the mark."""
        trellis.set_priority(self.conn, self.nonperson_id, "important")
        self.conn.commit()
        muted, protected = reconcile.apply_safe_mutes(self.conn, self.quality())
        self.assertEqual({self.owner_id}, {e["id"] for e in muted})
        self.assertEqual({self.nonperson_id}, {e["id"] for e in protected})
        self.assertEqual("important", self.priority(self.nonperson_id))

    def test_unmute_restores_a_muted_contact(self):
        reconcile.apply_safe_mutes(self.conn, self.quality())
        with contextlib.redirect_stdout(io.StringIO()):
            trellis.cmd_unmute(
                self.conn, argparse.Namespace(id=self.nonperson_id, name=None))
        self.assertEqual("normal", self.priority(self.nonperson_id))
        entry = {e["id"]: e for e in self.quality()}[self.nonperson_id]
        self.assertFalse(entry["muted"])


if __name__ == "__main__":
    unittest.main()

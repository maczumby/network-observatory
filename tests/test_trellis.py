import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest


SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import trellis


class TrellisTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "trellis.db")
        self.conn = trellis.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_person(self, name, source="linkedin", email="", company="Example"):
        parts = name.split(" ", 1)
        cur = self.conn.execute(
            """INSERT INTO connections
               (natural_key, first_name, last_name, full_name, url, email,
                company, title, func, rank, source, first_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"test:{name.lower()}",
                parts[0],
                parts[1] if len(parts) > 1 else "",
                name,
                "",
                email,
                company,
                "",
                "Other",
                2,
                source,
                trellis.now(),
                trellis.now(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_linkedin_person_works_without_enrichment(self):
        self.add_person("LinkedIn Only")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            trellis.cmd_recall(
                self.conn, argparse.Namespace(query="LinkedIn Only")
            )
        self.assertIn("from your LinkedIn graph only", out.getvalue())

    def test_optional_source_ingest_is_idempotent(self):
        event = {
            "person": {"name": "Gmail Person", "email": "person@example.com"},
            "kind": "email",
            "date": "2026-07-01",
            "summary": "Exchanged email",
            "source": "gmail",
            "source_ref": "message-123",
        }
        self.assertEqual("added", trellis._ingest_one(self.conn, event))
        self.assertEqual("skipped", trellis._ingest_one(self.conn, event))
        count = self.conn.execute(
            "SELECT count(*) FROM interactions"
        ).fetchone()[0]
        self.assertEqual(1, count)

    def test_merge_and_unmerge_restore_history_and_metadata(self):
        src = self.add_person("Source Person", source="manual")
        dst = self.add_person("Destination Person")
        interaction = self.conn.execute(
            """INSERT INTO interactions
               (connection_id, kind, occurred_on, summary, source, created_at)
               VALUES (?,?,?,?,?,?)""",
            (src, "meeting", "2026-07-01", "Met", "calendar", trellis.now()),
        ).lastrowid
        self.conn.execute(
            """INSERT INTO person_meta
               (connection_id, priority, mode, updated_at) VALUES (?,?,?,?)""",
            (src, "critical", "friend", "source-time"),
        )
        self.conn.execute(
            """INSERT INTO person_meta
               (connection_id, priority, mode, updated_at) VALUES (?,?,?,?)""",
            (dst, "normal", "prospect", "destination-time"),
        )
        self.conn.commit()

        with contextlib.redirect_stdout(io.StringIO()):
            trellis.cmd_merge(
                self.conn, argparse.Namespace(src=src, into=dst)
            )
        merge_id = self.conn.execute(
            "SELECT id FROM identity_merges"
        ).fetchone()["id"]

        moved = self.conn.execute(
            "SELECT connection_id FROM interactions WHERE id=?", (interaction,)
        ).fetchone()["connection_id"]
        merged_meta = trellis.meta_for(self.conn, dst)
        self.assertEqual(dst, moved)
        self.assertEqual("critical", merged_meta["priority"])
        self.assertEqual("prospect", merged_meta["mode"])
        self.assertIsNone(trellis.meta_for(self.conn, src))
        self.assertEqual(
            f"merged_into_{dst}", trellis.person_row(self.conn, src)["source"]
        )

        with contextlib.redirect_stdout(io.StringIO()):
            trellis.cmd_unmerge(
                self.conn, argparse.Namespace(merge_id=merge_id)
            )

        restored = self.conn.execute(
            "SELECT connection_id FROM interactions WHERE id=?", (interaction,)
        ).fetchone()["connection_id"]
        self.assertEqual(src, restored)
        self.assertEqual("manual", trellis.person_row(self.conn, src)["source"])
        self.assertEqual("critical", trellis.meta_for(self.conn, src)["priority"])
        self.assertEqual("friend", trellis.meta_for(self.conn, src)["mode"])
        self.assertEqual("normal", trellis.meta_for(self.conn, dst)["priority"])
        self.assertEqual("prospect", trellis.meta_for(self.conn, dst)["mode"])
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT undone_at FROM identity_merges WHERE id=?", (merge_id,)
            ).fetchone()["undone_at"]
        )

    def test_unmerge_refuses_to_overwrite_newer_metadata(self):
        src = self.add_person("First Identity")
        dst = self.add_person("Second Identity")
        with contextlib.redirect_stdout(io.StringIO()):
            trellis.cmd_merge(
                self.conn, argparse.Namespace(src=src, into=dst)
            )
        merge_id = self.conn.execute(
            "SELECT id FROM identity_merges"
        ).fetchone()["id"]
        self.conn.execute(
            """INSERT INTO person_meta
               (connection_id, priority, mode, updated_at) VALUES (?,?,?,?)""",
            (dst, "important", "friend", trellis.now()),
        )
        self.conn.commit()

        with self.assertRaisesRegex(SystemExit, "metadata changed after the merge"):
            trellis.cmd_unmerge(
                self.conn, argparse.Namespace(merge_id=merge_id)
            )
        self.assertEqual(
            f"merged_into_{dst}", trellis.person_row(self.conn, src)["source"]
        )

    def test_merged_alias_resolves_to_canonical_person(self):
        src = self.add_person("Alias Name", email="alias@example.com")
        dst = self.add_person("Canonical Name")
        with contextlib.redirect_stdout(io.StringIO()):
            trellis.cmd_merge(
                self.conn, argparse.Namespace(src=src, into=dst)
            )

        resolved = trellis.find_person(
            self.conn, email="alias@example.com"
        )
        self.assertEqual(dst, resolved["id"])


if __name__ == "__main__":
    unittest.main()

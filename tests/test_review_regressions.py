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


class IdentityMatchingScalesTest(unittest.TestCase):
    """Matching used to compare every swept contact against every LinkedIn
    person, so the People screen took ~11s at 1,200 people and never finished
    at the 12,000 the README advertises. Guard the shape of the fix: the
    render path does no matching at all, and the matcher itself is bounded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "data", "db.sqlite")
        self.conn = trellis.connect(self.db)
        # Distinct names that survive normalization — digits get stripped, so
        # "Given1"/"Given2" would collapse into one string and make every
        # person look like every other.
        syl = ["an", "be", "cor", "dal", "el", "fen", "gar", "hal", "ir", "jen",
               "kal", "lor", "mar", "nor", "ol", "pra", "quin", "ras", "sel", "tor"]
        rows = []
        for i in range(2000):
            first = syl[i % 20] + syl[(i // 20) % 20]
            last = syl[(i // 7) % 20] + syl[(i // 3) % 20] + syl[i % 20]
            rows.append((f"s:{i}", first.capitalize(), last.capitalize(),
                         f"{first.capitalize()} {last.capitalize()}",
                         f"https://li.test/{i}", "", "Megacorp", "", "Other", 0, 2,
                         "linkedin", "2026-01-01", "2026-01-01"))
        self.target_first = rows[7][1]
        self.target_last = rows[7][2]
        self.target_name = rows[7][3]
        self.conn.executemany("""INSERT INTO connections (natural_key, first_name,
            last_name, full_name, url, email, company, title, func, is_founder,
            rank, source, first_seen_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        # one swept contact that really is one of them — a bare first name and
        # a work address with a digit in it, which is what sweeps actually mint
        cid = trellis.find_or_create_person(
            self.conn, name=self.target_first,
            email=f"{self.target_first.lower()}.{self.target_last.lower()}2@x.test",
            origin="gmail")
        self.conn.execute("""INSERT INTO interactions (connection_id, kind,
            occurred_on, summary, source, source_ref, created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (cid, "email", "2026-06-01", "email sent", "gmail", "s1", trellis.now()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_matching_still_finds_the_real_person(self):
        proposals = trellis._match_candidates(self.conn)
        self.assertTrue(
            any(p["linkedin_name"] == self.target_name for p in proposals),
            f"the length/prefix bounds lost the true match ({self.target_name}); "
            f"got {[p['linkedin_name'] for p in proposals]}")

    def test_an_address_with_digits_still_matches_their_name(self):
        """Sweeps mint plenty of john.smith2@… addresses; comparing those raw
        against digit-stripped names never matched."""
        proposals = trellis._match_candidates(self.conn)
        top = [p for p in proposals if p["linkedin_name"] == self.target_name]
        self.assertTrue(top, "digits in the local part lost the match")
        self.assertIn("matches their name", top[0]["why"])

    def test_the_length_bound_only_skips_impossible_pairs(self):
        # 2*min/(la+lb) is a hard ceiling on SequenceMatcher's ratio, so anything
        # the bound rejects could never have met the threshold.
        from difflib import SequenceMatcher
        pairs = [("ada lovelace", "ada lovelace"), ("jon smith", "john smith"),
                 ("bo", "bartholomew winterbottom"), ("li", "liang"),
                 ("grace hopper", "g"), ("brock", "brock kelly")]
        for a, b in pairs:
            with self.subTest(pair=(a, b)):
                if trellis._too_different(a, b):
                    self.assertLess(SequenceMatcher(None, a, b).ratio(),
                                    trellis.NAME_SIMILARITY,
                                    "bound rejected a pair that would have matched")

    def test_building_the_people_screen_does_no_matching(self):
        """The screen reads the queue reconcile writes. If it ever recomputes,
        this test gets slow — and a slow build is the bug coming back."""
        import time
        import workbench_export
        start = time.time()
        payload = workbench_export.build_payload(self.db)
        elapsed = time.time() - start
        self.assertEqual(payload["candidates"], {})
        self.assertLess(elapsed, 2.0,
                        f"page build took {elapsed:.1f}s — is it matching again?")


if __name__ == "__main__":
    unittest.main()

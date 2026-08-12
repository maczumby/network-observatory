"""The post-meeting nudge — "you met X and haven't been in touch since".

This is the reason calendar data is worth feeding in, and it shipped with no
tests at all. A nudge that fires when it shouldn't is worse than no nudge: it
teaches you to ignore radar. Every suppression rule gets a case here.
"""

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import trellis  # noqa: E402
import calendar_crm  # noqa: E402


class MeetingNudgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = trellis.connect(os.path.join(self.tmp.name, "data", "db.sqlite"))
        self.today = trellis.TODAY

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def ago(self, days):
        return (self.today - timedelta(days=days)).isoformat()

    def ahead(self, days):
        return (self.today + timedelta(days=days)).isoformat()

    def met(self, name, days_ago, email=None):
        calendar_crm.ingest_records(self.conn, [{
            "event_id": f"ev-{name}-{days_ago}", "date": self.ago(days_ago),
            "attendees": [{"name": name, "email": email or f"{name[0].lower()}@x.test"}],
        }], today=self.today.isoformat())
        return trellis.find_person(self.conn, name=name)["id"]

    def radar(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            trellis.cmd_radar(self.conn, argparse.Namespace(limit=20))
        return out.getvalue()

    # -- it fires when it should ------------------------------------------
    def test_fires_after_a_recent_meeting_with_silence_since(self):
        self.met("Annalisa", 6)
        text = self.radar()
        self.assertIn("Annalisa", text)
        self.assertIn("you met 6 days ago", text)

    def test_says_yesterday_and_today_in_words(self):
        self.met("Yesterday", 1)
        self.assertIn("you met yesterday", self.radar())
        self.conn.execute("DELETE FROM interactions")
        self.conn.commit()
        self.met("Today", 0)
        self.assertIn("you met today", self.radar())

    def test_fires_on_the_last_day_of_the_window(self):
        self.met("Edge", trellis.MEETING_FOLLOW_UP_DAYS)
        self.assertIn("Edge", self.radar())

    # -- and stays quiet when it shouldn't --------------------------------
    def test_silent_once_you_have_written_since(self):
        cid = self.met("Replied", 6)
        self.conn.execute("""INSERT INTO interactions (connection_id, kind,
            occurred_on, summary, source, source_ref, created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (cid, "email", self.ago(2), "email sent", "gmail", "r1", trellis.now()))
        self.conn.commit()
        self.assertNotIn("Replied", self.radar())

    def test_silent_when_you_wrote_the_same_day_as_the_meeting(self):
        """occurred_on is a date, so a same-day follow-up is equal, not later —
        and meeting someone then writing that afternoon is the common case."""
        cid = self.met("SameDay", 6)
        self.conn.execute("""INSERT INTO interactions (connection_id, kind,
            occurred_on, summary, source, source_ref, created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (cid, "email", self.ago(6), "email sent", "gmail", "s1", trellis.now()))
        self.conn.commit()
        self.assertNotIn("SameDay", self.radar())

    def test_silent_when_you_are_seeing_them_again(self):
        cid = self.met("Upcoming", 6)
        self.conn.execute("""INSERT INTO calendar_plans (connection_id,
            planned_on, source, source_ref, created_at) VALUES (?,?,?,?,?)""",
            (cid, self.ahead(4), "calendar", "plan-1", trellis.now()))
        self.conn.commit()
        self.assertNotIn("Upcoming", self.radar())

    def test_silent_when_an_open_loop_already_says_what_you_owe(self):
        cid = self.met("Owed", 6)
        self.conn.execute("""INSERT INTO open_loops (connection_id, description,
            status, source, created_at) VALUES (?,?, 'open', 'manual', ?)""",
            (cid, "send the deck", trellis.now()))
        self.conn.commit()
        text = self.radar()
        self.assertIn("send the deck", text)
        self.assertNotIn("haven't been in touch since", text)

    def test_silent_when_they_set_their_own_follow_up_date(self):
        cid = self.met("Dated", 6)
        trellis.set_follow_up(self.conn, cid, self.ahead(30), "after their raise")
        self.conn.commit()
        self.assertNotIn("haven't been in touch since", self.radar())

    def test_silent_for_a_deprioritized_person(self):
        cid = self.met("Muted", 6)
        trellis.set_priority(self.conn, cid, "muted")
        self.conn.commit()
        self.assertNotIn("Muted", self.radar())

    def test_silent_once_the_window_has_passed(self):
        self.met("LongAgo", trellis.MEETING_FOLLOW_UP_DAYS + 1)
        self.assertNotIn("LongAgo", self.radar())

    def test_silent_when_the_meeting_date_is_unreadable(self):
        """Rather than "you met None days ago"."""
        cid = trellis.find_or_create_person(
            self.conn, name="Garbled", email="g@x.test", origin="calendar")
        self.conn.execute("""INSERT INTO interactions (connection_id, kind,
            occurred_on, summary, source, source_ref, created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (cid, "meeting", "not-a-date", "meeting held", "calendar", "b1",
             trellis.now()))
        self.conn.commit()
        text = self.radar()
        self.assertNotIn("None days", text)

    # -- and it doesn't outrank a concrete debt ---------------------------
    def test_an_open_loop_you_owe_outranks_a_recent_meeting(self):
        self.met("JustMet", 3)
        owed = trellis.find_or_create_person(
            self.conn, name="Owed Person", email="o@x.test", origin="manual")
        self.conn.execute("""INSERT INTO open_loops (connection_id, description,
            status, source, created_at) VALUES (?,?, 'open', 'manual', ?)""",
            (owed, "send the contract", trellis.now()))
        self.conn.commit()
        text = self.radar()
        self.assertLess(text.index("Owed Person"), text.index("JustMet"),
                        "a meeting outranked something you actually owe")

    def test_a_due_follow_up_still_outranks_everything(self):
        self.met("JustMet", 3)
        dated = trellis.find_or_create_person(
            self.conn, name="Due Person", email="d@x.test", origin="manual")
        trellis.set_follow_up(self.conn, dated, self.ago(1), "you asked")
        self.conn.commit()
        text = self.radar()
        self.assertLess(text.index("Due Person"), text.index("JustMet"))


if __name__ == "__main__":
    unittest.main()

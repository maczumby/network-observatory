"""calendar_crm.ingest_records: held events become interactions, upcoming ones
become plans, and everything that isn't a real external human is skipped.

`today` is passed explicitly everywhere so the held/planned split never depends
on the wall clock.
"""

import os
import sys
import tempfile
import unittest


SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import calendar_crm
import trellis


TODAY = "2026-08-11"
PAST = "2026-07-01"
FUTURE = "2026-09-01"
LATER = "2026-09-08"

OWNER_IDS = {"owner_emails": ["mari@filament.dm"],
             "owner_domains": ["myco.test"]}


class CalendarCrmTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "data", "trellis.db")
        self.conn = trellis.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def ingest(self, *records, **kwargs):
        kwargs.setdefault("today", TODAY)
        return calendar_crm.ingest_records(self.conn, list(records), **kwargs)

    def interactions(self):
        return self.conn.execute(
            "SELECT * FROM interactions ORDER BY id").fetchall()

    def plans(self):
        return self.conn.execute(
            "SELECT * FROM calendar_plans ORDER BY id").fetchall()

    # -- held events --------------------------------------------------------

    def test_past_event_records_one_interaction_per_external_attendee(self):
        counts = self.ingest({
            "event_id": "ev-1", "date": PAST,
            "attendees": [{"name": "Ada Lovelace", "email": "ada@example.com"},
                          {"name": "Grace Hopper", "email": "grace@example.com"}],
        })
        self.assertEqual(2, counts["held"])
        rows = self.interactions()
        self.assertEqual(2, len(rows))
        for row in rows:
            self.assertEqual("meeting", row["kind"])
            self.assertEqual("meeting held", row["summary"])
            self.assertEqual("calendar", row["source"])
            self.assertEqual(PAST, row["occurred_on"])
        self.assertEqual(["ev-1:ada@example.com", "ev-1:grace@example.com"],
                         sorted(r["source_ref"] for r in rows))
        self.assertEqual(0, len(self.plans()))

    def test_re_ingesting_a_held_event_is_idempotent(self):
        record = {"event_id": "ev-1", "date": PAST,
                  "attendees": [{"name": "Ada Lovelace",
                                 "email": "ada@example.com"}]}
        first = self.ingest(record)
        second = self.ingest(record)
        self.assertEqual(1, first["held"])
        self.assertEqual(0, first["skipped_dup"])
        self.assertEqual(0, second["held"])
        self.assertEqual(1, second["skipped_dup"])
        self.assertEqual(1, len(self.interactions()))

    # -- planned events -----------------------------------------------------

    def test_future_event_creates_a_plan_row(self):
        counts = self.ingest({
            "event_id": "ev-2", "date": FUTURE,
            "attendees": [{"name": "Ada Lovelace", "email": "ada@example.com"}],
        })
        self.assertEqual(1, counts["planned"])
        self.assertEqual(0, counts["held"])
        rows = self.plans()
        self.assertEqual(1, len(rows))
        self.assertEqual(FUTURE, rows[0]["planned_on"])
        self.assertEqual("calendar", rows[0]["source"])
        self.assertEqual("ev-2:ada@example.com", rows[0]["source_ref"])
        self.assertEqual(0, len(self.interactions()))

    def test_plan_upserts_instead_of_duplicating(self):
        record = {"event_id": "ev-2", "date": FUTURE,
                  "attendees": [{"name": "Ada Lovelace",
                                 "email": "ada@example.com"}]}
        self.ingest(record)
        self.ingest(record)
        self.assertEqual(1, len(self.plans()))

    def test_plan_date_change_updates_in_place(self):
        attendees = [{"name": "Ada Lovelace", "email": "ada@example.com"}]
        self.ingest({"event_id": "ev-2", "date": FUTURE, "attendees": attendees})
        self.ingest({"event_id": "ev-2", "date": LATER, "attendees": attendees})
        rows = self.plans()
        self.assertEqual(1, len(rows))
        self.assertEqual(LATER, rows[0]["planned_on"])

    # -- exclusions ---------------------------------------------------------

    def test_owner_addresses_are_skipped_on_held_events(self):
        counts = self.ingest({
            "event_id": "ev-3", "date": PAST,
            "attendees": [{"name": "Mari Zumbro", "email": "mari@filament.dm"},
                          {"name": "Teammate Person", "email": "teammate@myco.test"},
                          {"name": "Ada Lovelace", "email": "ada@example.com"}],
        }, owner_ids=OWNER_IDS)
        self.assertEqual(2, counts["skipped_owner"])
        self.assertEqual(1, counts["held"])
        rows = self.interactions()
        self.assertEqual(["ev-3:ada@example.com"],
                         [r["source_ref"] for r in rows])

    def test_owner_addresses_are_skipped_on_future_events(self):
        counts = self.ingest({
            "event_id": "ev-4", "date": FUTURE,
            "attendees": [{"name": "Mari Zumbro", "email": "mari@filament.dm"},
                          {"name": "Teammate Person", "email": "teammate@myco.test"},
                          {"name": "Ada Lovelace", "email": "ada@example.com"}],
        }, owner_ids=OWNER_IDS)
        self.assertEqual(2, counts["skipped_owner"])
        self.assertEqual(1, counts["planned"])
        self.assertEqual(["ev-4:ada@example.com"],
                         [r["source_ref"] for r in self.plans()])

    def test_non_person_attendees_are_skipped(self):
        counts = self.ingest({
            "event_id": "ev-5", "date": PAST,
            "attendees": [
                {"name": "Conference Room A",
                 "email": "room@resource.calendar.google.com"},
                {"name": "Ada Lovelace", "email": "ada@example.com"}],
        })
        self.assertEqual(1, counts["skipped_nonperson"])
        self.assertEqual(1, counts["held"])
        names = [r["full_name"] for r in self.conn.execute(
            "SELECT full_name FROM connections")]
        self.assertNotIn("Conference Room A", names)

    def test_big_invites_are_skipped_entirely(self):
        attendees = [{"name": f"Attendee Number{i}",
                      "email": f"attendee{i}@example.com"} for i in range(11)]
        counts = self.ingest({"event_id": "ev-6", "date": PAST,
                              "attendees": attendees})
        self.assertEqual(1, counts["skipped_big"])
        self.assertEqual(0, counts["held"])
        self.assertEqual(0, len(self.interactions()))
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM connections").fetchone()[0])

    def test_ten_external_attendees_still_ingest(self):
        attendees = [{"name": f"Attendee Number{i}",
                      "email": f"attendee{i}@example.com"} for i in range(10)]
        counts = self.ingest({"event_id": "ev-7", "date": PAST,
                              "attendees": attendees})
        self.assertEqual(0, counts["skipped_big"])
        self.assertEqual(10, counts["held"])

    # -- malformed input ----------------------------------------------------

    def test_missing_event_id_or_date_is_counted_not_crashed(self):
        counts = self.ingest(
            {"date": PAST, "attendees": [{"name": "Ada Lovelace",
                                          "email": "ada@example.com"}]},
            {"event_id": "ev-8", "attendees": [{"name": "Ada Lovelace",
                                                "email": "ada@example.com"}]},
            {},
            {"event_id": "ev-9", "date": PAST, "attendees": [
                {"name": "Ada Lovelace", "email": "ada@example.com"}]},
        )
        self.assertEqual(3, counts["skipped_bad"])
        self.assertEqual(1, counts["held"])
        self.assertEqual(1, len(self.interactions()))

    def test_blank_attendees_are_ignored_without_crashing(self):
        counts = self.ingest({
            "event_id": "ev-10", "date": PAST,
            "attendees": [{}, {"name": "", "email": ""},
                          {"name": "Ada Lovelace", "email": "ada@example.com"}],
        })
        self.assertEqual(1, counts["held"])
        self.assertEqual(1, len(self.interactions()))


if __name__ == "__main__":
    unittest.main()

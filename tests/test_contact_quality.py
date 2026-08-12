"""classify_contact: person / non_person / ambiguous, from name + address alone.

These cases mirror the hygiene rules the skill pack publishes, so a drift in
either direction (a room minted as a person, a real person confidently muted)
fails here first.
"""

import os
import sys
import unittest


SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import contact_quality


# (name, email) pairs grouped by the verdict they must produce.
PERSON_CASES = [
    ("Brock Kelly", "brock.kelly@x.com"),
    ("Brock Kelly", "brockkelly@x.com"),
    ("Brock Kelly", "brock@x.com"),
    ("Jane O'Neil-Smith", "jane.oneilsmith@x.com"),
    ("Jane O'Neil-Smith", "jane@x.com"),
    ("Renée Dubois", "renee.dubois@x.co"),
]

NON_PERSON_CASES = [
    ("GitHub", "notifications@github.com"),
    ("GitHub", "no-reply@github.com"),
    ("GitHub", "noreply@github.com"),
    ("GitHub", "donotreply@github.com"),
    ("GitHub", "do-not-reply@github.com"),
    ("Push", "push@brex.com"),
    ("Transactions", "transactions@bank.test"),
    ("Subscribed", "subscribed@service.test"),
    ("Conference Room A", "room@resource.calendar.google.com"),
    ("Anything At All", "anything@calendar.google.com"),
    ("Notion Calendar", "calendar-invite@em.notion.so"),
]

AMBIGUOUS_CASES = [
    ("nico", "nico@gmail.com"),
    ("brock", "brock@corp.test"),
    ("Brock Kelly via Notion", "calendar-invite@em.notion.so"),
    ("Jane Doe via Slack", "team-digest@slack.example"),
]

ALL_CASES = PERSON_CASES + NON_PERSON_CASES + AMBIGUOUS_CASES


class ClassifyContactTest(unittest.TestCase):
    def verdict(self, name, email):
        return contact_quality.classify_contact(name, email)["verdict"]

    def test_person_shaped_names_and_matching_addresses(self):
        for name, email in PERSON_CASES:
            with self.subTest(name=name, email=email):
                self.assertEqual("person", self.verdict(name, email))

    def test_system_local_parts_are_non_person(self):
        for name, email in NON_PERSON_CASES[:5]:
            with self.subTest(email=email):
                result = contact_quality.classify_contact(name, email)
                self.assertEqual("non_person", result["verdict"])
                self.assertTrue(
                    any("system sender" in s for s in result["signals"]),
                    result["signals"])

    def test_generic_single_word_display_names_are_non_person(self):
        for name in ("Push", "Transactions", "Billing", "Reservations"):
            with self.subTest(name=name):
                result = contact_quality.classify_contact(
                    name, f"{name.lower()}@company.test")
                self.assertEqual("non_person", result["verdict"])

    def test_google_calendar_resources_are_non_person(self):
        for email in ("room@resource.calendar.google.com",
                      "c_1234@resource.calendar.google.com",
                      "anything@calendar.google.com"):
            with self.subTest(email=email):
                result = contact_quality.classify_contact("Big Room", email)
                self.assertEqual("non_person", result["verdict"])
                self.assertTrue(
                    any("calendar resource" in s for s in result["signals"]),
                    result["signals"])

    def test_machine_domain_without_via_is_non_person(self):
        result = contact_quality.classify_contact(
            "Notion Calendar", "calendar-invite@em.notion.so")
        self.assertEqual("non_person", result["verdict"])
        self.assertTrue(any("machine sending domain" in s
                            for s in result["signals"]), result["signals"])

    def test_bare_first_name_matching_local_part_is_ambiguous(self):
        result = contact_quality.classify_contact("nico", "nico@gmail.com")
        self.assertEqual("ambiguous", result["verdict"])

    def test_via_name_is_never_confidently_non_person(self):
        # A relay address can still carry a real person's mail — that's a
        # question for the user, even when the sending domain is machinery.
        for name, email in (("Brock Kelly via Notion", "calendar-invite@em.notion.so"),
                            ("Jane Doe via Slack", "team-digest@slack.example")):
            with self.subTest(name=name):
                result = contact_quality.classify_contact(name, email)
                self.assertEqual("ambiguous", result["verdict"])
                self.assertTrue(any("'via' display name" in s
                                    for s in result["signals"]),
                                result["signals"])

    def test_ambiguous_cases(self):
        for name, email in AMBIGUOUS_CASES:
            with self.subTest(name=name, email=email):
                self.assertEqual("ambiguous", self.verdict(name, email))

    def test_every_result_carries_a_verdict_and_evidence(self):
        for name, email in ALL_CASES + [("", ""), (None, None)]:
            with self.subTest(name=name, email=email):
                result = contact_quality.classify_contact(name, email)
                self.assertIn(result["verdict"],
                              ("person", "non_person", "ambiguous"))
                self.assertTrue(result["signals"])
                self.assertTrue(all(s.strip() for s in result["signals"]))


if __name__ == "__main__":
    unittest.main()

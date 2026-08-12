#!/usr/bin/env python3
"""
contact_quality.py — is this contact a person, or plumbing?

Email and calendar sweeps mint contacts from addresses, and addresses lie:
"Brex", "Transactions", no-reply@github.com. This module encodes the same
hygiene rules the skill pack teaches agents (skills/network-observatory/
SKILL.md, "Hygiene first"), as one deterministic, testable function — so the
published rules and the code can't drift apart.

classify_contact(name, email) -> {"verdict": "person"|"non_person"|"ambiguous",
                                  "signals": [...]}

It never touches the DB. reconcile.py uses it to build the review queue;
nothing is muted without either a confident verdict or the user's confirmation.

Standard library only.
"""

import argparse
import json
import re
import sys

# Address local parts that are systems, not people (from the published skill).
SYSTEM_LOCAL_PARTS = {
    "no-reply", "noreply", "donotreply", "do-not-reply", "notifications",
    "notification", "notify", "updates", "update", "newsletter", "newsletters",
    "digest", "alerts", "alert", "billing", "invoice", "invoices", "receipts",
    "receipt", "info", "admin", "hello", "team", "support", "mailer-daemon",
    "postmaster", "marketing", "sales", "careers", "jobs", "hr", "security",
    "legal", "help", "contact", "service", "accounts", "account", "payroll",
    "orders", "order", "bounce", "bounces", "feedback", "news", "press",
    "community", "events", "welcome", "onboarding", "subscriptions",
    "subscription", "reply", "mail", "email", "automated", "robot", "bot",
}

# Subdomain labels that mark a sending domain as machinery even when the local
# part looks personal (calendar-invite@em.notion.so and friends). These are
# matched ONLY against subdomain labels, never the registrable domain itself:
# "mail" as a subdomain (mail.notion.so) is a sending host, but as the domain
# itself (mail.ru, mail.com) it's one of the largest consumer mail providers in
# the world, and its users are people.
SYSTEM_DOMAIN_LABELS = {
    "noreply", "no-reply", "donotreply", "notifications", "notification",
    "mailer", "mail", "email", "em", "bounce", "bounces", "mta", "smtp",
    "marketing", "newsletters", "updates", "alerts",
}

# Display names that are a single generic word — a product surface, not a person.
GENERIC_NAMES = {
    "push", "subscribed", "finance", "transactions", "billing", "payments",
    "payment", "receipts", "team", "support", "info", "news", "hello", "admin",
    "notifications", "updates", "alerts", "newsletter", "digest", "noreply",
    "no-reply", "marketing", "sales", "careers", "jobs", "hr", "security",
    "legal", "help", "contact", "service", "accounts", "payroll", "invoices",
    "invoice", "orders", "community", "events", "welcome", "reservations",
    "bookings", "recruiting", "membership", "concierge",
}

_NAMEISH = re.compile(r"^[^\W\d_]+([-'’ .][^\W\d_]+)*$", re.UNICODE)


def _local_and_domain(email):
    e = (email or "").strip().lower()
    if "@" not in e:
        return e, ""
    local, domain = e.rsplit("@", 1)
    return local, domain


def _norm_token(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


def classify_contact(name, email):
    """Deterministic person/non_person/ambiguous verdict with the evidence.

    Confident non_person verdicts are safe to act on (reversibly). Everything
    that merely smells off is 'ambiguous' — the review queue asks the user,
    it never guesses."""
    name = (name or "").strip()
    local, domain = _local_and_domain(email)
    signals = []

    local_key = re.sub(r"[^a-z-]", "", local)
    if local_key in SYSTEM_LOCAL_PARTS or _norm_token(local) in {
            _norm_token(x) for x in SYSTEM_LOCAL_PARTS}:
        signals.append(f"system sender local part '{local}@…'")
    if domain:
        if domain.endswith("calendar.google.com"):
            signals.append(f"calendar resource address ({domain}) — a room, not a person")
        # Subdomains only: everything left of the registrable domain. A bare
        # two-label domain has no subdomain and so can never match here.
        subdomains = set(domain.split(".")[:-2])
        if subdomains & SYSTEM_DOMAIN_LABELS:
            signals.append(f"machine sending domain ({domain})")
    if re.search(r"\bvia\b", name.lower()):
        signals.append(f"'via' display name ({name!r}) — a relay, not the person's own address")
    lowered = name.lower().strip(".,'\"")
    if lowered in GENERIC_NAMES:
        signals.append(f"generic single-word name {name!r}")

    if signals:
        # A 'via' display name means the PERSON may be real behind a relay
        # address — that's a question for the user, not a confident verdict,
        # even when the sending domain is machinery.
        via = any("'via' display name" in s for s in signals)
        confident = any("system sender" in s or "generic single-word" in s
                        or "calendar resource" in s for s in signals) \
            or (not via and any("machine sending" in s for s in signals))
        return {"verdict": "non_person" if confident else "ambiguous",
                "signals": signals}

    # Positive person evidence.
    tokens = name.split()
    person_signals = []
    if len(tokens) >= 2 and all(_NAMEISH.match(t) for t in tokens):
        person_signals.append("multi-word person-shaped name")
    first = _norm_token(tokens[0]) if tokens else ""
    last = _norm_token(tokens[-1]) if len(tokens) > 1 else ""
    local_norm = _norm_token(local)  # strips dots/digits: "mari.z87" -> "mariz"
    if local_norm and first:
        forms = {first, first + last, last + first}
        if last:
            forms |= {last, first + last[:1], first[:1] + last}
        if local_norm in forms:
            person_signals.append(f"address '{local}@…' matches their name")
    if len(person_signals) >= 1 and len(tokens) >= 2:
        return {"verdict": "person", "signals": person_signals}

    return {"verdict": "ambiguous",
            "signals": person_signals or
            [f"can't tell from name {name!r} and address alone"]}


def main():
    ap = argparse.ArgumentParser(
        description="Classify a contact (or stdin JSON list) as person / non_person / ambiguous.")
    ap.add_argument("--name", default="")
    ap.add_argument("--email", default="")
    ap.add_argument("--json", action="store_true",
                    help="read a JSON array of {name,email} from stdin and classify each")
    a = ap.parse_args()
    if a.json:
        items = json.load(sys.stdin)
        out = [dict(item, **classify_contact(item.get("name"), item.get("email")))
               for item in items]
        print(json.dumps(out, indent=2))
        return
    if not a.name and not a.email:
        ap.error("pass --name/--email, or --json with a list on stdin")
    print(json.dumps(classify_contact(a.name, a.email), indent=2))


if __name__ == "__main__":
    main()

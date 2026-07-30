#!/usr/bin/env python3
"""Create one one-time Network Observatory onboarding invite."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description="Create a one-time tester invite.")
    parser.add_argument("--url", required=True, help="deployed onboarding base URL")
    parser.add_argument("--label", required=True, help="who this invite is for")
    parser.add_argument("--email", help="optional exact Google email restriction")
    parser.add_argument("--hours", type=int, default=168, help="expiry, default 168")
    args = parser.parse_args()

    token = os.environ.get("NETWORK_OBSERVATORY_INVITE_ADMIN_TOKEN")
    if not token:
        raise SystemExit(
            "Set NETWORK_OBSERVATORY_INVITE_ADMIN_TOKEN in your shell first."
        )

    payload = json.dumps(
        {
            "label": args.label,
            "email": args.email,
            "expiresInHours": args.hours,
        }
    ).encode()
    request = urllib.request.Request(
        args.url.rstrip("/") + "/api/admin/invites",
        data=payload,
        method="POST",
        headers={
            "authorization": "Bearer " + token,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(response.read().decode())
    except urllib.error.HTTPError as error:
        print(error.read().decode(), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

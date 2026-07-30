#!/usr/bin/env python3
"""List or revoke Network Observatory onboarding access."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def request(url, token, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    call = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "authorization": "Bearer " + token,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(call, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        print(error.read().decode(), file=sys.stderr)
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description="List or revoke tester access.")
    parser.add_argument("--url", required=True, help="deployed onboarding base URL")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list invitations and access state")
    revoke = subparsers.add_parser("revoke", help="revoke one Composio session")
    revoke.add_argument("session_id")
    args = parser.parse_args()

    token = os.environ.get("NETWORK_OBSERVATORY_INVITE_ADMIN_TOKEN")
    if not token:
        raise SystemExit(
            "Set NETWORK_OBSERVATORY_INVITE_ADMIN_TOKEN in your shell first."
        )

    endpoint = args.url.rstrip("/") + "/api/admin/access"
    if args.command == "list":
        result = request(endpoint, token)
    else:
        result = request(
            endpoint,
            token,
            method="DELETE",
            payload={"sessionId": args.session_id},
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

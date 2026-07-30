# Composio onboarding operations

The public repository never contains a Composio key, Google client secret, or
shared Google token. The hosted Connect service keeps the project credential on
the server and gives each tester a separate, revocable endpoint.

## Runtime configuration

Set these four values on the hosted `onboarding/` site:

- `COMPOSIO_API_KEY`: a Composio project API key. Keep it server-side.
- `COMPOSIO_GMAIL_AUTH_CONFIG_ID`: the custom Gmail auth config ID beginning
  with `ac_`.
- `INVITE_ADMIN_TOKEN`: a random 32-byte or longer operator secret.
- `IDENTITY_PEPPER`: a different random 32-byte or longer secret used to
  pseudonymize email addresses and IP rate-limit keys.

Never commit an `.env` file. The example file contains names only.

The D1 database uses the migrations in `onboarding/drizzle/`. It stores hashed
invite codes, hashed MCP bearer tokens, pseudonymous user IDs, Composio session
IDs, expiry timestamps, and rate-limit counters. It does not store plain email
addresses or Google tokens.

## Google and Composio setup

In the Google Cloud project:

1. Enable the Gmail API.
2. Configure the OAuth consent screen for External users.
3. Keep the app in Testing while the verification work is pending.
4. Add only `https://www.googleapis.com/auth/gmail.metadata`.
5. Add each invited Google account as a test user.
6. Use the redirect URI Composio displays for the custom Gmail auth config.

In Composio:

1. Keep the custom Gmail auth config on the same project as the API key.
2. Confirm its ID is the value in `COMPOSIO_GMAIL_AUTH_CONFIG_ID`.
3. Confirm it uses the Google client ID and client secret from this project.
4. Confirm the requested scope is exactly `gmail.metadata`.

Google Testing authorizations can expire after seven days. The tester keeps the
same Observatory MCP URL. When a metadata call fails, the endpoint returns a
fresh `reconnectUrl`; the tester completes that link and retries.

## Invite a tester

On the operator Mac, the production token is stored in Keychain as
`network-observatory-invite-admin`. Load it into the current shell without
placing the value in command history:

```bash
export NETWORK_OBSERVATORY_INVITE_ADMIN_TOKEN="$(
  security find-generic-password \
    -s network-observatory-invite-admin -w
)"
export NETWORK_OBSERVATORY_CONNECT_URL='https://YOUR-PUBLIC-CONNECT-HOST'
python3 onboarding/scripts/create_invite.py \
  --url "$NETWORK_OBSERVATORY_CONNECT_URL" \
  --label 'Tester name' \
  --email 'their-google-address@example.com'
```

The response contains a one-time `netobs_...` code. Send the Connect site and
code privately. The code is shown only once and is bound to that exact email
when `--email` is used.

The tester:

1. Opens the Connect site.
2. Enters the email and invite.
3. Approves Google on the unverified test screen.
4. Runs the private `hermes mcp add` command shown by the site.
5. Runs `hermes mcp test network-observatory-gmail`.

The resulting MCP URL is a bearer credential. Send it only to the tester and
never post it in a shared channel.

## Review and revoke access

```bash
python3 onboarding/scripts/manage_access.py \
  --url "$NETWORK_OBSERVATORY_CONNECT_URL" list

python3 onboarding/scripts/manage_access.py \
  --url "$NETWORK_OBSERVATORY_CONNECT_URL" revoke 'trs_SESSION_ID'
```

Revocation disables the hashed Observatory MCP token and deletes the associated
Composio session. The tester's Google permission can also be revoked from their
Google Account permissions page.

## What the tester endpoint can do

The public MCP proxy implements two read-only tools:

- Sweep up to 25 recent message IDs and return only From, To, Cc, Bcc, Date,
  labels, internal date, and stable Gmail IDs.
- Fetch the same metadata for one known message ID.

It constructs fixed Gmail `GET` requests with `format=metadata`. It does not
accept Gmail `q`, arbitrary URLs, methods, headers, or request bodies. It strips
subject, snippet, body, attachments, and all unapproved response fields before
returning data to the agent.

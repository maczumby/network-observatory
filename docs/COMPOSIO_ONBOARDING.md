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

## Onboard a tester

**There are no invite codes.** The Connect page asks for an email address and
nothing else. The only gate is Google's tester list, and adding someone to it
is a manual step in the Google Cloud console that nothing here can do for you:

> Google Cloud console → **APIs & Services → OAuth consent screen → Audience →
> Test users → Add users** → their exact Gmail address.

**Do this before they try.** Nothing checks the list — not the Connect page,
not `/api/health`, not any script. Provisioning succeeds either way: someone
who isn't on the list still sees a green "Ready", still gets a real setup
command and a real 180-day token, and only discovers the problem when Google
refuses them. Every tool call after that returns the same
`Gmail metadata access is unavailable.` regardless of cause.

The tester's path, **in the page's order** — the setup command comes before
Google, and is shown only once:

1. Opens the Connect site and enters their Gmail address.
2. Copies the **setup block** the page shows (it begins `Run this: hermes mcp
   add …`) and pastes it to their agent. It contains a private URL that works
   like a password. If you are screensharing a demo, pause the share here.
3. Their agent runs the `hermes mcp add …` command from that block.
4. Approves Google on the unverified test screen.
5. **Starts a new chat** — the tools only exist in the next session — and runs
   `hermes mcp test network-observatory-gmail` there.

Google test authorizations expire after about seven days. Nothing warns them
in advance; their agent gets a `reconnectUrl` on the next failed call, which
they click to re-approve. The Observatory MCP URL itself does not change.

Older versions of this flow used one-time `netobs_…` invite codes minted by
`onboarding/scripts/create_invite.py`. The server still accepts a code if one
is supplied, but the shipped page has no field for it and none is needed —
ignore that script.

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

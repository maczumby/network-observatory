# Connect service threat model

## Assets and boundaries

Protected assets are the Composio project API key, Google OAuth client secret,
tester Google tokens, tester email addresses, private MCP bearer URLs, LinkedIn
exports, and local Trellis databases.

The public repository and public Connect page are untrusted surfaces. The
Connect worker and its D1 database are the application boundary. Composio and
Google are external processors. Each tester's Hermes instance is a separate
trust boundary.

## Main threats and controls

| STRIDE area | Threat | Control |
| --- | --- | --- |
| Spoofing | Someone redeems another tester's invite | One-time random invites can be HMAC-bound to an exact normalized email |
| Spoofing | Someone guesses a tester endpoint | MCP URLs contain 256-bit random bearer material and only hashes are stored |
| Tampering | A client asks Composio to send or delete email | The Observatory MCP server exposes two fixed read tools and never forwards arbitrary tool names |
| Tampering | A client injects a Gmail URL, method, or query | Server code constructs fixed `GET` endpoints and does not accept `q` or arbitrary proxy parameters |
| Repudiation | Access cannot be tied to an invitation | D1 records invite label, timestamps, pseudonymous user ID, and Composio session ID |
| Information disclosure | Project key reaches a tester or GitHub | The key exists only as a hosted runtime secret; the Composio MCP URL and headers are never returned |
| Information disclosure | Gmail content leaks through a broad tool response | Gmail uses `gmail.metadata`; the server requests `format=metadata` and allowlists response fields |
| Information disclosure | Plain tester email is retained | Email and IP values are HMAC-pseudonymized before storage |
| Denial of service | Invite guessing or MCP flooding consumes quota | Per-IP provisioning limits, per-token MCP limits, invite expiry, payload limits, and batch limits |
| Elevation of privilege | Agent discovers Composio write tools or workbench | Search, workbench, and multi-execute are disabled; the tester never reaches Composio directly |

## Residual risks

- Anyone who receives a private MCP URL can use that tester's metadata endpoint
  until it expires or is revoked. Treat the URL as a password.
- The operator's Composio project can execute allowed actions across project
  users. Protect the runtime key, use a scoped project key if practical, and
  rotate it after suspected exposure.
- Composio and Google retain OAuth and execution records under their own
  policies.
- Google Testing access may expire after seven days. Reauthentication is
  expected and does not require issuing a new Observatory endpoint.
- D1 records pseudonymous identifiers and session IDs. They are less sensitive
  than plain email but still require normal production access controls.
- The Sites scaffold currently resolves nested PostCSS and Sharp versions that
  npm flags through Next. The deployed app does not process user CSS or images
  and does not use Next image optimization. Recheck and remove this exception
  when stable patched transitive releases become available.

## Revocation and recovery

The operator can revoke the hashed endpoint token and delete its Composio
session by session ID. A tester can separately revoke the Google grant. Confirmed
Trellis identity merges are journaled and can be undone with `trellis.py
unmerge`; connector revocation does not rewrite local relationship history.

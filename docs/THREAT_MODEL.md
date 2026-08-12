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

## The dashboard write API (`serve.py --rw`)

By default `serve.py` only reads: the sole POST it accepts is `/login`, and the
screens fall back to a copy-paste sync block. `--rw` adds two write endpoints
(`/api/person`, `/api/merge`) so the screens can set priority, set a follow-up
date, add a note, or confirm an identity merge.

Boundary: this is a local tool serving a local SQLite file. The write API's job
is to make sure only the person who already holds the viewing password, in a
real browser session, on this origin, can change that file.

| Threat | Control |
|---|---|
| Open server + write API | `--rw` refuses to start without a saved password. |
| Unauthenticated writes | Every write requires the signed HttpOnly session cookie; no cookie, `401`. |
| Cross-site request forgery | `SameSite=Lax` is **not** sufficient on localhost, because ports don't factor into "site" — so writes additionally require `Content-Type: application/json` (which an HTML form cannot send cross-origin without a CORS preflight that is never granted), and an `Origin` header, when present, must match `Host`. |
| Cross-origin reads of responses | No CORS headers are ever emitted. |
| DNS rebinding | A rebound origin has a different cookie jar, so it holds no session; combined with the password requirement above, writes fail closed. |
| Resource exhaustion | Bodies cap at 16 KB; writes are rate-limited per client IP. |
| Shared link, unwanted edits | `--rw-local-only` accepts writes from loopback only, so viewers with the password can look but not change. |
| A bad write | Priority and follow-ups are plain reversible values; merges go through the journal and `unmerge` restores them. |

**Residual risk, stated plainly:** with `--rw` and without `--rw-local-only`,
the viewing password is also a writing password. Anyone you share the link and
password with can mark, snooze, and merge people in your graph. Share a
read-only link (the default) unless you specifically want otherwise.

## Revocation and recovery

The operator can revoke the hashed endpoint token and delete its Composio
session by session ID. A tester can separately revoke the Google grant. Confirmed
Trellis identity merges are journaled and can be undone with `trellis.py
unmerge`; connector revocation does not rewrite local relationship history.

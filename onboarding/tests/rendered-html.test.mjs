import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("ships the public onboarding page and privacy promises", async () => {
  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(layout, /Connect Gmail \| Network Observatory/);
  assert.match(page, /Connect Gmail without handing over your inbox/);
  assert.match(page, /LinkedIn remains the source of truth/);
  assert.match(page, /Identity decisions stay reversible/);
  assert.match(page, /View the public source/);
  assert.doesNotMatch(page, /SkeletonPreview|react-loading-skeleton/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
});

test("health endpoint is public but does not disclose secret values", async () => {
  const health = await readFile(
    new URL("../app/api/health/route.ts", import.meta.url),
    "utf8",
  );

  assert.match(health, /mode:\s*"invite-only"/);
  assert.match(
    health,
    /gmailScope:\s*"https:\/\/www\.googleapis\.com\/auth\/gmail\.metadata"/,
  );
  assert.match(health, /COMPOSIO_API_KEY/);
  assert.match(health, /COMPOSIO_GMAIL_AUTH_CONFIG_ID/);
  assert.doesNotMatch(health, /apiKey:\s*|adminToken:\s*|pepper:\s*/i);
});

test("source constrains Composio to the two Gmail read tools", async () => {
  const [composio, provision] = await Promise.all([
    readFile(new URL("../lib/composio.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/provision/route.ts", import.meta.url), "utf8"),
  ]);

  assert.match(composio, /GMAIL_FETCH_EMAILS/);
  assert.match(composio, /GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID/);
  assert.match(composio, /workbench:\s*\{\s*enable:\s*false/);
  assert.match(composio, /search:\s*\{\s*enable:\s*false/);
  assert.doesNotMatch(composio, /SEND_EMAIL|CREATE_DRAFT|DELETE_MESSAGE/);
  assert.match(provision, /"cache-control":\s*"no-store"/);
  assert.match(provision, /hmacSha256\(runtime\.IDENTITY_PEPPER, email\)/);
});

import { createGmailSession, deleteSession } from "@/lib/composio";
import {
  hmacSha256,
  isEmail,
  normalizeEmail,
  randomToken,
  sha256,
} from "@/lib/crypto";
import {
  checkRateLimit,
  completeInvite,
  createMcpToken,
  getInvite,
  releaseInvite,
  reserveInvite,
} from "@/lib/database";
import { requireRuntimeConfig } from "@/lib/runtime";

export const dynamic = "force-dynamic";

function response(body: Record<string, unknown>, status: number, retryAfter?: number) {
  const headers: Record<string, string> = {
    "cache-control": "no-store",
  };
  if (retryAfter) headers["retry-after"] = String(retryAfter);
  return Response.json(body, { status, headers });
}

export async function POST(request: Request) {
  const runtime = requireRuntimeConfig();
  const ip = request.headers.get("cf-connecting-ip") || "local";
  const ipKey = await hmacSha256(runtime.IDENTITY_PEPPER, `ip:${ip}`);
  const rate = await checkRateLimit(ipKey);
  if (!rate.allowed) {
    return response(
      { error: "Too many attempts. Wait a few minutes and try again." },
      429,
      rate.retryAfter,
    );
  }

  const body = (await request.json().catch(() => null)) as
    | { inviteCode?: string; email?: string }
    | null;
  const inviteCode = body?.inviteCode?.trim() || "";
  const email = normalizeEmail(body?.email || "");
  if (!isEmail(email)) {
    return response({ error: "Enter a valid email." }, 400);
  }
  // Invite codes are optional. The real gate is Google's tester list: anyone
  // not on it fails at the consent screen. A code, when supplied, still binds
  // to its intended email and burns on use.
  const usingInvite = inviteCode.length > 0;
  if (usingInvite && !inviteCode.startsWith("netobs_")) {
    return response({ error: "That invite code doesn't look right." }, 400);
  }

  const emailHash = await hmacSha256(runtime.IDENTITY_PEPPER, email);
  const userId = `netobs_${emailHash.slice(0, 32)}`;
  const tokenHash = usingInvite ? await sha256(inviteCode) : null;

  if (usingInvite && tokenHash) {
    const invite = await getInvite(tokenHash);
    if (
      !invite ||
      invite.redeemed_at ||
      invite.expires_at <= new Date().toISOString() ||
      (invite.intended_email_hash && invite.intended_email_hash !== emailHash)
    ) {
      return response({ error: "This invite is invalid, expired, or already used." }, 403);
    }
    if (!(await reserveInvite(tokenHash, emailHash, userId))) {
      return response({ error: "This invite is already being used." }, 409);
    }
  }

  try {
    const origin = new URL(request.url).origin;
    const session = await createGmailSession(
      runtime.COMPOSIO_API_KEY,
      runtime.COMPOSIO_GMAIL_AUTH_CONFIG_ID,
      userId,
      `${origin}/connected`,
    );
    const mcpToken = `nobs_${randomToken(32)}`;
    const mcpTokenHash = await sha256(mcpToken);
    const mcpExpiresAt = new Date(
      Date.now() + 180 * 24 * 60 * 60_000,
    ).toISOString();
    try {
      await createMcpToken(mcpTokenHash, session.sessionId, mcpExpiresAt);
      if (usingInvite && tokenHash) {
        await completeInvite(tokenHash, session.sessionId);
      }
    } catch (error) {
      await deleteSession(runtime.COMPOSIO_API_KEY, session.sessionId);
      throw error;
    }

    const mcpUrl = `${origin}/api/mcp/${mcpToken}`;
    // The paste block is a message TO the agent, not just a command: agents
    // that only get `mcp add` forget the capability exists by the next
    // session and try to open their own broad-scope Gmail connection.
    const hermesCommand = [
      `Run this: hermes mcp add network-observatory-gmail --url "${mcpUrl}"`,
      `Then: hermes mcp test network-observatory-gmail`,
      `Then save this to your permanent memory or notes: I have Gmail metadata`,
      `search via the network-observatory-gmail MCP. Use it for ANY question`,
      `about my email history, contact recency, or who I've talked to. Never`,
      `connect Gmail any other way — this endpoint is deliberately`,
      `metadata-only (no message content) for my security.`,
    ].join("\n");
    return response(
      {
        connectUrl: session.connectUrl,
        mcpUrl,
        hermesCommand,
        hermesConfig: null,
        note:
          "Paste the whole block to your agent — the last part teaches it to remember this connection instead of creating a new one later.",
      },
      201,
    );
  } catch (error) {
    if (usingInvite && tokenHash) {
      await releaseInvite(tokenHash);
    }
    console.error("Composio provisioning failed", {
      message: error instanceof Error ? error.message : "unknown error",
    });
    return response(
      { error: "We could not create your private Gmail connection. Try again shortly." },
      502,
    );
  }
}

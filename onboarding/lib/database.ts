import { runtimeEnv } from "./runtime";

export interface InviteRecord {
  token_hash: string;
  label: string;
  intended_email_hash: string | null;
  created_at: string;
  expires_at: string;
  redeemed_at: string | null;
  user_id: string | null;
  session_id: string | null;
}

export interface McpTokenRecord {
  token_hash: string;
  session_id: string;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
}

export async function getInvite(tokenHash: string) {
  return runtimeEnv().DB.prepare(
    `SELECT token_hash, label, intended_email_hash, created_at, expires_at,
      redeemed_at, user_id, session_id
     FROM invites WHERE token_hash = ?`,
  )
    .bind(tokenHash)
    .first<InviteRecord>();
}

export async function reserveInvite(
  tokenHash: string,
  emailHash: string,
  userId: string,
) {
  const result = await runtimeEnv().DB.prepare(
    `UPDATE invites
     SET redeemed_at = 'pending', intended_email_hash = COALESCE(intended_email_hash, ?),
         user_id = ?
     WHERE token_hash = ? AND redeemed_at IS NULL AND expires_at > ?`,
  )
    .bind(emailHash, userId, tokenHash, new Date().toISOString())
    .run();
  return Number(result.meta.changes ?? 0) === 1;
}

export async function completeInvite(tokenHash: string, sessionId: string) {
  await runtimeEnv().DB.prepare(
    `UPDATE invites SET redeemed_at = ?, session_id = ?
     WHERE token_hash = ? AND redeemed_at = 'pending'`,
  )
    .bind(new Date().toISOString(), sessionId, tokenHash)
    .run();
}

export async function releaseInvite(tokenHash: string) {
  await runtimeEnv().DB.prepare(
    `UPDATE invites SET redeemed_at = NULL, user_id = NULL
     WHERE token_hash = ? AND redeemed_at = 'pending'`,
  )
    .bind(tokenHash)
    .run();
}

export async function recordOpenProvision(
  tokenHash: string,
  label: string,
  emailHash: string,
  userId: string,
  sessionId: string,
) {
  // Open-door (no-invite) provisions get a synthetic, already-redeemed
  // invites row so listAccess and revocation can see them. Without this,
  // self-serve users are invisible to the admin API.
  const now = new Date().toISOString();
  await runtimeEnv().DB.prepare(
    `INSERT INTO invites
      (token_hash, label, intended_email_hash, created_at, expires_at,
       redeemed_at, user_id, session_id)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  )
    .bind(tokenHash, label, emailHash, now, now, now, userId, sessionId)
    .run();
}

export async function createMcpToken(
  tokenHash: string,
  sessionId: string,
  expiresAt: string,
) {
  await runtimeEnv().DB.prepare(
    `INSERT INTO mcp_tokens
      (token_hash, session_id, created_at, expires_at)
     VALUES (?, ?, ?, ?)`,
  )
    .bind(tokenHash, sessionId, new Date().toISOString(), expiresAt)
    .run();
}

export async function getMcpToken(tokenHash: string) {
  return runtimeEnv().DB.prepare(
    `SELECT token_hash, session_id, created_at, expires_at, revoked_at
     FROM mcp_tokens
     WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?`,
  )
    .bind(tokenHash, new Date().toISOString())
    .first<McpTokenRecord>();
}

export async function revokeMcpToken(sessionId: string) {
  const result = await runtimeEnv().DB.prepare(
    `UPDATE mcp_tokens SET revoked_at = ?
     WHERE session_id = ? AND revoked_at IS NULL`,
  )
    .bind(new Date().toISOString(), sessionId)
    .run();
  return Number(result.meta.changes ?? 0) === 1;
}

export async function listAccess() {
  return runtimeEnv().DB.prepare(
    `SELECT i.label, i.created_at, i.expires_at AS invite_expires_at,
      i.redeemed_at, i.session_id, t.created_at AS access_created_at,
      t.expires_at AS access_expires_at, t.revoked_at
     FROM invites i
     LEFT JOIN mcp_tokens t ON t.session_id = i.session_id
     ORDER BY i.created_at DESC
     LIMIT 100`,
  ).all<{
    label: string;
    created_at: string;
    invite_expires_at: string;
    redeemed_at: string | null;
    session_id: string | null;
    access_created_at: string | null;
    access_expires_at: string | null;
    revoked_at: string | null;
  }>();
}

export async function checkRateLimit(key: string, limit = 8, windowMs = 15 * 60_000) {
  const now = Date.now();
  const existing = await runtimeEnv().DB.prepare(
    "SELECT window_started_at, attempts FROM rate_limits WHERE key = ?",
  )
    .bind(key)
    .first<{ window_started_at: number; attempts: number }>();

  if (!existing || now - existing.window_started_at >= windowMs) {
    await runtimeEnv().DB.prepare(
      `INSERT INTO rate_limits (key, window_started_at, attempts) VALUES (?, ?, 1)
       ON CONFLICT(key) DO UPDATE SET window_started_at = excluded.window_started_at,
       attempts = 1`,
    )
      .bind(key, now)
      .run();
    return { allowed: true, retryAfter: 0 };
  }

  if (existing.attempts >= limit) {
    return {
      allowed: false,
      retryAfter: Math.max(1, Math.ceil((windowMs - (now - existing.window_started_at)) / 1000)),
    };
  }

  await runtimeEnv().DB.prepare(
    "UPDATE rate_limits SET attempts = attempts + 1 WHERE key = ?",
  )
    .bind(key)
    .run();
  return { allowed: true, retryAfter: 0 };
}

import { index, integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const invites = sqliteTable(
  "invites",
  {
    tokenHash: text("token_hash").primaryKey(),
    label: text("label").notNull(),
    intendedEmailHash: text("intended_email_hash"),
    createdAt: text("created_at").notNull(),
    expiresAt: text("expires_at").notNull(),
    redeemedAt: text("redeemed_at"),
    userId: text("user_id"),
    sessionId: text("session_id"),
  },
  (table) => [
    index("invites_expires_at_idx").on(table.expiresAt),
    index("invites_session_id_idx").on(table.sessionId),
  ],
);

export const rateLimits = sqliteTable(
  "rate_limits",
  {
    key: text("key").primaryKey(),
    windowStartedAt: integer("window_started_at").notNull(),
    attempts: integer("attempts").notNull().default(0),
  },
  (table) => [index("rate_limits_window_idx").on(table.windowStartedAt)],
);

export const mcpTokens = sqliteTable(
  "mcp_tokens",
  {
    tokenHash: text("token_hash").primaryKey(),
    sessionId: text("session_id").notNull().unique(),
    createdAt: text("created_at").notNull(),
    expiresAt: text("expires_at").notNull(),
    revokedAt: text("revoked_at"),
  },
  (table) => [
    index("mcp_tokens_session_id_idx").on(table.sessionId),
    index("mcp_tokens_expires_at_idx").on(table.expiresAt),
  ],
);

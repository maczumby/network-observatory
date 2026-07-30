CREATE TABLE `mcp_tokens` (
	`token_hash` text PRIMARY KEY NOT NULL,
	`session_id` text NOT NULL,
	`created_at` text NOT NULL,
	`expires_at` text NOT NULL,
	`revoked_at` text
);
--> statement-breakpoint
CREATE UNIQUE INDEX `mcp_tokens_session_id_unique` ON `mcp_tokens` (`session_id`);--> statement-breakpoint
CREATE INDEX `mcp_tokens_session_id_idx` ON `mcp_tokens` (`session_id`);--> statement-breakpoint
CREATE INDEX `mcp_tokens_expires_at_idx` ON `mcp_tokens` (`expires_at`);
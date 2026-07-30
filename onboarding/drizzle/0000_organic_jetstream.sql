CREATE TABLE `invites` (
	`token_hash` text PRIMARY KEY NOT NULL,
	`label` text NOT NULL,
	`intended_email_hash` text,
	`created_at` text NOT NULL,
	`expires_at` text NOT NULL,
	`redeemed_at` text,
	`user_id` text,
	`session_id` text
);
--> statement-breakpoint
CREATE INDEX `invites_expires_at_idx` ON `invites` (`expires_at`);--> statement-breakpoint
CREATE INDEX `invites_session_id_idx` ON `invites` (`session_id`);--> statement-breakpoint
CREATE TABLE `rate_limits` (
	`key` text PRIMARY KEY NOT NULL,
	`window_started_at` integer NOT NULL,
	`attempts` integer DEFAULT 0 NOT NULL
);
--> statement-breakpoint
CREATE INDEX `rate_limits_window_idx` ON `rate_limits` (`window_started_at`);
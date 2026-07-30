import { env } from "cloudflare:workers";

export interface RuntimeEnv {
  DB: D1Database;
  COMPOSIO_API_KEY?: string;
  COMPOSIO_GMAIL_AUTH_CONFIG_ID?: string;
  INVITE_ADMIN_TOKEN?: string;
  IDENTITY_PEPPER?: string;
}

export function runtimeEnv(): RuntimeEnv {
  return env as unknown as RuntimeEnv;
}

export function requireRuntimeConfig() {
  const runtime = runtimeEnv();
  const missing = [
    "COMPOSIO_API_KEY",
    "COMPOSIO_GMAIL_AUTH_CONFIG_ID",
    "INVITE_ADMIN_TOKEN",
    "IDENTITY_PEPPER",
  ].filter((key) => !runtime[key as keyof RuntimeEnv]);

  if (missing.length) {
    throw new Error(`Missing runtime configuration: ${missing.join(", ")}`);
  }

  return runtime as Required<RuntimeEnv>;
}

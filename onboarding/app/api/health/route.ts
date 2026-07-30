import { runtimeEnv } from "@/lib/runtime";

export const dynamic = "force-dynamic";

export async function GET() {
  const runtime = runtimeEnv();
  const configured = Boolean(
    runtime.COMPOSIO_API_KEY &&
      runtime.COMPOSIO_GMAIL_AUTH_CONFIG_ID &&
      runtime.INVITE_ADMIN_TOKEN &&
      runtime.IDENTITY_PEPPER,
  );

  return Response.json(
    {
      ok: true,
      configured,
      gmailScope: "https://www.googleapis.com/auth/gmail.metadata",
      mode: "invite-only",
    },
    {
      headers: {
        "cache-control": "no-store",
      },
    },
  );
}

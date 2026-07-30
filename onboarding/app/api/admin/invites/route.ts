import { hmacSha256, isEmail, normalizeEmail, randomToken, secretMatches, sha256 } from "@/lib/crypto";
import { requireRuntimeConfig } from "@/lib/runtime";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const runtime = requireRuntimeConfig();
  const authorization = request.headers.get("authorization") || "";
  const suppliedToken = authorization.startsWith("Bearer ")
    ? authorization.slice("Bearer ".length)
    : "";

  if (!suppliedToken || !(await secretMatches(suppliedToken, runtime.INVITE_ADMIN_TOKEN))) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const body = (await request.json().catch(() => null)) as
    | { label?: string; email?: string; expiresInHours?: number }
    | null;
  const label = body?.label?.trim();
  const email = body?.email ? normalizeEmail(body.email) : "";
  const expiresInHours = Math.min(Math.max(body?.expiresInHours || 168, 1), 720);

  if (!label || label.length > 120 || (email && !isEmail(email))) {
    return Response.json({ error: "Provide a valid label and optional email." }, { status: 400 });
  }

  const code = `netobs_${randomToken(24)}`;
  const tokenHash = await sha256(code);
  const emailHash = email
    ? await hmacSha256(runtime.IDENTITY_PEPPER, email)
    : null;
  const createdAt = new Date();
  const expiresAt = new Date(createdAt.getTime() + expiresInHours * 60 * 60_000);

  await runtime.DB.prepare(
    `INSERT INTO invites
      (token_hash, label, intended_email_hash, created_at, expires_at)
     VALUES (?, ?, ?, ?, ?)`,
  )
    .bind(
      tokenHash,
      label,
      emailHash,
      createdAt.toISOString(),
      expiresAt.toISOString(),
    )
    .run();

  return Response.json(
    {
      inviteCode: code,
      expiresAt: expiresAt.toISOString(),
      restrictedToEmail: Boolean(email),
    },
    {
      status: 201,
      headers: {
        "cache-control": "no-store",
      },
    },
  );
}

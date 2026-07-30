import { deleteSession } from "@/lib/composio";
import { secretMatches } from "@/lib/crypto";
import { listAccess, revokeMcpToken } from "@/lib/database";
import { requireRuntimeConfig } from "@/lib/runtime";

export const dynamic = "force-dynamic";

async function authorized(request: Request, expected: string) {
  const authorization = request.headers.get("authorization") || "";
  const supplied = authorization.startsWith("Bearer ")
    ? authorization.slice("Bearer ".length)
    : "";
  return Boolean(supplied) && secretMatches(supplied, expected);
}

function reply(body: unknown, status = 200) {
  return Response.json(body, {
    status,
    headers: { "cache-control": "no-store" },
  });
}

export async function GET(request: Request) {
  const runtime = requireRuntimeConfig();
  if (!(await authorized(request, runtime.INVITE_ADMIN_TOKEN))) {
    return reply({ error: "Unauthorized" }, 401);
  }
  const result = await listAccess();
  return reply({ access: result.results });
}

export async function DELETE(request: Request) {
  const runtime = requireRuntimeConfig();
  if (!(await authorized(request, runtime.INVITE_ADMIN_TOKEN))) {
    return reply({ error: "Unauthorized" }, 401);
  }
  const body = (await request.json().catch(() => null)) as
    | { sessionId?: string }
    | null;
  const sessionId = body?.sessionId?.trim() || "";
  if (!sessionId.startsWith("trs_") || sessionId.length > 128) {
    return reply({ error: "Provide a valid sessionId." }, 400);
  }

  const revoked = await revokeMcpToken(sessionId);
  if (!revoked) {
    return reply({ error: "Active access not found." }, 404);
  }
  await deleteSession(runtime.COMPOSIO_API_KEY, sessionId);
  return reply({ revoked: true, sessionId });
}

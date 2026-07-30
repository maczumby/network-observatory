import {
  createGmailLink,
  getGmailMessageMetadata,
  sweepGmailMetadata,
} from "@/lib/composio";
import { sha256 } from "@/lib/crypto";
import { checkRateLimit, getMcpToken } from "@/lib/database";
import { requireRuntimeConfig } from "@/lib/runtime";

export const dynamic = "force-dynamic";

type JsonRpcRequest = {
  jsonrpc?: string;
  id?: string | number | null;
  method?: string;
  params?: Record<string, unknown>;
};

const MCP_PROTOCOL_VERSION = "2025-06-18";
const TOOL_SWEEP = "network_observatory_sweep_email_metadata";
const TOOL_GET = "network_observatory_get_message_metadata";

const tools = [
  {
    name: TOOL_SWEEP,
    description:
      "Fetch up to 25 recent Gmail messages as relationship metadata only: sender, recipients, date, labels, and IDs. It never returns subject, body, snippet, or attachments. Use nextPageToken to continue the sweep. Gmail search queries are intentionally unavailable under the metadata-only scope.",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        max_results: {
          type: "integer",
          minimum: 1,
          maximum: 25,
          default: 25,
        },
        page_token: { type: "string" },
        label_ids: {
          type: "array",
          items: { type: "string" },
          maxItems: 10,
        },
        include_spam_trash: { type: "boolean", default: false },
      },
    },
  },
  {
    name: TOOL_GET,
    description:
      "Fetch one Gmail message by ID as relationship metadata only: sender, recipients, date, labels, and IDs. It never returns subject, body, snippet, or attachments.",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        message_id: { type: "string", minLength: 1 },
      },
      required: ["message_id"],
    },
  },
];

function rpcResult(id: JsonRpcRequest["id"], result: unknown) {
  return { jsonrpc: "2.0", id: id ?? null, result };
}

function rpcError(
  id: JsonRpcRequest["id"],
  code: number,
  message: string,
  data?: unknown,
) {
  return {
    jsonrpc: "2.0",
    id: id ?? null,
    error: { code, message, ...(data === undefined ? {} : { data }) },
  };
}

function json(body: unknown, status = 200) {
  return Response.json(body, {
    status,
    headers: {
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}

function textResult(data: unknown, isError = false) {
  return {
    content: [{ type: "text", text: JSON.stringify(data) }],
    structuredContent: data,
    ...(isError ? { isError: true } : {}),
  };
}

function integer(value: unknown, fallback: number) {
  return typeof value === "number" && Number.isInteger(value) ? value : fallback;
}

async function handleRpc(
  request: Request,
  rpc: JsonRpcRequest,
  sessionId: string,
) {
  const runtime = requireRuntimeConfig();

  if (rpc.jsonrpc !== "2.0" || !rpc.method) {
    return rpcError(rpc.id, -32600, "Invalid Request");
  }

  if (rpc.method === "initialize") {
    return rpcResult(rpc.id, {
      protocolVersion: MCP_PROTOCOL_VERSION,
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: "network-observatory-gmail", version: "1.0.0" },
      instructions:
        "Use Gmail only as optional relationship metadata enrichment. Store who and when, never message content. LinkedIn remains sufficient on its own.",
    });
  }
  if (rpc.method === "ping") return rpcResult(rpc.id, {});
  if (rpc.method === "tools/list") return rpcResult(rpc.id, { tools });
  if (rpc.method.startsWith("notifications/")) return null;

  if (rpc.method !== "tools/call") {
    return rpcError(rpc.id, -32601, "Method not found");
  }

  const name = rpc.params?.name;
  const args =
    rpc.params?.arguments && typeof rpc.params.arguments === "object"
      ? (rpc.params.arguments as Record<string, unknown>)
      : {};

  try {
    if (name === TOOL_SWEEP) {
      const maxResults = Math.min(Math.max(integer(args.max_results, 25), 1), 25);
      const labelIds = Array.isArray(args.label_ids)
        ? args.label_ids
            .filter((item): item is string => typeof item === "string")
            .slice(0, 10)
        : [];
      const data = await sweepGmailMetadata(
        runtime.COMPOSIO_API_KEY,
        sessionId,
        {
          maxResults,
          pageToken:
            typeof args.page_token === "string" ? args.page_token : undefined,
          labelIds,
          includeSpamTrash: args.include_spam_trash === true,
        },
      );
      return rpcResult(rpc.id, textResult(data));
    }

    if (name === TOOL_GET) {
      const messageId =
        typeof args.message_id === "string" ? args.message_id.trim() : "";
      if (!messageId) {
        return rpcError(rpc.id, -32602, "message_id is required");
      }
      const data = await getGmailMessageMetadata(
        runtime.COMPOSIO_API_KEY,
        sessionId,
        messageId,
      );
      return rpcResult(rpc.id, textResult(data));
    }

    return rpcError(rpc.id, -32602, "Unknown tool");
  } catch {
    let reconnectUrl: string | null = null;
    try {
      reconnectUrl = await createGmailLink(
        runtime.COMPOSIO_API_KEY,
        sessionId,
        `${new URL(request.url).origin}/connected`,
      );
    } catch {
      // Preserve the original execution failure.
    }
    return rpcResult(
      rpc.id,
      textResult(
        {
          error: "Gmail metadata access is unavailable.",
          action: reconnectUrl
            ? "Reconnect Google, then retry the same call."
            : "Ask the Network Observatory operator to check this connection.",
          reconnectUrl,
        },
        true,
      ),
    );
  }
}

export async function POST(
  request: Request,
  context: { params: Promise<{ token: string }> },
) {
  if (request.headers.get("origin")) {
    return json({ error: "Browser-origin requests are not accepted." }, 403);
  }
  const contentLength = Number(request.headers.get("content-length") || 0);
  if (contentLength > 65_536) {
    return json({ error: "Request too large." }, 413);
  }

  const { token } = await context.params;
  if (!token.startsWith("nobs_") || token.length > 96) {
    return json({ error: "Unauthorized" }, 401);
  }
  const tokenHash = await sha256(token);
  const access = await getMcpToken(tokenHash);
  if (!access) return json({ error: "Unauthorized" }, 401);

  const rate = await checkRateLimit(`mcp:${tokenHash}`, 180, 15 * 60_000);
  if (!rate.allowed) {
    return new Response(JSON.stringify({ error: "Too many requests." }), {
      status: 429,
      headers: {
        "content-type": "application/json",
        "cache-control": "no-store",
        "retry-after": String(rate.retryAfter),
      },
    });
  }

  const body = (await request.json().catch(() => null)) as
    | JsonRpcRequest
    | JsonRpcRequest[]
    | null;
  if (!body) return json(rpcError(null, -32700, "Parse error"), 400);

  if (Array.isArray(body)) {
    if (!body.length || body.length > 10) {
      return json(rpcError(null, -32600, "Invalid batch"), 400);
    }
    const results = (
      await Promise.all(
        body.map((item) => handleRpc(request, item, access.session_id)),
      )
    ).filter((item) => item !== null);
    return results.length ? json(results) : new Response(null, { status: 202 });
  }

  const result = await handleRpc(request, body, access.session_id);
  return result === null ? new Response(null, { status: 202 }) : json(result);
}

export async function GET() {
  return new Response(null, {
    status: 405,
    headers: { allow: "POST", "cache-control": "no-store" },
  });
}

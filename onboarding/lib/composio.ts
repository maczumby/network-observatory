const COMPOSIO_API = "https://backend.composio.dev/api/v3.1";

interface SessionResponse {
  session_id: string;
  mcp: {
    type: "http";
    url: string;
    headers?: Record<string, string>;
  };
}

interface LinkResponse {
  redirect_url: string;
}

interface ProxyResponse {
  data: unknown;
  status: number;
}

async function composioRequest<T>(
  apiKey: string,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${COMPOSIO_API}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      "x-api-key": apiKey,
      ...init.headers,
    },
  });

  const data = (await response.json().catch(() => null)) as
    | (T & { message?: string; error?: string | { message?: string } })
    | null;
  if (!response.ok || !data) {
    const nestedError =
      data?.error && typeof data.error === "object" ? data.error.message : null;
    const detail =
      data?.message ||
      nestedError ||
      (typeof data?.error === "string" ? data.error : null) ||
      `Composio returned ${response.status}`;
    throw new Error(detail);
  }
  return data;
}

export async function createGmailLink(
  apiKey: string,
  sessionId: string,
  callbackUrl: string,
) {
  const link = await composioRequest<LinkResponse>(
    apiKey,
    `/tool_router/session/${encodeURIComponent(sessionId)}/link`,
    {
      method: "POST",
      body: JSON.stringify({
        toolkit: "gmail",
        callback_url: callbackUrl,
      }),
    },
  );
  return link.redirect_url;
}

export async function createGmailSession(
  apiKey: string,
  authConfigId: string,
  userId: string,
  callbackUrl: string,
) {
  const session = await composioRequest<SessionResponse>(
    apiKey,
    "/tool_router/session",
    {
      method: "POST",
      body: JSON.stringify({
        user_id: userId,
        toolkits: { enable: ["gmail"] },
        auth_configs: { gmail: authConfigId },
        tools: {
          gmail: {
            enable: [
              "GMAIL_FETCH_EMAILS",
              "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
            ],
          },
        },
        manage_connections: {
          enable: true,
          callback_url: callbackUrl,
          enable_wait_for_connections: false,
          enable_connection_removal: true,
        },
        workbench: { enable: false, enable_proxy_execution: true },
        multi_account: { enable: false },
        preload: {
          tools: [
            "GMAIL_FETCH_EMAILS",
            "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
          ],
        },
        search: { enable: false },
        execute: { enable_multi_execute: false },
      }),
    },
  );

  const connectUrl = await createGmailLink(
    apiKey,
    session.session_id,
    callbackUrl,
  );

  return {
    sessionId: session.session_id,
    connectUrl,
  };
}

export async function deleteSession(apiKey: string, sessionId: string) {
  try {
    await composioRequest<Record<string, unknown>>(
      apiKey,
      `/tool_router/session/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    );
  } catch {
    // Deletion is best effort during rollback or revocation.
  }
}

async function gmailProxy(
  apiKey: string,
  sessionId: string,
  endpoint: string,
) {
  const response = await composioRequest<ProxyResponse>(
    apiKey,
    `/tool_router/session/${encodeURIComponent(sessionId)}/proxy_execute`,
    {
      method: "POST",
      body: JSON.stringify({
        toolkit_slug: "gmail",
        endpoint,
        method: "GET",
      }),
    },
  );
  return response.data;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

function sanitizeMessage(value: unknown) {
  const message = asRecord(value);
  const payload = asRecord(message.payload);
  const headers = Array.isArray(payload.headers) ? payload.headers : [];
  const allowed = new Set(["from", "to", "cc", "bcc", "date"]);
  const cleanHeaders: Record<string, string> = {};

  for (const header of headers) {
    const item = asRecord(header);
    const name = typeof item.name === "string" ? item.name.toLowerCase() : "";
    if (allowed.has(name) && typeof item.value === "string") {
      cleanHeaders[name] = item.value;
    }
  }

  return {
    id: typeof message.id === "string" ? message.id : "",
    threadId: typeof message.threadId === "string" ? message.threadId : "",
    labelIds: Array.isArray(message.labelIds)
      ? message.labelIds.filter((item): item is string => typeof item === "string")
      : [],
    internalDate:
      typeof message.internalDate === "string" ? message.internalDate : "",
    headers: cleanHeaders,
  };
}

export async function getGmailMessageMetadata(
  apiKey: string,
  sessionId: string,
  messageId: string,
) {
  const query = new URLSearchParams({ format: "metadata" });
  for (const header of ["From", "To", "Cc", "Bcc", "Date"]) {
    query.append("metadataHeaders", header);
  }
  const endpoint =
    `https://gmail.googleapis.com/gmail/v1/users/me/messages/` +
    `${encodeURIComponent(messageId)}?${query.toString()}`;
  return sanitizeMessage(await gmailProxy(apiKey, sessionId, endpoint));
}

export async function sweepGmailMetadata(
  apiKey: string,
  sessionId: string,
  options: {
    maxResults: number;
    pageToken?: string;
    labelIds?: string[];
    includeSpamTrash?: boolean;
  },
) {
  const query = new URLSearchParams({
    maxResults: String(Math.min(Math.max(options.maxResults, 1), 25)),
    includeSpamTrash: options.includeSpamTrash ? "true" : "false",
  });
  if (options.pageToken) query.set("pageToken", options.pageToken);
  for (const labelId of options.labelIds || []) query.append("labelIds", labelId);

  const endpoint =
    `https://gmail.googleapis.com/gmail/v1/users/me/messages?${query.toString()}`;
  const list = asRecord(await gmailProxy(apiKey, sessionId, endpoint));
  const messages = Array.isArray(list.messages) ? list.messages : [];
  const ids = messages
    .map((item) => asRecord(item).id)
    .filter((item): item is string => typeof item === "string");

  const metadata = await Promise.all(
    ids.map((id) => getGmailMessageMetadata(apiKey, sessionId, id)),
  );
  return {
    messages: metadata,
    nextPageToken:
      typeof list.nextPageToken === "string" ? list.nextPageToken : null,
    resultSizeEstimate:
      typeof list.resultSizeEstimate === "number" ? list.resultSizeEstimate : null,
  };
}

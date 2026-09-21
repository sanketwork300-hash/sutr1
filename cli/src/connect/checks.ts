import {
  HttpError,
  RequestTimeoutError,
  buildHeaders,
  request,
} from "../client.js";
import { maskKey, type Config } from "../config.js";
import { ConnectError } from "./errors.js";

export interface ConfigurationCheck {
  url: string;
  mcpEndpoint: string;
  authMode: string;
}

export interface SessionCheck {
  /** An email for a user-scoped credential, a masked key for an API key. */
  account: string;
  scope: "user" | "organization";
  /** Integrations installed for the authenticated organization. */
  installed: number;
}

export interface ToolRecord {
  name?: string;
  integration_id?: string;
  execution_mode?: string;
}

export interface PolicySummary {
  allow: number;
  require_approval: number;
  deny: number;
}

const AUTH_HINT =
  "Create an API key in the Sutr UI under Develop → API Keys, then run:\n  sutr auth login --api-key ap_...";

export function mcpEndpointFor(url: string): string {
  return `${url.replace(/\/+$/, "")}/mcp`;
}

/**
 * Step one, and entirely local: is there enough on this machine to try at all?
 * Failing here costs no network round trip and names the exact missing piece.
 */
export function checkConfiguration(config: Config): ConfigurationCheck {
  const url = config.url?.trim();
  if (!url) {
    throw new ConnectError(
      "No Sutr instance configured",
      "The CLI has no server URL. Set SUTR_URL, or store one in the config.",
      "sutr auth set-instance-url https://app.sutr.sh",
    );
  }

  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new ConnectError(
      "Invalid configuration",
      `'${url}' is not a valid URL.`,
      "sutr auth set-instance-url https://app.sutr.sh",
    );
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new ConnectError(
      "Invalid configuration",
      `'${url}' must use http or https, not '${parsed.protocol.replace(":", "")}'.`,
      "sutr auth set-instance-url https://app.sutr.sh",
    );
  }

  if (config.auth_mode !== "api_key" || !config.api_key) {
    throw new ConnectError(
      "Not authenticated",
      `No API key is stored for ${url}.`,
      AUTH_HINT,
    );
  }

  return {
    url: url.replace(/\/+$/, ""),
    mcpEndpoint: mcpEndpointFor(url),
    authMode: config.auth_mode,
  };
}

/**
 * Step two: the stored credential is presented to the server and accepted.
 *
 * An API key authenticates an *organization*, not a person — `/api/users/me` is
 * user-scoped and rejects it — so the credential is exercised against an
 * endpoint that takes agent auth, and the account line reports the masked key
 * rather than inventing an identity the server never returned.
 */
export async function authenticateSession(
  config: Config,
  timeoutMs: number,
): Promise<SessionCheck> {
  try {
    if (config.auth_mode !== "api_key") {
      const me = await request<{ email: string }>("/api/users/me", { config, timeoutMs });
      const installed = await request<unknown[]>("/api/installed", { config, timeoutMs });
      return {
        account: me.email,
        scope: "user",
        installed: Array.isArray(installed) ? installed.length : 0,
      };
    }

    const installed = await request<unknown[]>("/api/installed", { config, timeoutMs });
    return {
      account: maskKey(config.api_key),
      scope: "organization",
      installed: Array.isArray(installed) ? installed.length : 0,
    };
  } catch (error) {
    throw asConnectError(error, "Authentication failed", config.url, {
      401: AUTH_HINT,
      403: AUTH_HINT,
    });
  }
}

/**
 * Step three: the MCP endpoint an agent will be pointed at actually exists.
 *
 * A GET without `Accept: text/event-stream` is not a valid Streamable HTTP
 * request, and that is the point — the gateway answers with a 4xx, which proves
 * the route is mounted without opening a stream we would then have to abandon.
 * Only 404 (not mounted) and 5xx (broken) are failures.
 */
export async function verifyMcpEndpoint(
  config: Config,
  timeoutMs: number,
): Promise<{ endpoint: string; status: number }> {
  const endpoint = mcpEndpointFor(config.url);
  let response: Response;
  try {
    response = await fetch(endpoint, {
      method: "GET",
      headers: buildHeaders(config, false),
      signal: AbortSignal.timeout(timeoutMs),
      redirect: "manual",
    });
  } catch (error) {
    throw asConnectError(error, "Could not reach the MCP endpoint", endpoint, {});
  }
  // Nothing is read from the body; releasing it keeps the socket from lingering.
  await response.body?.cancel().catch(() => undefined);

  if (response.status === 404) {
    throw new ConnectError(
      "MCP endpoint not found",
      `${endpoint} returned 404. This instance may be older than the MCP gateway, or the URL may point at something else.`,
      "Check the URL with `sutr auth status`, then `sutr auth set-instance-url <url>`.",
    );
  }
  if (response.status === 401 || response.status === 403) {
    throw new ConnectError(
      "The MCP endpoint rejected the credential",
      `${endpoint} returned ${response.status}.`,
      AUTH_HINT,
    );
  }
  if (response.status >= 500) {
    throw new ConnectError(
      "The Sutr server returned an error",
      `${endpoint} returned ${response.status}.`,
      "Retry shortly, or check the server logs if you self-host.",
    );
  }

  return { endpoint, status: response.status };
}

/** Step four: the tools this account can actually reach, with their policies. */
export async function loadTools(
  config: Config,
  timeoutMs: number,
): Promise<ToolRecord[]> {
  try {
    const tools = await request<ToolRecord[]>("/api/tools", { config, timeoutMs });
    return Array.isArray(tools) ? tools : [];
  } catch (error) {
    throw asConnectError(error, "Could not load tools", config.url, {
      401: AUTH_HINT,
      403: AUTH_HINT,
    });
  }
}

/**
 * Step five, a local reduction of what step four returned. The policies are
 * enforced by the server on every call; this only reports the shape of them, so
 * the summary is never mistaken for the CLI having applied anything.
 */
export function summarizePolicies(tools: ToolRecord[]): PolicySummary {
  const summary: PolicySummary = { allow: 0, require_approval: 0, deny: 0 };
  for (const tool of tools) {
    const mode = tool.execution_mode ?? "require_approval";
    if (mode === "allow") summary.allow += 1;
    else if (mode === "deny") summary.deny += 1;
    else summary.require_approval += 1;
  }
  return summary;
}

export function countIntegrations(tools: ToolRecord[]): number {
  return new Set(tools.map((tool) => tool.integration_id).filter(Boolean)).size;
}

/**
 * Translate a transport-level failure into something with a next step, keeping
 * the original message intact — the server's own words are the useful part.
 */
function asConnectError(
  error: unknown,
  title: string,
  target: string,
  hints: Record<number, string>,
): ConnectError {
  if (error instanceof ConnectError) return error;
  if (error instanceof RequestTimeoutError || (error as Error)?.name === "TimeoutError") {
    return new ConnectError(
      "Timed out",
      (error as Error).message,
      `${target} did not answer in time. Check the URL and your network, then retry with --timeout <seconds>.`,
    );
  }
  if (error instanceof HttpError) {
    return new ConnectError(title, `HTTP ${error.status} — ${error.message}`, hints[error.status]);
  }
  const message = (error as Error)?.message ?? String(error);
  // `request()` has already turned the low-level failure into prose, so both
  // the raw errno and its rewritten form have to be recognised here.
  if (
    /fetch failed|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|ECONNRESET|certificate|Could not connect to server|failed: /i.test(
      message,
    )
  ) {
    return new ConnectError(
      "Could not connect to Sutr",
      message,
      `Check that ${target} is reachable, then run \`sutr auth status\` to confirm what the CLI is pointed at.`,
    );
  }
  return new ConnectError(title, message);
}

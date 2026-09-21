import { existsSync, mkdirSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Config } from "../src/config.js";
import type { PathContext } from "../src/connect/agents.js";
import { ConnectCancelled, ConnectError } from "../src/connect/errors.js";
import { runConnect, type ConnectDeps, type ConnectOptions } from "../src/connect/flow.js";
import { createTerminalUi } from "../src/ui/terminal.js";

const KEY = "ap_notarealkeynotarealkey";
const URL = "https://sutr.test";

const TOOLS = [
  { name: "create_issue", integration_id: "github", execution_mode: "require_approval" },
  { name: "list_issues", integration_id: "github", execution_mode: "allow" },
  { name: "create_refund", integration_id: "stripe", execution_mode: "deny" },
];

interface Route {
  status?: number;
  body?: unknown;
  throws?: Error;
}

function config(overrides: Partial<Config> = {}): Config {
  return {
    url: URL,
    auth_mode: "api_key",
    output_format: "human",
    api_key: KEY,
    access_token: "",
    refresh_token: "",
    oauth_client_id: "",
    ...overrides,
  };
}

/** Routes keyed by the path suffix of the request. */
function scriptedFetch(routes: Record<string, Route>) {
  const calls: { url: string; headers: Record<string, string> }[] = [];
  const fetchMock = vi.fn(async (input: unknown, init: RequestInit = {}) => {
    const url = String(input);
    calls.push({ url, headers: (init.headers ?? {}) as Record<string, string> });
    const key = Object.keys(routes).find((suffix) => url.endsWith(suffix));
    const route = key ? routes[key] : undefined;
    if (!route) throw new Error(`unexpected request: ${url}`);
    if (route.throws) throw route.throws;
    const status = route.status ?? 200;
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: `status ${status}`,
      body: null,
      json: async () => route.body ?? {},
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

const HAPPY_ROUTES: Record<string, Route> = {
  "/api/installed": { body: [{ integration_id: "github" }, { integration_id: "stripe" }] },
  "/mcp": { status: 406 },
  "/api/tools": { body: TOOLS },
};

function recorder() {
  const chunks: string[] = [];
  return { chunks, isTTY: false, write: (c: string) => chunks.push(c), text: () => chunks.join("") };
}

let root: string;
let stream: ReturnType<typeof recorder>;
let errorStream: ReturnType<typeof recorder>;

function context(): PathContext {
  const home = join(root, "home");
  const cwd = join(root, "project");
  mkdirSync(home, { recursive: true });
  mkdirSync(cwd, { recursive: true });
  return { home, cwd, env: {}, platform: "linux" };
}

function deps(overrides: Partial<ConnectDeps> = {}): ConnectDeps {
  return {
    config: config(),
    ui: createTerminalUi({ stream, errorStream, env: { LANG: "en_US.UTF-8" }, platform: "linux" }),
    context: context(),
    interactive: false,
    confirm: async () => true,
    ...overrides,
  };
}

function options(overrides: Partial<ConnectOptions> = {}): ConnectOptions {
  return {
    timeoutMs: 5000,
    writeApiKey: true,
    dryRun: false,
    assumeYes: true,
    ...overrides,
  };
}

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "sutr-flow-"));
  stream = recorder();
  errorStream = recorder();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("runConnect — success", () => {
  it("reports the endpoint, the tool counts and the policy split", async () => {
    scriptedFetch(HAPPY_ROUTES);

    const result = await runConnect(options(), deps());

    expect(result.mcp_endpoint).toBe(`${URL}/mcp`);
    expect(result.tools).toBe(3);
    expect(result.integrations).toBe(2);
    expect(result.policies).toEqual({ allow: 1, require_approval: 1, deny: 1 });
    expect(result.agent).toBeNull();
    expect(result.config_path).toBeNull();
    expect(result.restart_required).toBe(false);
  });

  it("always offers the skills install command as an optional next step", async () => {
    scriptedFetch(HAPPY_ROUTES);
    const result = await runConnect(options(), deps());
    expect(result.skills_command).toBe("npx skills add sutr-dev/sutr-skills");
    expect(result.skills_repository).toBe("https://github.com/sutr-dev/sutr-skills");
  });

  it("never puts the API key in the result or on screen", async () => {
    scriptedFetch(HAPPY_ROUTES);
    const result = await runConnect(options(), deps());

    expect(JSON.stringify(result)).not.toContain(KEY);
    expect(result.account).toBe("ap_not...lkey");
    expect(stream.text()).not.toContain(KEY);
  });

  it("sends the key as a header, and only to the configured instance", async () => {
    const { calls } = scriptedFetch(HAPPY_ROUTES);
    await runConnect(options(), deps());
    expect(calls.every((call) => call.url.startsWith(URL))).toBe(true);
    expect(calls[0].headers["X-API-Key"]).toBe(KEY);
  });

  it("prints each step once, in order, with no escape codes off a terminal", async () => {
    scriptedFetch(HAPPY_ROUTES);
    await runConnect(options(), deps());

    const text = stream.text();
    expect(text).not.toContain("\x1b");
    const order = [
      "Checking configuration",
      "Authenticating session",
      "Verifying the MCP endpoint",
      "Loading available tools",
      "Reading access policies",
    ].map((label) => text.indexOf(label));
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(order[0]).toBeGreaterThan(-1);
  });

  it("warns when the account has no tools yet", async () => {
    scriptedFetch({ ...HAPPY_ROUTES, "/api/tools": { body: [] } });
    const result = await runConnect(options(), deps());
    expect(result.tools).toBe(0);
    expect(stream.text()).toContain("sutr integrations add");
  });
});

describe("runConnect — failures", () => {
  it("stops before any request when no credential is stored", async () => {
    const { fetchMock } = scriptedFetch(HAPPY_ROUTES);
    const promise = runConnect(options(), deps({ config: config({ api_key: "", auth_mode: "" }) }));

    await expect(promise).rejects.toThrow(ConnectError);
    await expect(promise).rejects.toMatchObject({
      title: "Not authenticated",
      hint: expect.stringContaining("sutr auth login"),
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a URL that is not http(s)", async () => {
    scriptedFetch(HAPPY_ROUTES);
    await expect(
      runConnect(options(), deps({ config: config({ url: "ftp://sutr.test" }) })),
    ).rejects.toMatchObject({ title: "Invalid configuration" });
  });

  it("surfaces the server's own words on an authentication failure", async () => {
    scriptedFetch({
      ...HAPPY_ROUTES,
      "/api/installed": { status: 401, body: { detail: "Invalid API key" } },
    });

    await expect(runConnect(options(), deps())).rejects.toMatchObject({
      title: "Authentication failed",
      reason: expect.stringContaining("Invalid API key"),
      hint: expect.stringContaining("sutr auth login"),
    });
    expect(errorStream.text()).toBe("");
  });

  it("names the host when the server cannot be reached", async () => {
    scriptedFetch({
      ...HAPPY_ROUTES,
      "/api/installed": { throws: new TypeError("fetch failed") },
    });

    await expect(runConnect(options(), deps())).rejects.toMatchObject({
      title: "Could not connect to Sutr",
      hint: expect.stringContaining(URL),
    });
  });

  it("distinguishes a timeout from a refusal", async () => {
    const timeout = new Error("The operation was aborted due to timeout");
    timeout.name = "TimeoutError";
    scriptedFetch({ ...HAPPY_ROUTES, "/api/installed": { throws: timeout } });

    await expect(runConnect(options(), deps())).rejects.toMatchObject({
      title: "Timed out",
      hint: expect.stringContaining("--timeout"),
    });
  });

  it("fails when the MCP endpoint is not mounted", async () => {
    scriptedFetch({ ...HAPPY_ROUTES, "/mcp": { status: 404 } });
    await expect(runConnect(options(), deps())).rejects.toMatchObject({
      title: "MCP endpoint not found",
    });
  });

  it("fails when the gateway is broken rather than claiming success", async () => {
    scriptedFetch({ ...HAPPY_ROUTES, "/mcp": { status: 503 } });
    await expect(runConnect(options(), deps())).rejects.toMatchObject({
      title: "The Sutr server returned an error",
    });
  });

  it("marks the failing step and leaves earlier steps intact", async () => {
    scriptedFetch({ ...HAPPY_ROUTES, "/api/tools": { status: 500, body: {} } });
    await expect(runConnect(options(), deps())).rejects.toThrow(ConnectError);

    const text = stream.text();
    expect(text).toContain("✓ Checking configuration");
    expect(text).toContain("✗ Loading available tools");
  });

  it("rejects an unknown agent without touching the network", async () => {
    const { fetchMock } = scriptedFetch(HAPPY_ROUTES);
    await expect(runConnect(options({ agent: "emacs" }), deps())).rejects.toMatchObject({
      title: "Unknown agent",
      hint: expect.stringContaining("claude-code"),
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a scope the agent does not have", async () => {
    scriptedFetch(HAPPY_ROUTES);
    await expect(
      runConnect(options({ agent: "vscode", scope: "user" }), deps()),
    ).rejects.toMatchObject({ title: "Unsupported scope" });
  });
});

describe("runConnect — configuring an agent", () => {
  it("writes the config and asks for a restart", async () => {
    scriptedFetch(HAPPY_ROUTES);
    const dependencies = deps();

    const result = await runConnect(options({ agent: "claude-code" }), dependencies);

    expect(result.agent_name).toBe("Claude Code");
    expect(result.config_action).toBe("create");
    expect(result.restart_required).toBe(true);
    expect(result.restart_hint).toContain("Claude Code");

    const written = JSON.parse(readFileSync(result.config_path!, "utf-8"));
    expect(written.mcpServers.sutr.url).toBe(`${URL}/mcp`);
  });

  it("writes nothing for a dry run", async () => {
    scriptedFetch(HAPPY_ROUTES);
    const result = await runConnect(options({ agent: "claude-code", dryRun: true }), deps());

    expect(result.config_action).toBe("planned");
    expect(result.restart_required).toBe(false);
    expect(existsSync(result.config_path!)).toBe(false);
  });

  it("refuses to change a file unattended when nothing can confirm", async () => {
    scriptedFetch(HAPPY_ROUTES);
    const dependencies = deps({ interactive: false });

    await expect(
      runConnect(options({ agent: "claude-code", assumeYes: false }), dependencies),
    ).rejects.toMatchObject({
      title: "Refusing to change a configuration file unattended",
      hint: expect.stringContaining("--yes"),
    });
    expect(existsSync(join(dependencies.context.cwd, ".mcp.json"))).toBe(false);
  });

  it("shows the change, with the key masked, before asking", async () => {
    scriptedFetch(HAPPY_ROUTES);
    const confirm = vi.fn(async () => true);
    const dependencies = deps({ interactive: true, confirm });

    await runConnect(options({ agent: "claude-code", assumeYes: false }), dependencies);

    expect(confirm).toHaveBeenCalledOnce();
    const text = stream.text();
    expect(text).toContain("ap_not...lkey");
    expect(text).not.toContain(KEY);
    expect(text).toContain("Do not commit it.");
  });

  it("leaves the file alone when the user declines", async () => {
    scriptedFetch(HAPPY_ROUTES);
    const dependencies = deps({ interactive: true, confirm: async () => false });

    await expect(
      runConnect(options({ agent: "claude-code", assumeYes: false }), dependencies),
    ).rejects.toThrow(ConnectCancelled);
    expect(existsSync(join(dependencies.context.cwd, ".mcp.json"))).toBe(false);
  });

  it("is idempotent: a second run reports no change and no restart", async () => {
    scriptedFetch(HAPPY_ROUTES);
    const dependencies = deps();

    await runConnect(options({ agent: "claude-code" }), dependencies);
    const second = await runConnect(options({ agent: "claude-code" }), {
      ...dependencies,
      ui: createTerminalUi({ stream, errorStream, env: {}, platform: "linux" }),
    });

    expect(second.config_action).toBe("unchanged");
    expect(second.restart_required).toBe(false);
  });

  it("can leave the credential out of the agent's config", async () => {
    scriptedFetch(HAPPY_ROUTES);
    const result = await runConnect(
      options({ agent: "cursor", scope: "user", writeApiKey: false }),
      deps(),
    );

    const written = readFileSync(result.config_path!, "utf-8");
    expect(written).not.toContain(KEY);
    expect(written).not.toContain("X-API-Key");
  });
});

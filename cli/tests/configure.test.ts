import { mkdtempSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { findAgent, type PathContext } from "../src/connect/agents.js";
import { planAgentConfiguration } from "../src/connect/configure.js";
import { ConnectError } from "../src/connect/errors.js";

const KEY = "ap_fakefakefakefakefake";
const ENDPOINT = "https://app.sutr.sh/mcp";

const roots: string[] = [];

function context(): PathContext {
  const root = mkdtempSync(join(tmpdir(), "sutr-connect-"));
  roots.push(root);
  const home = join(root, "home");
  const cwd = join(root, "project");
  mkdirSync(home, { recursive: true });
  mkdirSync(cwd, { recursive: true });
  return { home, cwd, env: {}, platform: "linux" };
}

function plan(agentId: string, scope: "user" | "project", ctx: PathContext, apiKey: string | null = KEY) {
  const agent = findAgent(agentId);
  if (!agent) throw new Error(`missing agent ${agentId}`);
  return planAgentConfiguration(agent, scope, { endpoint: ENDPOINT, apiKey }, ctx);
}

afterEach(() => {
  roots.length = 0;
});

describe("planAgentConfiguration (JSON clients)", () => {
  it("creates the file when there is nothing there yet", () => {
    const ctx = context();
    const configuration = plan("claude-code", "project", ctx);

    expect(configuration.action).toBe("create");
    expect(configuration.path).toBe(join(ctx.cwd, ".mcp.json"));

    const { backupPath } = configuration.write();
    expect(backupPath).toBeUndefined();

    const written = JSON.parse(readFileSync(configuration.path, "utf-8"));
    expect(written.mcpServers.sutr).toEqual({
      type: "http",
      url: ENDPOINT,
      headers: { "X-API-Key": KEY },
    });
  });

  it("never prints the key it is about to write", () => {
    const configuration = plan("claude-code", "project", context());
    expect(configuration.preview).not.toContain(KEY);
    expect(configuration.preview).toContain("ap_fak");
    expect(configuration.carriesSecret).toBe(true);
  });

  it("omits the header entirely when the key is withheld", () => {
    const ctx = context();
    const configuration = plan("cursor", "user", ctx, null);
    configuration.write();
    const written = JSON.parse(readFileSync(configuration.path, "utf-8"));
    expect(written.mcpServers.sutr).toEqual({ url: ENDPOINT });
    expect(configuration.carriesSecret).toBe(false);
  });

  it("merges into an existing file, keeping other servers and backing it up", () => {
    const ctx = context();
    const path = join(ctx.cwd, ".mcp.json");
    writeFileSync(
      path,
      JSON.stringify({ mcpServers: { other: { url: "https://other.example/mcp" } }, extra: 1 }),
    );

    const configuration = plan("claude-code", "project", ctx);
    expect(configuration.action).toBe("update");

    const { backupPath } = configuration.write();
    expect(backupPath).toBe(path + ".sutr-backup");
    expect(JSON.parse(readFileSync(backupPath!, "utf-8")).mcpServers.other).toBeDefined();

    const written = JSON.parse(readFileSync(path, "utf-8"));
    expect(written.extra).toBe(1);
    expect(written.mcpServers.other).toEqual({ url: "https://other.example/mcp" });
    expect(written.mcpServers.sutr.url).toBe(ENDPOINT);
  });

  it("reports an identical entry as unchanged", () => {
    const ctx = context();
    plan("claude-code", "project", ctx).write();
    expect(plan("claude-code", "project", ctx).action).toBe("unchanged");
  });

  it("restricts a file that now holds a credential", () => {
    const ctx = context();
    const configuration = plan("claude-code", "project", ctx);
    configuration.write();
    expect(statSync(configuration.path).mode & 0o777).toBe(0o600);
  });

  it("uses the VS Code schema variant", () => {
    const ctx = context();
    const configuration = plan("vscode", "project", ctx);
    expect(configuration.path).toBe(join(ctx.cwd, ".vscode", "mcp.json"));
    configuration.write();
    const written = JSON.parse(readFileSync(configuration.path, "utf-8"));
    expect(written.servers.sutr.type).toBe("http");
    expect(written.mcpServers).toBeUndefined();
  });

  it("bridges Claude Desktop over stdio, since its config cannot call HTTP", () => {
    const ctx = context();
    const configuration = plan("claude-desktop", "user", ctx);
    configuration.write();
    const written = JSON.parse(readFileSync(configuration.path, "utf-8"));
    expect(written.mcpServers.sutr.command).toBe("npx");
    expect(written.mcpServers.sutr.args).toContain("mcp-remote");
    expect(written.mcpServers.sutr.env.SUTR_API_KEY).toBe(KEY);
  });

  it("refuses a file it cannot parse instead of overwriting it", () => {
    const ctx = context();
    const path = join(ctx.cwd, ".mcp.json");
    writeFileSync(path, "{ not json");
    expect(() => plan("claude-code", "project", ctx)).toThrow(ConnectError);
    expect(readFileSync(path, "utf-8")).toBe("{ not json");
  });

  it("refuses a config whose server map is the wrong shape", () => {
    const ctx = context();
    writeFileSync(join(ctx.cwd, ".mcp.json"), JSON.stringify({ mcpServers: ["nope"] }));
    expect(() => plan("claude-code", "project", ctx)).toThrow(/not an object/);
  });
});

describe("planAgentConfiguration (Codex TOML)", () => {
  it("appends a table when the file has no sutr entry", () => {
    const ctx = context();
    const configuration = plan("codex", "user", ctx);
    expect(configuration.action).toBe("create");
    configuration.write();
    const written = readFileSync(configuration.path, "utf-8");
    expect(written).toContain("[mcp_servers.sutr]");
    expect(written).toContain(`url = "${ENDPOINT}"`);
  });

  it("keeps the rest of the file, comments included", () => {
    const ctx = context();
    const path = join(ctx.home, ".codex", "config.toml");
    mkdirSync(join(ctx.home, ".codex"), { recursive: true });
    writeFileSync(path, "# my settings\nmodel = \"gpt-5\"\n");

    const configuration = plan("codex", "user", ctx);
    expect(configuration.action).toBe("update");
    configuration.write();

    const written = readFileSync(path, "utf-8");
    expect(written).toContain("# my settings");
    expect(written).toContain("[mcp_servers.sutr]");
  });

  it("hands back a manual step rather than rewriting an existing entry", () => {
    const ctx = context();
    plan("codex", "user", ctx).write();

    const second = plan("codex", "user", ctx);
    expect(second.action).toBe("manual");
    expect(second.reason).toContain("[mcp_servers.sutr]");
    expect(second.preview).not.toContain(KEY);
  });
});

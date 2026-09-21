import { join } from "node:path";

/**
 * The agents this CLI knows how to wire up, and where each one keeps its MCP
 * configuration.
 *
 * Every path and entry shape here is the one documented for that agent — the
 * CLI writes what a person following the docs by hand would have written, so a
 * config it touches stays hand-editable afterwards.
 */

export type AgentScope = "user" | "project";

export interface PathContext {
  home: string;
  cwd: string;
  env: NodeJS.ProcessEnv;
  platform: NodeJS.Platform;
}

export interface EntryInput {
  /** The `/mcp` URL the agent will call. */
  endpoint: string;
  /** The API key to embed, or null to leave authentication to the client. */
  apiKey: string | null;
}

interface AgentBase {
  id: string;
  name: string;
  scopes: readonly AgentScope[];
  defaultScope: AgentScope;
  /** What the user must do for the change to take effect. */
  restart: string;
  resolvePath(scope: AgentScope, context: PathContext): string;
}

export interface JsonAgent extends AgentBase {
  kind: "json";
  /** Top-level key holding the server map — VS Code calls it `servers`. */
  container: string;
  buildEntry(input: EntryInput): Record<string, unknown>;
}

export interface TomlAgent extends AgentBase {
  kind: "toml";
  /** The table header used to detect an entry that already exists. */
  section: string;
  buildSection(input: EntryInput): string;
}

export type AgentDefinition = JsonAgent | TomlAgent;

/** The server name written into every client config. */
export const SERVER_NAME = "sutr";

function headers(input: EntryInput): Record<string, unknown> {
  return input.apiKey ? { headers: { "X-API-Key": input.apiKey } } : {};
}

function claudeDesktopDirectory(context: PathContext): string {
  if (context.platform === "darwin") {
    return join(context.home, "Library", "Application Support", "Claude");
  }
  if (context.platform === "win32") {
    return join(context.env.APPDATA ?? join(context.home, "AppData", "Roaming"), "Claude");
  }
  return join(context.home, ".config", "Claude");
}

export const AGENTS: readonly AgentDefinition[] = [
  {
    kind: "json",
    id: "claude-code",
    name: "Claude Code",
    scopes: ["project", "user"],
    defaultScope: "project",
    container: "mcpServers",
    restart: "Restart Claude Code (or run /mcp) to pick up the server.",
    resolvePath: (scope, context) =>
      scope === "user"
        ? join(context.home, ".claude.json")
        : join(context.cwd, ".mcp.json"),
    buildEntry: (input) => ({ type: "http", url: input.endpoint, ...headers(input) }),
  },
  {
    kind: "json",
    id: "claude-desktop",
    name: "Claude Desktop",
    scopes: ["user"],
    defaultScope: "user",
    container: "mcpServers",
    restart: "Quit and reopen Claude Desktop.",
    resolvePath: (_scope, context) =>
      join(claudeDesktopDirectory(context), "claude_desktop_config.json"),
    // Claude Desktop's config only launches stdio servers, so the documented
    // route to an HTTP gateway is the mcp-remote bridge.
    buildEntry: (input) =>
      input.apiKey
        ? {
            command: "npx",
            args: [
              "-y",
              "mcp-remote",
              input.endpoint,
              "--header",
              "X-API-Key:${SUTR_API_KEY}",
            ],
            env: { SUTR_API_KEY: input.apiKey },
          }
        : {
            command: "npx",
            args: ["-y", "mcp-remote", input.endpoint],
          },
  },
  {
    kind: "json",
    id: "cursor",
    name: "Cursor",
    scopes: ["user", "project"],
    defaultScope: "user",
    container: "mcpServers",
    restart: "Reload Cursor, then open Settings → MCP to authenticate.",
    resolvePath: (scope, context) =>
      scope === "user"
        ? join(context.home, ".cursor", "mcp.json")
        : join(context.cwd, ".cursor", "mcp.json"),
    buildEntry: (input) => ({ url: input.endpoint, ...headers(input) }),
  },
  {
    kind: "json",
    id: "vscode",
    name: "VS Code",
    scopes: ["project"],
    defaultScope: "project",
    // VS Code uses its own schema variant; the key is `servers`, not `mcpServers`.
    container: "servers",
    restart: "Reload the VS Code window, then start the server from the MCP panel.",
    resolvePath: (_scope, context) => join(context.cwd, ".vscode", "mcp.json"),
    buildEntry: (input) => ({ type: "http", url: input.endpoint, ...headers(input) }),
  },
  {
    kind: "toml",
    id: "codex",
    name: "OpenAI Codex",
    scopes: ["user"],
    defaultScope: "user",
    section: `[mcp_servers.${SERVER_NAME}]`,
    restart: "Start a new Codex session; `codex mcp list` should show sutr.",
    resolvePath: (_scope, context) => join(context.home, ".codex", "config.toml"),
    buildSection: (input) => {
      const lines = [`[mcp_servers.${SERVER_NAME}]`, `url = "${input.endpoint}"`];
      if (input.apiKey) {
        lines.push("", `[mcp_servers.${SERVER_NAME}.headers]`, `"X-API-Key" = "${input.apiKey}"`);
      }
      return lines.join("\n") + "\n";
    },
  },
];

export function findAgent(id: string): AgentDefinition | undefined {
  const normalized = id.trim().toLowerCase();
  return AGENTS.find((agent) => agent.id === normalized);
}

export function agentIds(): string[] {
  return AGENTS.map((agent) => agent.id);
}

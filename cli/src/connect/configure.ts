import { chmodSync, copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { maskKey } from "../config.js";
import {
  SERVER_NAME,
  type AgentDefinition,
  type AgentScope,
  type EntryInput,
  type PathContext,
} from "./agents.js";
import { ConnectError } from "./errors.js";

/**
 * Planning and applying a change to someone else's configuration file.
 *
 * The plan is computed first and shown before anything is written, because
 * these files are hand-maintained: the CLI merges one `sutr` entry into
 * whatever is already there, keeps a `.sutr-backup` of the previous contents,
 * and refuses outright rather than guessing when it cannot understand the file.
 */

export type PlanAction = "create" | "update" | "unchanged" | "manual";

export interface ConfigurePlan {
  agent: AgentDefinition;
  scope: AgentScope;
  path: string;
  action: PlanAction;
  /** Why the CLI will not write it itself, when `action` is "manual". */
  reason?: string;
  /** The entry, with any credential masked. Safe to print. */
  preview: string;
  carriesSecret: boolean;
  write(): { backupPath?: string };
}

const BACKUP_SUFFIX = ".sutr-backup";

export function planAgentConfiguration(
  agent: AgentDefinition,
  scope: AgentScope,
  input: EntryInput,
  context: PathContext,
): ConfigurePlan {
  const path = agent.resolvePath(scope, context);
  const carriesSecret = input.apiKey !== null;
  const masked: EntryInput = {
    endpoint: input.endpoint,
    apiKey: input.apiKey ? maskKey(input.apiKey) : null,
  };

  return agent.kind === "json"
    ? planJson(agent, scope, path, input, masked, carriesSecret, context)
    : planToml(agent, scope, path, input, masked, carriesSecret, context);
}

function planJson(
  agent: Extract<AgentDefinition, { kind: "json" }>,
  scope: AgentScope,
  path: string,
  input: EntryInput,
  masked: EntryInput,
  carriesSecret: boolean,
  context: PathContext,
): ConfigurePlan {
  const exists = existsSync(path);
  const document = exists ? parseJsonConfig(path) : {};
  const container = document[agent.container];
  if (container !== undefined && (typeof container !== "object" || container === null || Array.isArray(container))) {
    throw new ConnectError(
      "Unexpected agent configuration",
      `'${agent.container}' in ${path} is not an object.`,
      `Fix or remove that key, then run the command again.`,
    );
  }

  const servers = (container as Record<string, unknown> | undefined) ?? {};
  const entry = agent.buildEntry(input);
  const unchanged = deepEqual(servers[SERVER_NAME], entry);

  return {
    agent,
    scope,
    path,
    action: unchanged ? "unchanged" : exists ? "update" : "create",
    preview: JSON.stringify(
      { [agent.container]: { [SERVER_NAME]: agent.buildEntry(masked) } },
      null,
      2,
    ),
    carriesSecret,
    write() {
      const backupPath = backup(path, exists);
      const next = { ...document, [agent.container]: { ...servers, [SERVER_NAME]: entry } };
      writeFile(path, JSON.stringify(next, null, 2) + "\n", carriesSecret, context);
      return { backupPath };
    },
  };
}

function planToml(
  agent: Extract<AgentDefinition, { kind: "toml" }>,
  scope: AgentScope,
  path: string,
  input: EntryInput,
  masked: EntryInput,
  carriesSecret: boolean,
  context: PathContext,
): ConfigurePlan {
  const exists = existsSync(path);
  const current = exists ? readFileSync(path, "utf-8") : "";
  const section = agent.buildSection(input);
  // Rewriting TOML means parsing it, and a half-understood parse would silently
  // drop the user's comments and unrelated tables. Appending a table the file
  // does not have is safe; anything else is handed back as a manual step.
  const alreadyPresent = new RegExp(`^\\s*\\[mcp_servers\\.${SERVER_NAME}\\]`, "m").test(current);

  return {
    agent,
    scope,
    path,
    action: alreadyPresent ? "manual" : exists ? "update" : "create",
    reason: alreadyPresent
      ? `${path} already has an ${agent.section} entry, and the CLI will not rewrite it.`
      : undefined,
    preview: agent.buildSection(masked).trimEnd(),
    carriesSecret,
    write() {
      const backupPath = backup(path, exists);
      const separator = current.length === 0 || current.endsWith("\n\n") ? "" : current.endsWith("\n") ? "\n" : "\n\n";
      writeFile(path, current + separator + section, carriesSecret, context);
      return { backupPath };
    },
  };
}

function parseJsonConfig(path: string): Record<string, unknown> {
  const raw = readFileSync(path, "utf-8").trim();
  if (raw === "") return {};
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("not a JSON object");
    }
    return parsed as Record<string, unknown>;
  } catch (error) {
    throw new ConnectError(
      "Could not read the agent configuration",
      `${path} is not valid JSON (${(error as Error).message}).`,
      "Fix the file, or move it aside, and run the command again.",
    );
  }
}

function backup(path: string, exists: boolean): string | undefined {
  if (!exists) return undefined;
  const backupPath = path + BACKUP_SUFFIX;
  copyFileSync(path, backupPath);
  return backupPath;
}

function writeFile(
  path: string,
  contents: string,
  carriesSecret: boolean,
  context: PathContext,
): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, contents, "utf-8");
  if (carriesSecret && context.platform !== "win32") {
    // The file now holds an API key; nobody else on the box needs to read it.
    try {
      chmodSync(path, 0o600);
    } catch {
      // A filesystem without POSIX modes is not a reason to fail the connection.
    }
  }
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null) return false;
  if (typeof a !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const left = a as Record<string, unknown>;
  const right = b as Record<string, unknown>;
  const keys = Object.keys(left);
  if (keys.length !== Object.keys(right).length) return false;
  return keys.every((key) => deepEqual(left[key], right[key]));
}

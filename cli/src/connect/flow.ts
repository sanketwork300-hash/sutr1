import type { Config } from "../config.js";
import type { TerminalUi } from "../ui/terminal.js";
import {
  agentIds,
  findAgent,
  type AgentDefinition,
  type AgentScope,
  type PathContext,
} from "./agents.js";
import {
  authenticateSession,
  checkConfiguration,
  countIntegrations,
  loadTools,
  summarizePolicies,
  verifyMcpEndpoint,
  type PolicySummary,
} from "./checks.js";
import { planAgentConfiguration, type ConfigurePlan } from "./configure.js";
import { ConnectCancelled, ConnectError } from "./errors.js";
import { SKILLS_INSTALL_COMMAND, SKILLS_REPOSITORY } from "./skills.js";

/**
 * The fixed part of the progress list. The width is exported so the command can
 * tell the UI how far to pad, and the details line up into a column.
 */
export const STEP_LABELS = {
  configuration: "Checking configuration",
  authentication: "Authenticating session",
  endpoint: "Verifying the MCP endpoint",
  tools: "Loading available tools",
  policies: "Reading access policies",
  configured: "Agent already configured",
  planning: "Planning agent configuration",
  writing: "Writing agent configuration",
  updating: "Updating agent configuration",
} as const;

export const STEP_LABEL_WIDTH = Math.max(
  ...Object.values(STEP_LABELS).map((label) => label.length),
);

export interface ConnectOptions {
  agent?: string;
  scope?: AgentScope;
  timeoutMs: number;
  /** Embed the stored API key in the agent's config file. */
  writeApiKey: boolean;
  dryRun: boolean;
  assumeYes: boolean;
}

export interface ConnectDeps {
  config: Config;
  ui: TerminalUi;
  context: PathContext;
  /** False when nothing can answer a prompt (pipes, CI, `-o json` in a script). */
  interactive: boolean;
  confirm: (question: string) => Promise<boolean>;
}

export interface ConnectResult {
  url: string;
  mcp_endpoint: string;
  auth_mode: string;
  /** Email for a user credential; the masked key for an API key. Never a secret. */
  account: string;
  account_scope: "user" | "organization";
  agent: string | null;
  agent_name: string | null;
  /** This command wires an agent up over MCP; the CLI itself is the other route. */
  connection_method: "mcp";
  config_path: string | null;
  /** What happened to the agent's config: created, updated, unchanged, manual, planned. */
  config_action: string | null;
  config_backup: string | null;
  /** The entry to add by hand when the CLI declined to edit the file. Redacted. */
  config_snippet: string | null;
  restart_required: boolean;
  restart_hint: string | null;
  integrations: number;
  tools: number;
  policies: PolicySummary;
  skills_command: string;
  skills_repository: string;
}

/**
 * The connect flow. Each progress line below wraps exactly one real operation:
 * a local check, an authenticated request, an endpoint probe, a file write.
 * Nothing is announced before it has happened, and no step sleeps to look busy.
 */
export async function runConnect(
  options: ConnectOptions,
  deps: ConnectDeps,
): Promise<ConnectResult> {
  const { ui, config } = deps;
  const target = resolveTarget(options);

  ui.banner(["Sutr", "Secure tool access for AI agents"]);
  ui.intro(
    target
      ? `Connecting ${target.agent.name} to Sutr...`
      : "Connecting your agent to Sutr...",
  );

  const configuration = await ui.step(
    STEP_LABELS.configuration,
    async () => checkConfiguration(config),
    (value) => value.url,
  );

  const session = await ui.step(
    STEP_LABELS.authentication,
    () => authenticateSession(config, options.timeoutMs),
    (value) =>
      value.scope === "user" ? value.account : `organization credential ${value.account}`,
  );

  await ui.step(
    STEP_LABELS.endpoint,
    () => verifyMcpEndpoint(config, options.timeoutMs),
    () => configuration.mcpEndpoint,
  );

  const tools = await ui.step(
    STEP_LABELS.tools,
    () => loadTools(config, options.timeoutMs),
    (value) =>
      `${value.length} ${value.length === 1 ? "tool" : "tools"} across ${countIntegrations(value)} ${
        countIntegrations(value) === 1 ? "integration" : "integrations"
      }`,
  );

  const policies = await ui.step(
    STEP_LABELS.policies,
    async () => summarizePolicies(tools),
    (value) =>
      `${value.allow} auto-approve · ${value.require_approval} need approval · ${value.deny} denied`,
  );

  let plan: ConfigurePlan | null = null;
  let backupPath: string | undefined;
  let action: string | null = null;

  if (target) {
    plan = planAgentConfiguration(
      target.agent,
      target.scope,
      {
        endpoint: configuration.mcpEndpoint,
        apiKey: options.writeApiKey ? config.api_key : null,
      },
      deps.context,
    );
    action = plan.action;

    if (plan.action === "manual") {
      ui.warn(plan.reason ?? `${plan.path} needs to be edited by hand.`);
    } else if (plan.action === "unchanged") {
      await ui.step(STEP_LABELS.configured, async () => undefined, () => plan!.path);
    } else if (options.dryRun) {
      action = "planned";
      await ui.step(
        STEP_LABELS.planning,
        async () => undefined,
        () => `${plan!.path} (dry run, nothing written)`,
      );
    } else {
      await confirmChange(plan, options, deps);
      const outcome = await ui.step(
        plan.action === "create" ? STEP_LABELS.writing : STEP_LABELS.updating,
        async () => plan!.write(),
        () => plan!.path,
      );
      backupPath = outcome.backupPath;
    }
  }

  if (tools.length === 0) {
    ui.warn(
      "No tools are reachable yet — install an integration with `sutr integrations add <id>`.",
    );
  }

  return {
    url: configuration.url,
    mcp_endpoint: configuration.mcpEndpoint,
    auth_mode: configuration.authMode,
    account: session.account,
    account_scope: session.scope,
    agent: target?.agent.id ?? null,
    agent_name: target?.agent.name ?? null,
    connection_method: "mcp",
    config_path: plan?.path ?? null,
    config_action: action,
    config_backup: backupPath ?? null,
    config_snippet: plan?.action === "manual" ? plan.preview : null,
    restart_required: action === "create" || action === "update",
    restart_hint: target?.agent.restart ?? null,
    integrations: countIntegrations(tools),
    tools: tools.length,
    policies,
    skills_command: SKILLS_INSTALL_COMMAND,
    skills_repository: SKILLS_REPOSITORY,
  };
}

interface Target {
  agent: AgentDefinition;
  scope: AgentScope;
}

/** Resolve the agent and scope before any network call, so a typo fails fast. */
function resolveTarget(options: ConnectOptions): Target | null {
  if (!options.agent) return null;

  const agent = findAgent(options.agent);
  if (!agent) {
    throw new ConnectError(
      "Unknown agent",
      `'${options.agent}' is not an agent this CLI can configure.`,
      `Supported agents: ${agentIds().join(", ")}. Run \`sutr connect\` with no agent to print the setup manually.`,
    );
  }

  const scope = options.scope ?? agent.defaultScope;
  if (!agent.scopes.includes(scope)) {
    throw new ConnectError(
      "Unsupported scope",
      `${agent.name} has no ${scope}-level MCP configuration.`,
      `Use --scope ${agent.scopes.join(" or --scope ")}.`,
    );
  }
  return { agent, scope };
}

async function confirmChange(
  plan: ConfigurePlan,
  options: ConnectOptions,
  deps: ConnectDeps,
): Promise<void> {
  if (options.assumeYes) return;

  if (!deps.interactive) {
    throw new ConnectError(
      "Refusing to change a configuration file unattended",
      `${plan.path} would be ${plan.action === "create" ? "created" : "modified"}, and nothing here can answer a prompt.`,
      "Re-run with --yes to apply it, or --dry-run to see the change first.",
    );
  }

  const { ui } = deps;
  ui.heading(`${plan.action === "create" ? "Create" : "Update"} ${plan.path}`);
  ui.line();
  for (const line of plan.preview.split("\n")) ui.line(line);
  ui.line();
  if (plan.carriesSecret) {
    ui.warn("This writes your API key into that file. Do not commit it.");
  }

  const accepted = await deps.confirm("Apply this change? [y/N] ");
  if (!accepted) throw new ConnectCancelled("No changes were made.");
  ui.line();
}

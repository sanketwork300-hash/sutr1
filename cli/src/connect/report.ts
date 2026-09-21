import type { TerminalUi } from "../ui/terminal.js";
import { agentIds } from "./agents.js";
import type { ConnectResult } from "./flow.js";

/**
 * The human-readable summary of a finished connection.
 *
 * Everything printed here comes from the result of an operation that actually
 * ran — the account the server confirmed, the file that was written, the tools
 * that were listed. No credential is ever rendered.
 */
export function reportSuccess(ui: TerminalUi, result: ConnectResult): void {
  ui.ready("Sutr is ready.");
  ui.line();
  ui.info(
    result.tools > 0
      ? `Your agent can reach ${result.tools} ${result.tools === 1 ? "tool" : "tools"} across ${result.integrations} ${result.integrations === 1 ? "integration" : "integrations"},`
      : "Your agent is connected,",
  );
  ui.info("with your approval policies enforced on every call.");
  ui.line();

  ui.detail(
    result.account_scope === "user" ? "Account" : "Credential",
    result.account,
  );
  ui.detail("MCP endpoint", result.mcp_endpoint);
  if (result.agent_name) {
    ui.detail("Agent", `${result.agent_name} (${result.connection_method.toUpperCase()})`);
  }
  if (result.config_path) {
    ui.detail("Config file", `${result.config_path} (${describeAction(result.config_action)})`);
  }
  if (result.config_backup) {
    ui.detail("Previous copy", result.config_backup);
  }

  if (result.config_action === "manual" && result.config_snippet) {
    ui.line();
    ui.info("Add this yourself, or remove the existing entry and re-run:");
    ui.line();
    for (const line of result.config_snippet.split("\n")) ui.info(ui.theme.dim(line));
  }

  if (result.restart_required && result.restart_hint) {
    ui.line();
    ui.info(result.restart_hint);
  }

  if (!result.agent) {
    printManualSetup(ui, result);
  }

  ui.heading("Next step:");
  ui.info("Install Sutr Skills so your agent knows how to use Sutr:");
  ui.line();
  ui.command(result.skills_command);
  ui.line();
  ui.info(ui.theme.dim(result.skills_repository));
  ui.line();
}

/**
 * With no agent named, the CLI has verified the connection but configured
 * nothing — so it prints what to paste, and says exactly that.
 */
function printManualSetup(ui: TerminalUi, result: ConnectResult): void {
  ui.heading("Connect an agent:");
  ui.info("Nothing was configured — point your client at the endpoint above,");
  ui.info("or let the CLI write the entry for you:");
  ui.line();
  ui.command(`sutr connect <${agentIds().join("|")}>`);
  ui.line();
  ui.info("Most clients take an entry shaped like this:");
  ui.line();
  const snippet = JSON.stringify(
    {
      mcpServers: {
        sutr: { type: "http", url: result.mcp_endpoint, headers: { "X-API-Key": "ap_..." } },
      },
    },
    null,
    2,
  );
  for (const line of snippet.split("\n")) ui.info(ui.theme.dim(line));
}

function describeAction(action: string | null): string {
  switch (action) {
    case "create":
      return "created";
    case "update":
      return "updated";
    case "unchanged":
      return "already up to date";
    case "planned":
      return "dry run — not written";
    case "manual":
      return "needs a manual edit";
    default:
      return "unchanged";
  }
}

import { Command } from "commander";
import { authCommand } from "./commands/auth.js";
import { connectCommand } from "./commands/connect.js";
import { connectionsCommand } from "./commands/connections.js";
import { deployCommand } from "./commands/deploy.js";
import { integrationsCommand } from "./commands/integrations.js";
import { marketplaceCommand } from "./commands/marketplace.js";
import { openapiCommand } from "./commands/openapi.js";
import { outputCommand } from "./commands/output.js";
import { quotaCommand } from "./commands/quota.js";
import { toolsCommand } from "./commands/tools.js";
import { usageCommand } from "./commands/usage.js";
import { SKILLS_INSTALL_COMMAND } from "./connect/skills.js";

declare const __VERSION__: string;

/**
 * Assembling the program is separate from running it so tests can inspect the
 * command surface without the process parsing `process.argv` on import.
 */
export function createProgram(): Command {
  const program = new Command();

  program
    .name("sutr")
    .description("Sutr CLI — manage integrations and call tools")
    .version(typeof __VERSION__ === "string" ? __VERSION__ : "0.0.0-dev");

  program.addCommand(authCommand);
  program.addCommand(connectCommand);
  program.addCommand(connectionsCommand);
  program.addCommand(deployCommand);
  program.addCommand(integrationsCommand);
  program.addCommand(marketplaceCommand);
  program.addCommand(openapiCommand);
  program.addCommand(outputCommand);
  program.addCommand(quotaCommand);
  program.addCommand(toolsCommand);
  program.addCommand(usageCommand);

  program.addHelpText(
    "after",
    [
      "",
      "Getting started:",
      "  sutr auth login --api-key ap_...   Authenticate this machine",
      "  sutr connect claude-code           Connect an agent to Sutr over MCP",
      "  sutr connect --list                Agents this CLI can configure",
      "",
      "Sutr Skills (installed separately) teach an agent how to use Sutr:",
      `  ${SKILLS_INSTALL_COMMAND}`,
      "",
    ].join("\n"),
  );

  return program;
}

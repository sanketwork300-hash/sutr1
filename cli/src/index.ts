import { Command } from "commander";
import { authCommand } from "./commands/auth.js";
import { connectionsCommand } from "./commands/connections.js";
import { deployCommand } from "./commands/deploy.js";
import { integrationsCommand } from "./commands/integrations.js";
import { openapiCommand } from "./commands/openapi.js";
import { outputCommand } from "./commands/output.js";
import { toolsCommand } from "./commands/tools.js";
import { usageCommand } from "./commands/usage.js";

declare const __VERSION__: string;

const program = new Command();

program
  .name("sutr")
  .description("Sutr CLI — manage integrations and call tools")
  .version(__VERSION__);

program.addCommand(authCommand);
program.addCommand(connectionsCommand);
program.addCommand(deployCommand);
program.addCommand(integrationsCommand);
program.addCommand(openapiCommand);
program.addCommand(outputCommand);
program.addCommand(toolsCommand);
program.addCommand(usageCommand);

program.parse();

import { homedir } from "node:os";
import { Command } from "commander";
import { readConfig } from "../config.js";
import { print, printError, resolveFormat, type OutputFormat } from "../output.js";
import { promptConfirm } from "../prompt.js";
import { createTerminalUi, type TerminalUi } from "../ui/terminal.js";
import { AGENTS } from "../connect/agents.js";
import { ConnectCancelled, ConnectError } from "../connect/errors.js";
import { runConnect, STEP_LABEL_WIDTH, type ConnectResult } from "../connect/flow.js";
import { reportSuccess } from "../connect/report.js";
import { SKILLS_INSTALL_COMMAND } from "../connect/skills.js";

/** ctrl-c during a spinner. 128 + SIGINT, the shell convention. */
const EXIT_INTERRUPTED = 130;

interface ConnectCliOptions {
  output?: string;
  scope?: string;
  apiKey: boolean;
  animation: boolean;
  quiet?: boolean;
  yes?: boolean;
  dryRun?: boolean;
  timeout: string;
  list?: boolean;
}

export const connectCommand = new Command("connect")
  .description("Connect an AI agent to Sutr over MCP")
  .argument(
    "[agent]",
    `Agent to configure: ${AGENTS.map((a) => a.id).join(", ")}. Omit to verify the connection and print the setup.`,
  )
  .option("--list", "List the agents this CLI can configure and where each keeps its config")
  .option("--scope <scope>", "Write the config at 'user' or 'project' level")
  .option("--no-api-key", "Leave the API key out of the agent config (client authenticates itself)")
  .option("--no-animation", "Disable spinners and in-place redrawing")
  .option("--quiet", "Only print warnings, failures, and machine-readable output")
  .option("-y, --yes", "Apply the configuration change without asking")
  .option("--dry-run", "Show the change that would be made, without writing it")
  .option("--timeout <seconds>", "Per-request timeout", "20")
  .option("-o, --output <format>", "Output format")
  .action(async (agent: string | undefined, opts: ConnectCliOptions) => {
    const format = resolveFormat(opts.output);

    if (opts.list) {
      listAgents(format);
      return;
    }

    const timeoutSeconds = Number.parseInt(opts.timeout, 10);
    if (!Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0) {
      printError("--timeout must be a positive integer number of seconds.", 1);
    }
    if (opts.scope !== undefined && opts.scope !== "user" && opts.scope !== "project") {
      printError("--scope must be 'user' or 'project'.", 1);
    }

    // Human output owns stdout; structured output owns it instead, and the
    // progress display moves to stderr so a piped `-o json` stays parseable.
    const stream = format === "human" ? process.stdout : process.stderr;
    const ui = createTerminalUi({
      stream,
      errorStream: process.stderr,
      noAnimation: !opts.animation,
      quiet: opts.quiet,
      stepLabelWidth: STEP_LABEL_WIDTH,
    });

    const onInterrupt = () => {
      ui.stop();
      process.stderr.write("\n  Cancelled. Nothing was changed.\n");
      process.exit(EXIT_INTERRUPTED);
    };
    process.on("SIGINT", onInterrupt);

    try {
      const result = await runConnect(
        {
          agent,
          scope: opts.scope as "user" | "project" | undefined,
          timeoutMs: timeoutSeconds * 1000,
          writeApiKey: opts.apiKey,
          dryRun: Boolean(opts.dryRun),
          assumeYes: Boolean(opts.yes),
        },
        {
          config: readConfig(),
          ui,
          context: {
            home: homedir(),
            cwd: process.cwd(),
            env: process.env,
            platform: process.platform,
          },
          interactive: process.stdin.isTTY === true && stream.isTTY === true,
          confirm: promptConfirm,
        },
      );

      if (format === "human") {
        if (!opts.quiet) reportSuccess(ui, result);
      } else {
        print(result, format);
      }
    } catch (error) {
      reportFailure(error, ui, format);
    } finally {
      ui.stop();
      process.off("SIGINT", onInterrupt);
    }
  });

/**
 * Set the exit code rather than calling `process.exit`, so the last bytes are
 * still flushed when stdout is a pipe — which is exactly the case where the
 * caller is parsing them.
 */
function reportFailure(error: unknown, ui: TerminalUi, format: OutputFormat): void {
  if (error instanceof ConnectCancelled) {
    ui.stop();
    process.stderr.write(`\n  ${error.message}\n`);
    process.exitCode = EXIT_INTERRUPTED;
    return;
  }

  const failure =
    error instanceof ConnectError
      ? error
      : new ConnectError("Could not connect to Sutr", (error as Error)?.message ?? String(error));

  ui.failure(failure.title, { reason: failure.reason, hint: failure.hint });
  if (format !== "human") {
    print(
      { error: { title: failure.title, reason: failure.reason, hint: failure.hint ?? null } },
      format,
    );
  }
  process.exitCode = 1;
}

function listAgents(format: OutputFormat): void {
  print(
    AGENTS.map((agent) => ({
      agent: agent.id,
      name: agent.name,
      scopes: agent.scopes.join(", "),
      default_scope: agent.defaultScope,
      format: agent.kind,
    })),
    format,
  );
  if (format === "human") {
    process.stderr.write(
      `\nRun 'sutr connect <agent>' to configure one, then '${SKILLS_INSTALL_COMMAND}' to install Sutr Skills.\n`,
    );
  }
}

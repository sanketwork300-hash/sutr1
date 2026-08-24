import { Command } from "commander";
import { request } from "../client.js";
import { print, printError, resolveFormat } from "../output.js";

export const deployCommand = new Command("deploy").description(
  "Deploy generated MCP servers to local Docker, Cloud Run, Container Apps, or App Runner",
);

interface DeploymentRow {
  id: string;
  name: string;
  provider: string;
  status: string;
  url: string | null;
  health_url?: string | null;
  error: string | null;
  tool_count: number;
  env_var: string | null;
  has_token: boolean;
  created_at: string;
}

function row(d: DeploymentRow) {
  return {
    id: d.id,
    name: d.name,
    provider: d.provider,
    status: d.status,
    url: d.url ?? "-",
    tools: d.tool_count,
    error: d.error ?? "-",
  };
}

/**
 * Accumulate repeated `--config key=value` flags.
 *
 * Keys are whatever the chosen provider declares in `config_fields`, so the
 * CLI never has to learn what Cloud Run or App Runner needs; `sutr deploy
 * providers` prints the list.
 */
function collectConfig(
  pair: string,
  previous: Record<string, string>,
): Record<string, string> {
  const index = pair.indexOf("=");
  if (index < 1) {
    printError(`--config expects key=value, got "${pair}".`, 1);
  }
  return { ...previous, [pair.slice(0, index)]: pair.slice(index + 1) };
}

function splitList(value: string | undefined): string[] {
  return (value ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

deployCommand
  .command("providers")
  .description("List deployment providers and their availability")
  .option("-o, --output <format>", "Output format")
  .action(async (opts: { output: string }) => {
    const format = resolveFormat(opts.output);
    try {
      print(await request("/api/deployments/providers"), format);
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

deployCommand
  .command("list")
  .description("List deployments")
  .option("-o, --output <format>", "Output format")
  .action(async (opts: { output: string }) => {
    const format = resolveFormat(opts.output);
    try {
      const rows = await request<DeploymentRow[]>("/api/deployments");
      print(rows.map(row), format);
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

deployCommand
  .command("create")
  .description("Deploy an OpenAPI project as a running MCP server")
  .requiredOption("--project <id>", "OpenAPI project id (sutr openapi list)")
  .requiredOption("--name <name>", "Deployment name")
  .option("--provider <id>", "Deployment provider: docker, gcp, azure, aws", "docker")
  .option(
    "--connection <id>",
    "Connected cloud account authorizing the deploy (sutr connections list)",
  )
  .option(
    "--config <key=value...>",
    "Provider placement, repeatable: --config project=acme --config region=us-central1",
    collectConfig,
    {},
  )
  .option("--token <token>", "Upstream API token, injected as an env var at runtime")
  .option("--include-tags <tags>", "Only include operations with these tags (comma-separated)")
  .option("--exclude-tags <tags>", "Exclude operations with these tags (comma-separated)")
  .option("--server-url <url>", "Base URL override")
  .option("-o, --output <format>", "Output format")
  .action(
    async (opts: {
      project: string;
      name: string;
      provider: string;
      connection?: string;
      config: Record<string, string>;
      token?: string;
      includeTags?: string;
      excludeTags?: string;
      serverUrl?: string;
      output: string;
    }) => {
      const format = resolveFormat(opts.output);
      try {
        const created = await request<DeploymentRow>("/api/deployments", {
          method: "POST",
          body: {
            project_id: opts.project,
            name: opts.name,
            provider: opts.provider,
            connection_id: opts.connection ?? null,
            provider_config: opts.config,
            token: opts.token,
            compile: {
              filters: {
                include_tags: splitList(opts.includeTags),
                exclude_tags: splitList(opts.excludeTags),
              },
              server_url: opts.serverUrl,
            },
          },
        });
        print(row(created), format);
        if (format === "human") {
          console.log(
            `\nBuilding in the background. Watch it: sutr deploy status ${created.id}`,
          );
        }
      } catch (e) {
        printError((e as Error).message, 1);
      }
    },
  );

deployCommand
  .command("status <deployment_id>")
  .description("Show a deployment's live status")
  .option("-o, --output <format>", "Output format")
  .action(async (deploymentId: string, opts: { output: string }) => {
    const format = resolveFormat(opts.output);
    try {
      const d = await request<DeploymentRow>(
        `/api/deployments/${encodeURIComponent(deploymentId)}`,
      );
      print(format === "human" ? row(d) : (d as unknown as Record<string, unknown>), format);
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

deployCommand
  .command("logs <deployment_id>")
  .description("Show a deployment's container logs")
  .option("--tail <n>", "Number of lines", "100")
  .action(async (deploymentId: string, opts: { tail: string }) => {
    try {
      const result = await request<{ logs: string }>(
        `/api/deployments/${encodeURIComponent(deploymentId)}/logs`,
        { params: { tail: opts.tail } },
      );
      process.stdout.write(result.logs);
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

for (const action of ["stop", "start"] as const) {
  deployCommand
    .command(`${action} <deployment_id>`)
    .description(`${action === "stop" ? "Stop" : "Start"} a deployment`)
    .option("-o, --output <format>", "Output format")
    .action(async (deploymentId: string, opts: { output: string }) => {
      const format = resolveFormat(opts.output);
      try {
        const d = await request<DeploymentRow>(
          `/api/deployments/${encodeURIComponent(deploymentId)}/${action}`,
          { method: "POST" },
        );
        print(row(d), format);
      } catch (e) {
        printError((e as Error).message, 1);
      }
    });
}

deployCommand
  .command("delete <deployment_id>")
  .description("Delete a deployment (removes its container and image)")
  .action(async (deploymentId: string) => {
    try {
      await request(`/api/deployments/${encodeURIComponent(deploymentId)}`, {
        method: "DELETE",
      });
      console.log("Deleted.");
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

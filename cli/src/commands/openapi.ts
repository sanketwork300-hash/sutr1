import { readFileSync, writeFileSync } from "node:fs";
import { basename } from "node:path";
import { Command } from "commander";
import { buildHeaders, request } from "../client.js";
import { readConfig } from "../config.js";
import { print, printError, resolveFormat } from "../output.js";

export const openapiCommand = new Command("openapi").description(
  "Import OpenAPI specifications and compile them into tools",
);

interface ProjectRow {
  id: string;
  name: string;
  status: string;
  api_title: string;
  api_version: string;
  operation_count: number;
  integration_db_id: string | null;
  warnings: { code: string; message: string }[];
  operations?: {
    operation_id: string | null;
    method: string;
    path: string;
    summary: string | null;
    tags: string[];
    deprecated: boolean;
  }[];
  suggested_auth?: { token_header: string; token_format: string };
  servers?: { url: string }[];
}

interface CompileResult {
  tools: {
    name: string;
    method: string;
    path: string;
    renamed_from: string | null;
    param_count: number;
  }[];
  warnings: { code: string; message: string; context?: string | null }[];
  base_url: string;
  dry_run: boolean;
  integration_id?: string;
}

function projectSummary(p: ProjectRow) {
  return {
    id: p.id,
    name: p.name,
    status: p.status,
    api: `${p.api_title} ${p.api_version}`,
    operations: p.operation_count,
    integration: p.integration_db_id ? "compiled" : "-",
    warnings: p.warnings.length,
  };
}

function splitList(value: string | undefined): string[] {
  return (value ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

interface SpecCandidate {
  path: string;
  filename: string;
  size: number | null;
}

interface DiscoverResult {
  source_kind: string;
  owner: string;
  repo: string;
  branch: string | null;
  candidates: SpecCandidate[];
}

openapiCommand
  .command("discover")
  .description("List the OpenAPI spec files in a GitHub repository")
  .argument("<url>", "GitHub repository, tree, or blob URL")
  .option(
    "--github-token <token>",
    "Token for a private repository (falls back to $SUTR_GITHUB_TOKEN)",
  )
  .option(
    "--use-connection",
    "Use your connected GitHub account instead of a token (sutr connections connect github)",
  )
  .option("-o, --output <format>", "Output format")
  .action(
    async (
      url: string,
      opts: { githubToken?: string; useConnection?: boolean; output: string },
    ) => {
    const format = resolveFormat(opts.output);
    try {
      const result = await request<DiscoverResult>("/api/openapi/discover", {
        method: "POST",
        body: {
          url,
          use_connection: opts.useConnection ?? false,
          github_token: opts.githubToken ?? process.env.SUTR_GITHUB_TOKEN,
        },
      });
      print(
        result.candidates.map((c) => ({
          path: c.path,
          filename: c.filename,
          bytes: c.size ?? "-",
        })),
        format,
      );
      if (format === "human") {
        console.log(
          `\n${result.owner}/${result.repo} @ ${result.branch ?? "default branch"}` +
            `\nBest match first. Import one with:` +
            `\n  sutr openapi import --url ${url} --path ${result.candidates[0]?.path ?? "<path>"}`,
        );
      }
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

openapiCommand
  .command("import")
  .description("Import an OpenAPI 3.x document (local file, URL, GitHub, or SwaggerHub)")
  .option("--file <path>", "Path to a local spec file (JSON or YAML)")
  .option(
    "--use-connection",
    "Use your connected GitHub account instead of a token (sutr connections connect github)",
  )
  .option("--url <url>", "Spec URL, or a GitHub / SwaggerHub link (detected automatically)")
  .option("--path <path>", "For a GitHub repository: which spec file to import")
  .option(
    "--github-token <token>",
    "Token for a private repository (falls back to $SUTR_GITHUB_TOKEN)",
  )
  .option(
    "--swaggerhub-key <key>",
    "SwaggerHub API key for a private API (falls back to $SUTR_SWAGGERHUB_API_KEY)",
  )
  .option("--name <name>", "Project name (defaults to the API title)")
  .option("-o, --output <format>", "Output format")
  .action(
    async (opts: {
      file?: string;
      url?: string;
      path?: string;
      githubToken?: string;
      swaggerhubKey?: string;
      useConnection?: boolean;
      name?: string;
      output: string;
    }) => {
      const format = resolveFormat(opts.output);
      if (!opts.file && !opts.url) {
        printError("Provide --file <path> or --url <url>.", 1);
      }
      if (opts.file && opts.url) {
        printError("--file and --url are mutually exclusive.", 1);
      }
      try {
        const body: Record<string, unknown> = { name: opts.name };
        if (opts.file) {
          // "upload" rather than "paste": the project then records which file
          // it came from, which is the only provenance a local import has.
          body.source_kind = "upload";
          body.content = readFileSync(opts.file, "utf-8");
          body.filename = basename(opts.file);
        } else {
          // The server detects GitHub and SwaggerHub links and routes them
          // through the right adapter, so the CLI does not duplicate that rule.
          body.source_kind = "url";
          body.url = opts.url;
          body.path = opts.path;
          body.use_connection = opts.useConnection ?? false;
          body.github_token = opts.githubToken ?? process.env.SUTR_GITHUB_TOKEN;
          body.swaggerhub_api_key =
            opts.swaggerhubKey ?? process.env.SUTR_SWAGGERHUB_API_KEY;
        }
        const project = await request<ProjectRow>("/api/openapi/import", {
          method: "POST",
          body,
        });
        print(projectSummary(project), format);
        if (format === "human") {
          console.log(
            `\nNext: sutr openapi compile ${project.id} --dry-run   (preview the tools)`,
          );
        }
      } catch (e) {
        printError((e as Error).message, 1);
      }
    },
  );

openapiCommand
  .command("list")
  .description("List imported OpenAPI projects")
  .option("-o, --output <format>", "Output format")
  .action(async (opts: { output: string }) => {
    const format = resolveFormat(opts.output);
    try {
      const rows = await request<ProjectRow[]>("/api/openapi");
      print(rows.map(projectSummary), format);
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

openapiCommand
  .command("show <project_id>")
  .description("Show a project's operations, servers, and suggested auth")
  .option("-o, --output <format>", "Output format")
  .action(async (projectId: string, opts: { output: string }) => {
    const format = resolveFormat(opts.output);
    try {
      const project = await request<ProjectRow>(
        `/api/openapi/${encodeURIComponent(projectId)}`,
      );
      if (format === "human") {
        print(projectSummary(project), format);
        console.log("");
        print(
          (project.operations ?? []).map((op) => ({
            method: op.method,
            path: op.path,
            operation_id: op.operation_id ?? "-",
            tags: op.tags.join(","),
            deprecated: op.deprecated,
          })),
          format,
        );
      } else {
        print(project as unknown as Record<string, unknown>, format);
      }
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

openapiCommand
  .command("compile <project_id>")
  .description("Compile a project into an integration (use --dry-run to preview)")
  .option("--dry-run", "Preview the tools without creating anything")
  .option("--include-tags <tags>", "Only include operations with these tags (comma-separated)")
  .option("--exclude-tags <tags>", "Exclude operations with these tags (comma-separated)")
  .option("--exclude-paths <paths>", "Exclude these paths (comma-separated, * suffix wildcard)")
  .option("--include-deprecated", "Include operations marked deprecated")
  .option("--server-url <url>", "Base URL override (required when the spec has no servers)")
  .option("--name <name>", "Integration name (defaults to the project name)")
  .option("--auth-header <header>", "Auth header name override")
  .option("--auth-format <format>", "Auth format override, e.g. 'Bearer {token}'")
  .option("-o, --output <format>", "Output format")
  .action(
    async (
      projectId: string,
      opts: {
        dryRun?: boolean;
        includeTags?: string;
        excludeTags?: string;
        excludePaths?: string;
        includeDeprecated?: boolean;
        serverUrl?: string;
        name?: string;
        authHeader?: string;
        authFormat?: string;
        output: string;
      },
    ) => {
      const format = resolveFormat(opts.output);
      try {
        const body: Record<string, unknown> = {
          dry_run: opts.dryRun ?? false,
          filters: {
            include_tags: splitList(opts.includeTags),
            exclude_tags: splitList(opts.excludeTags),
            exclude_paths: splitList(opts.excludePaths),
            include_deprecated: opts.includeDeprecated ?? false,
          },
          server_url: opts.serverUrl,
          integration_name: opts.name,
        };
        if (opts.authHeader !== undefined || opts.authFormat !== undefined) {
          body.auth = {
            token_header: opts.authHeader ?? "",
            token_format: opts.authFormat ?? "",
          };
        }
        const result = await request<CompileResult>(
          `/api/openapi/${encodeURIComponent(projectId)}/compile`,
          { method: "POST", body },
        );
        print(
          result.tools.map((t) => ({
            name: t.name,
            endpoint: `${t.method} ${t.path}`,
            params: t.param_count,
            renamed_from: t.renamed_from ?? "-",
          })),
          format,
        );
        for (const warning of result.warnings) {
          console.error(`warning[${warning.code}]: ${warning.message}`);
        }
        if (format === "human") {
          if (result.dry_run) {
            console.log(
              `\nDry run - nothing created. Re-run without --dry-run to create the integration.`,
            );
          } else {
            console.log(
              `\nIntegration '${result.integration_id}' created (base URL ${result.base_url}).` +
                `\nConnect it: sutr integrations add ${result.integration_id}`,
            );
          }
        }
      } catch (e) {
        printError((e as Error).message, 1);
      }
    },
  );

openapiCommand
  .command("package <project_id>")
  .description("Download a standalone MCP server package (zip) for a project")
  .option("--out <path>", "Where to write the zip (defaults to the server-suggested name)")
  .option("--include-tags <tags>", "Only include operations with these tags (comma-separated)")
  .option("--exclude-tags <tags>", "Exclude operations with these tags (comma-separated)")
  .option("--exclude-paths <paths>", "Exclude these paths (comma-separated, * suffix wildcard)")
  .option("--include-deprecated", "Include operations marked deprecated")
  .option("--server-url <url>", "Base URL override")
  .option("--name <name>", "Package name (defaults to the project name)")
  .option("--auth-header <header>", "Auth header name override")
  .option("--auth-format <format>", "Auth format override, e.g. 'Bearer {token}'")
  .action(
    async (
      projectId: string,
      opts: {
        out?: string;
        includeTags?: string;
        excludeTags?: string;
        excludePaths?: string;
        includeDeprecated?: boolean;
        serverUrl?: string;
        name?: string;
        authHeader?: string;
        authFormat?: string;
      },
    ) => {
      try {
        const body: Record<string, unknown> = {
          filters: {
            include_tags: splitList(opts.includeTags),
            exclude_tags: splitList(opts.excludeTags),
            exclude_paths: splitList(opts.excludePaths),
            include_deprecated: opts.includeDeprecated ?? false,
          },
          server_url: opts.serverUrl,
          integration_name: opts.name,
        };
        if (opts.authHeader !== undefined || opts.authFormat !== undefined) {
          body.auth = {
            token_header: opts.authHeader ?? "",
            token_format: opts.authFormat ?? "",
          };
        }

        const config = readConfig();
        const baseUrl = config.url.replace(/\/+$/, "");
        const res = await fetch(
          `${baseUrl}/api/openapi/${encodeURIComponent(projectId)}/package`,
          {
            method: "POST",
            headers: buildHeaders(config, true),
            body: JSON.stringify(body),
          },
        );
        if (!res.ok) {
          const json = (await res.json().catch(() => ({}))) as { detail?: unknown };
          printError(
            typeof json.detail === "string"
              ? json.detail
              : `HTTP ${res.status}: ${res.statusText}`,
            1,
          );
        }
        const disposition = res.headers.get("content-disposition") ?? "";
        const suggested = /filename="([^"]+)"/.exec(disposition)?.[1] ?? "mcp-server.zip";
        const outPath = opts.out ?? suggested;
        writeFileSync(outPath, Buffer.from(await res.arrayBuffer()));
        console.log(`Wrote ${outPath}`);
        console.log("Unzip it, then: pip install -r requirements.txt && python server.py");
      } catch (e) {
        printError((e as Error).message, 1);
      }
    },
  );

openapiCommand
  .command("delete <project_id>")
  .description("Delete an imported project (a compiled integration lives on)")
  .action(async (projectId: string) => {
    try {
      await request(`/api/openapi/${encodeURIComponent(projectId)}`, {
        method: "DELETE",
      });
      console.log("Deleted.");
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

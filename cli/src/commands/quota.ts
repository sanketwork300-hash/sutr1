import { Command } from "commander";
import { request } from "../client.js";
import { print, printError, resolveFormat } from "../output.js";

export const quotaCommand = new Command("quota").description(
  "Inspect and set usage quotas for your organization",
);

interface Quota {
  id: string;
  kind: string;
  scope: string;
  scope_id: string;
  limit_value: number;
  enabled: boolean;
  used: number | null;
  window: string;
  enforceable?: boolean;
  note?: string;
  updated_at: string;
}

const KINDS = [
  "daily_tool_calls",
  "monthly_tool_calls",
  "concurrent_tool_calls",
  "daily_tokens",
  "monthly_tokens",
  "daily_data_transfer_bytes",
  "monthly_data_transfer_bytes",
];

const SCOPES = ["tenant", "integration", "tool"];

quotaCommand
  .command("list", { isDefault: true })
  .description("Every configured quota, with what it has consumed so far")
  .option("-o, --output <format>", "Output format")
  .action(async (opts: { output: string }) => {
    const format = resolveFormat(opts.output);
    try {
      const quotas = await request<Quota[]>("/api/quotas");
      if (format !== "human") {
        print(quotas as unknown as Record<string, unknown>[], format);
        return;
      }
      if (quotas.length === 0) {
        print("No quotas configured — nothing is limited.", format);
        return;
      }
      print(
        quotas.map((q) => ({
          kind: q.kind,
          scope: q.scope_id ? `${q.scope}:${q.scope_id}` : q.scope,
          limit: q.limit_value,
          // A quota whose metric is not recorded reports null rather than 0,
          // and says so, instead of looking like it is being enforced.
          used: q.enforceable === false ? "not enforced" : (q.used ?? "-"),
          window: q.window,
          enabled: q.enabled,
        })),
        format,
      );
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

quotaCommand
  .command("set <kind> <limit>")
  .description(`Create or replace a limit. Kinds: ${KINDS.join(", ")}`)
  .option("--scope <scope>", `One of: ${SCOPES.join(", ")}`, "tenant")
  .option(
    "--scope-id <id>",
    "Integration id, or '<integration>/<tool>' for a tool-scoped quota",
  )
  .option("--disabled", "Store the limit without enforcing it")
  .option("-o, --output <format>", "Output format")
  .action(
    async (
      kind: string,
      limit: string,
      opts: { scope: string; scopeId?: string; disabled?: boolean; output: string },
    ) => {
      const format = resolveFormat(opts.output);
      const value = Number(limit);
      if (!Number.isInteger(value) || value < 0) {
        printError("The limit must be a non-negative whole number.", 1);
      }
      try {
        const quota = await request<Quota>("/api/quotas", {
          method: "PUT",
          body: {
            kind,
            limit_value: value,
            scope: opts.scope,
            scope_id: opts.scopeId ?? "",
            enabled: !opts.disabled,
          },
        });
        print(quota as unknown as Record<string, unknown>, format);
      } catch (e) {
        printError((e as Error).message, 1);
      }
    },
  );

quotaCommand
  .command("remove <id>")
  .description("Delete a quota by id")
  .action(async (id: string) => {
    try {
      await request(`/api/quotas/${id}`, { method: "DELETE" });
      print(`Removed quota ${id}.`, "human");
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

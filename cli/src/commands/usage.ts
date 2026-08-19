import { Command } from "commander";
import { request } from "../client.js";
import { print, printError, resolveFormat } from "../output.js";

export const usageCommand = new Command("usage").description(
  "Show metered usage for your organization",
);

interface UsageSummary {
  start: string;
  end: string;
  tool_calls: number;
  totals_by_kind: { kind: string; quantity: number; events: number }[];
  tool_calls_by_outcome: { outcome: string | null; count: number }[];
  tool_calls_by_source: { source: string | null; count: number }[];
  top_tools: { integration_id: string | null; tool_name: string | null; count: number }[];
  daily: { date: string; count: number }[];
  duration_ms: { avg: number | null; max: number | null };
}

function isoDaysAgo(days: number): string {
  return new Date(Date.now() - days * 86400_000).toISOString();
}

usageCommand
  .command("summary", { isDefault: true })
  .description("Usage totals and breakdowns (default: last 30 days)")
  .option("--days <n>", "Window size in days", "30")
  .option("-o, --output <format>", "Output format")
  .action(async (opts: { days: string; output: string }) => {
    const format = resolveFormat(opts.output);
    const days = Number(opts.days);
    if (!Number.isFinite(days) || days <= 0) {
      printError("--days must be a positive number.", 1);
    }
    try {
      const summary = await request<UsageSummary>("/api/usage/summary", {
        params: { start: isoDaysAgo(days) },
      });

      if (format !== "human") {
        print(summary as unknown as Record<string, unknown>, format);
        return;
      }

      const outcomes = Object.fromEntries(
        summary.tool_calls_by_outcome.map((r) => [r.outcome ?? "unknown", r.count]),
      );
      print(
        {
          window_days: days,
          tool_calls: summary.tool_calls,
          succeeded: outcomes.executed ?? 0,
          errors: outcomes.error ?? 0,
          avg_ms: summary.duration_ms.avg ?? "-",
          max_ms: summary.duration_ms.max ?? "-",
        },
        format,
      );

      if (summary.totals_by_kind.length > 0) {
        console.log("");
        print(summary.totals_by_kind, format);
      }
      if (summary.top_tools.length > 0) {
        console.log("");
        print(
          summary.top_tools.slice(0, 10).map((r) => ({
            integration: r.integration_id ?? "-",
            tool: r.tool_name ?? "-",
            calls: r.count,
          })),
          format,
        );
      }
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

usageCommand
  .command("events")
  .description("List raw metered events")
  .option("--days <n>", "Window size in days", "7")
  .option("--kind <kind>", "Filter by kind (tool_call | deployment_runtime)")
  .option("--limit <n>", "Maximum rows", "50")
  .option("-o, --output <format>", "Output format")
  .action(async (opts: { days: string; kind?: string; limit: string; output: string }) => {
    const format = resolveFormat(opts.output);
    try {
      const params: Record<string, string> = {
        start: isoDaysAgo(Number(opts.days) || 7),
        limit: opts.limit,
      };
      if (opts.kind) params.kind = opts.kind;
      const rows = await request<Record<string, unknown>[]>("/api/usage/events", { params });
      print(rows, format);
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

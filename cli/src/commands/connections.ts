import { Command } from "commander";
import { request } from "../client.js";
import { print, printError, resolveFormat } from "../output.js";

/**
 * Connected accounts from the terminal.
 *
 * The authorization-code providers cannot be completed here — they need a
 * browser — so `connect` prints the URL and stops. The AWS device grant, on
 * the other hand, was designed for exactly this situation: it shows a code and
 * polls, which is why it is the one flow the CLI can finish end to end.
 */
export const connectionsCommand = new Command("connections").description(
  "Connect GitHub and cloud accounts sutr may act on for you",
);

interface ProviderRow {
  id: string;
  display_name: string;
  kind: string;
  flow: string;
  scopes: string;
  configured: boolean;
  reason: string | null;
  setup_url: string;
  grant_summary: string;
  connection: {
    id: string;
    account_label: string;
    expired: boolean;
    updated_at: string;
  } | null;
}

interface AuthorizeResult {
  flow: string;
  grant_summary: string;
  authorization_url?: string;
  state?: string;
  user_code?: string;
  verification_uri_complete?: string;
  interval?: number;
  expires_in?: number;
}

interface PollResult {
  status: "pending" | "connected";
  connection?: { account_label: string };
}

function summary(provider: ProviderRow) {
  return {
    provider: provider.id,
    name: provider.display_name,
    kind: provider.kind,
    status: !provider.configured
      ? "not configured"
      : provider.connection
        ? provider.connection.expired
          ? "expired"
          : "connected"
        : "not connected",
    account: provider.connection?.account_label || "-",
  };
}

connectionsCommand
  .command("list")
  .description("Show every provider, whether it is set up, and who is connected")
  .option("-o, --output <format>", "Output format")
  .action(async (opts: { output: string }) => {
    const format = resolveFormat(opts.output);
    try {
      const result = await request<{ providers: ProviderRow[] }>("/api/connections");
      print(result.providers.map(summary), format);
      if (format === "human") {
        for (const provider of result.providers) {
          if (!provider.configured) {
            console.log(`\n${provider.display_name}: ${provider.reason}`);
          }
        }
      }
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

connectionsCommand
  .command("connect <provider>")
  .description("Authorize github, gcp, azure, or aws")
  .option("--no-wait", "For aws: print the code and exit instead of polling")
  .action(async (provider: string, opts: { wait: boolean }) => {
    try {
      const result = await request<AuthorizeResult>(
        `/api/connections/${encodeURIComponent(provider)}/authorize`,
        { method: "POST", body: {} },
      );
      console.log(`\n${result.grant_summary}\n`);

      if (result.flow === "authorization_code") {
        // The redirect lands back on the sutr server, which stores the
        // connection; there is nothing further for the CLI to do.
        console.log("Open this URL in a browser to authorize:\n");
        console.log(`  ${result.authorization_url}\n`);
        console.log("Then run: sutr connections list");
        return;
      }

      console.log(`Confirmation code: ${result.user_code}`);
      console.log(`Approve it at:     ${result.verification_uri_complete}\n`);
      if (!opts.wait) {
        console.log("Then run: sutr connections list");
        return;
      }

      const interval = Math.max(result.interval ?? 5, 2) * 1000;
      const deadline = Date.now() + (result.expires_in ?? 600) * 1000;
      process.stdout.write("Waiting for approval");
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, interval));
        const poll = await request<PollResult>("/api/connections/aws/poll", {
          method: "POST",
          body: { state: result.state },
        });
        if (poll.status === "connected") {
          console.log(`\nConnected as ${poll.connection?.account_label ?? "your account"}.`);
          return;
        }
        process.stdout.write(".");
      }
      printError("\nThe authorization expired before it was approved.", 1);
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

connectionsCommand
  .command("targets <connection_id>")
  .description("List the projects, subscriptions, or accounts a connection can deploy into")
  .option("-o, --output <format>", "Output format")
  .action(async (connectionId: string, opts: { output: string }) => {
    const format = resolveFormat(opts.output);
    try {
      const result = await request<{
        provider: string;
        targets: { id: string; label: string; detail: string; roles?: string[] }[];
      }>(`/api/connections/${encodeURIComponent(connectionId)}/targets`);
      print(
        result.targets.map((target) => ({
          id: target.id,
          label: target.label,
          detail: target.detail,
          roles: target.roles?.join(", ") ?? "-",
        })),
        format,
      );
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

connectionsCommand
  .command("disconnect <connection_id>")
  .description("Delete the stored tokens for a connection")
  .action(async (connectionId: string) => {
    try {
      await request(`/api/connections/${encodeURIComponent(connectionId)}`, {
        method: "DELETE",
      });
      console.log(
        "Disconnected. The tokens are gone from sutr; revoke the app itself in the " +
          "provider's own settings if you also want the grant withdrawn.",
      );
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

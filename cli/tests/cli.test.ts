import { execFileSync, spawn, spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeAll, describe, expect, it } from "vitest";
import { createProgram } from "../src/program.js";

const CLI_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const BINARY = join(CLI_ROOT, "dist", "index.js");

/** Exercise the real binary: exit codes and stream routing are the contract. */
function run(args: string[], env: Record<string, string> = {}) {
  return spawnSync(process.execPath, [BINARY, ...args], {
    encoding: "utf-8",
    env: {
      ...process.env,
      HOME: mkdtempSync(join(tmpdir(), "sutr-cli-home-")),
      NO_COLOR: "1",
      SUTR_URL: "http://127.0.0.1:1",
      ...env,
    },
  });
}

describe("command surface", () => {
  it("keeps every existing command registered, and adds connect", () => {
    const names = createProgram()
      .commands.map((command) => command.name())
      .sort();
    expect(names).toEqual([
      "auth",
      "connect",
      "connections",
      "deploy",
      "integrations",
      "marketplace",
      "openapi",
      "output",
      "quota",
      "tools",
      "usage",
    ]);
  });

});

describe("sutr connect (end to end)", () => {
  beforeAll(() => {
    if (!existsSync(BINARY)) {
      execFileSync("pnpm", ["exec", "tsup"], { cwd: CLI_ROOT, stdio: "inherit" });
    }
  });

  it("exits 1 and explains itself when nothing is configured", () => {
    const result = run(["connect"]);
    expect(result.status).toBe(1);
    const output = result.stdout + result.stderr;
    expect(output).toContain("Not authenticated");
    expect(output).toContain("sutr auth login");
  });

  it("points at connect and the skills install command from the top-level help", () => {
    const help = run(["--help"]);
    expect(help.status).toBe(0);
    expect(help.stdout).toContain("sutr connect claude-code");
    expect(help.stdout).toContain("npx skills add sutr-dev/sutr-skills");
  });

  it("lists the agents it can configure", () => {
    const result = run(["connect", "--list"]);
    expect(result.status).toBe(0);
    expect(result.stdout).toContain("claude-code");
    expect(result.stdout).toContain("codex");
  });

  it("keeps --list machine-readable under -o json", () => {
    const result = run(["connect", "--list", "-o", "json"]);
    expect(result.status).toBe(0);
    const parsed = JSON.parse(result.stdout);
    expect(parsed.map((row: { agent: string }) => row.agent)).toContain("cursor");
  });

  it("emits a JSON error object on stdout and stays parseable when it fails", () => {
    const result = run(["connect", "-o", "json"]);
    expect(result.status).toBe(1);
    expect(JSON.parse(result.stdout).error.title).toBe("Not authenticated");
  });

  it("writes no ANSI or spinner frames when stdout is a pipe", () => {
    const result = run(["connect"]);
    expect(result.stdout + result.stderr).not.toContain("\x1b");
  });

  it("accepts --no-animation and --quiet", () => {
    const quiet = run(["connect", "--quiet", "--no-animation"]);
    expect(quiet.status).toBe(1);
    // Quiet keeps the failure: a silent non-zero exit is unactionable.
    expect(quiet.stderr).toContain("Not authenticated");
    expect(quiet.stdout).toBe("");
  });

  it("rejects a nonsense timeout before doing anything", () => {
    const result = run(["connect", "--timeout", "0"]);
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("--timeout");
  });

  it("rejects an unknown scope", () => {
    const result = run(["connect", "cursor", "--scope", "global"]);
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("--scope");
  });

  it("exits 130 and restores the terminal when interrupted mid-step", async () => {
    // A server that accepts the connection and never answers, so the CLI is
    // reliably sitting on a live step when the signal arrives.
    const hanging = createServer(() => {});
    await new Promise<void>((resolve) => hanging.listen(0, "127.0.0.1", resolve));
    const port = (hanging.address() as { port: number }).port;
    const home = mkdtempSync(join(tmpdir(), "sutr-cli-home-"));

    try {
      const login = spawnSync(
        process.execPath,
        [BINARY, "auth", "login", "--api-key", "ap_interrupt_test"],
        { encoding: "utf-8", env: { ...process.env, HOME: home, SUTR_URL: `http://127.0.0.1:${port}` } },
      );
      expect(login.status).toBe(0);

      const child = spawn(process.execPath, [BINARY, "connect"], {
        env: { ...process.env, HOME: home, SUTR_URL: `http://127.0.0.1:${port}` },
      });
      let output = "";
      child.stdout.on("data", (chunk) => (output += chunk));
      child.stderr.on("data", (chunk) => (output += chunk));

      const exit = new Promise<number | null>((resolve) => {
        child.on("exit", (code) => resolve(code));
      });
      await new Promise((resolve) => setTimeout(resolve, 600));
      child.kill("SIGINT");

      expect(await exit).toBe(130);
      expect(output).toContain("Cancelled");
      // The cursor is handed back, and no step claims an outcome it never had.
      expect(output).not.toContain("✓ Authenticating session");
    } finally {
      hanging.close();
    }
  }, 15000);

  it("still runs the pre-existing commands", () => {
    const status = run(["auth", "status", "-o", "json"]);
    expect(status.status).toBe(0);
    expect(JSON.parse(status.stdout).auth_mode).toBe("none");

    const help = run(["tools", "--help"]);
    expect(help.status).toBe(0);
    expect(help.stdout).toContain("await-approval");
  });
});

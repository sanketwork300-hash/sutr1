import { describe, expect, it } from "vitest";
import { createTerminalUi } from "../src/ui/terminal.js";

function recorder(isTTY = false) {
  const chunks: string[] = [];
  return {
    chunks,
    isTTY,
    write: (chunk: string) => chunks.push(chunk),
    text: () => chunks.join(""),
  };
}

describe("createTerminalUi", () => {
  it("draws a Unicode box when the terminal can render one", () => {
    const stream = recorder();
    const ui = createTerminalUi({ stream, env: { LANG: "en_US.UTF-8" }, platform: "linux" });
    ui.banner(["Sutr", "Secure tool access for AI agents"]);
    expect(stream.text()).toContain("╭");
    expect(stream.text()).toContain("│  Sutr");
  });

  it("falls back to ASCII when it cannot", () => {
    const stream = recorder();
    const ui = createTerminalUi({ stream, env: { LANG: "C" }, platform: "linux" });
    ui.banner(["Sutr", "Secure tool access for AI agents"]);
    expect(stream.text()).not.toContain("╭");
    expect(stream.text()).toContain("+--");
  });

  it("drops the frame on a narrow terminal rather than wrapping it", () => {
    const stream = { ...recorder(), columns: 20 };
    const ui = createTerminalUi({ stream, env: {}, platform: "linux" });
    ui.banner(["Sutr", "Secure tool access for AI agents"]);
    expect(stream.text()).not.toContain("╭");
    expect(stream.text()).toContain("Sutr");
  });

  it("emits no ANSI when NO_COLOR is set", async () => {
    const stream = recorder(true);
    const ui = createTerminalUi({
      stream,
      env: { NO_COLOR: "1" },
      platform: "linux",
      noAnimation: true,
    });
    ui.banner(["Sutr"]);
    ui.intro("Connecting your agent to Sutr...");
    await ui.step("Checking configuration", async () => "https://app.sutr.sh", (v) => v);
    ui.ready("Sutr is ready.");
    expect(stream.text()).not.toContain("\x1b");
  });

  it("suppresses progress under --quiet but still shows warnings", async () => {
    const stream = recorder();
    const ui = createTerminalUi({ stream, env: {}, platform: "linux", quiet: true });
    ui.banner(["Sutr"]);
    ui.intro("Connecting");
    const value = await ui.step("Checking configuration", async () => 42);
    ui.ready("Sutr is ready.");
    expect(value).toBe(42);
    expect(stream.text()).toBe("");

    ui.warn("No tools are reachable yet.");
    expect(stream.text()).toContain("No tools are reachable yet.");
  });

  it("routes failures to the error stream with a reason and a next step", () => {
    const stream = recorder();
    const errorStream = recorder();
    const ui = createTerminalUi({ stream, errorStream, env: {}, platform: "linux" });

    ui.failure("Could not connect to Sutr", {
      reason: "connect ECONNREFUSED 127.0.0.1:4747",
      hint: "Run `sutr auth status` to check the URL.",
    });

    expect(stream.text()).toBe("");
    expect(errorStream.text()).toContain("Could not connect to Sutr");
    expect(errorStream.text()).toContain("Reason: connect ECONNREFUSED 127.0.0.1:4747");
    expect(errorStream.text()).toContain("Next step:");
  });

  it("marks a failed step and re-throws the original error", async () => {
    const stream = recorder();
    const ui = createTerminalUi({ stream, env: { LANG: "C" }, platform: "linux" });
    const boom = new Error("HTTP 401");

    await expect(
      ui.step("Authenticating session", async () => {
        throw boom;
      }),
    ).rejects.toBe(boom);

    expect(stream.text()).toContain("x Authenticating session");
  });
});

import { describe, expect, it, vi } from "vitest";
import { Spinner } from "../src/ui/spinner.js";
import { ASCII_SYMBOLS, UNICODE_SYMBOLS } from "../src/ui/symbols.js";
import { themeFor } from "../src/ui/theme.js";

function recorder() {
  const chunks: string[] = [];
  return { chunks, write: (chunk: string) => chunks.push(chunk), text: () => chunks.join("") };
}

const THEME = themeFor(false);

describe("Spinner", () => {
  it("writes nothing until it settles when animation is off", () => {
    const stream = recorder();
    const spinner = new Spinner({ stream, symbols: ASCII_SYMBOLS, theme: THEME, animate: false });

    spinner.start("Loading available tools");
    expect(stream.chunks).toHaveLength(0);

    spinner.succeed("50 tools");
    expect(stream.text()).toBe("  + Loading available tools 50 tools\n");
  });

  it("trims the alignment padding when a step has no detail", () => {
    const stream = recorder();
    const spinner = new Spinner({ stream, symbols: ASCII_SYMBOLS, theme: THEME, animate: false });

    spinner.start("Authenticating session    ");
    spinner.fail();
    expect(stream.text()).toBe("  x Authenticating session\n");
  });

  it("animates in place and restores the cursor on success", () => {
    vi.useFakeTimers();
    const stream = recorder();
    const spinner = new Spinner({
      stream,
      symbols: UNICODE_SYMBOLS,
      theme: THEME,
      animate: true,
      intervalMs: 10,
    });

    spinner.start("Connecting");
    vi.advanceTimersByTime(25);
    const frames = stream.text();
    expect(frames).toContain("\x1b[?25l");
    expect(frames).toContain(UNICODE_SYMBOLS.spinnerFrames[1]);

    spinner.succeed();
    expect(stream.text()).toContain("\x1b[?25h");
    expect(stream.text().endsWith("  ✓ Connecting\n")).toBe(true);
    expect(spinner.isActive).toBe(false);
    vi.useRealTimers();
  });

  it("stops cleanly, and stopping twice is harmless", () => {
    vi.useFakeTimers();
    const stream = recorder();
    const spinner = new Spinner({
      stream,
      symbols: UNICODE_SYMBOLS,
      theme: THEME,
      animate: true,
      intervalMs: 10,
    });

    spinner.start("Interrupted work");
    vi.advanceTimersByTime(10);
    spinner.stop();
    const afterFirstStop = stream.text();

    spinner.stop();
    expect(stream.text()).toBe(afterFirstStop);
    expect(afterFirstStop).toContain("\x1b[?25h");
    // An interrupted step must not claim an outcome it never had.
    expect(afterFirstStop).not.toContain("✓");
    expect(afterFirstStop).not.toContain("✗");

    spinner.succeed("late");
    expect(stream.text()).toBe(afterFirstStop);
    vi.useRealTimers();
  });

  it("emits no escape sequences at all without animation", () => {
    const stream = recorder();
    const spinner = new Spinner({ stream, symbols: ASCII_SYMBOLS, theme: THEME, animate: false });
    spinner.start("Step");
    spinner.succeed("done");
    spinner.stop();
    expect(stream.text()).not.toContain("\x1b");
  });
});

import { describe, expect, it } from "vitest";
import {
  detectCapabilities,
  isCi,
  supportsColor,
  supportsUnicode,
} from "../src/ui/capabilities.js";

const TTY = { isTTY: true };
const PIPE = { isTTY: false };

describe("supportsColor", () => {
  it("is on for a terminal and off for a pipe", () => {
    expect(supportsColor(true, {})).toBe(true);
    expect(supportsColor(false, {})).toBe(false);
  });

  it("honours NO_COLOR on a terminal", () => {
    expect(supportsColor(true, { NO_COLOR: "1" })).toBe(false);
    expect(supportsColor(true, { NO_COLOR: "" })).toBe(true);
  });

  it("honours FORCE_COLOR off a terminal, but not FORCE_COLOR=0", () => {
    expect(supportsColor(false, { FORCE_COLOR: "1" })).toBe(true);
    expect(supportsColor(true, { FORCE_COLOR: "0" })).toBe(true);
    expect(supportsColor(false, { FORCE_COLOR: "0" })).toBe(false);
  });

  it("treats TERM=dumb as no colour", () => {
    expect(supportsColor(true, { TERM: "dumb" })).toBe(false);
  });
});

describe("supportsUnicode", () => {
  it("falls back to ASCII on a bare Windows console", () => {
    expect(supportsUnicode({}, "win32")).toBe(false);
    expect(supportsUnicode({ WT_SESSION: "1" }, "win32")).toBe(true);
    expect(supportsUnicode({ TERM_PROGRAM: "vscode" }, "win32")).toBe(true);
  });

  it("follows the POSIX locale when one is set", () => {
    expect(supportsUnicode({ LANG: "en_US.UTF-8" }, "linux")).toBe(true);
    expect(supportsUnicode({ LANG: "C" }, "linux")).toBe(false);
    expect(supportsUnicode({ LC_ALL: "C", LANG: "en_US.UTF-8" }, "linux")).toBe(false);
  });

  it("can be forced off with SUTR_ASCII", () => {
    expect(supportsUnicode({ LANG: "en_US.UTF-8", SUTR_ASCII: "1" }, "linux")).toBe(false);
  });
});

describe("isCi", () => {
  it("reads the usual signals, and ignores CI=false", () => {
    expect(isCi({ CI: "true" })).toBe(true);
    expect(isCi({ CI: "false" })).toBe(false);
    expect(isCi({ GITHUB_ACTIONS: "true" })).toBe(true);
    expect(isCi({})).toBe(false);
  });
});

describe("detectCapabilities", () => {
  it("animates only on an interactive terminal", () => {
    expect(detectCapabilities({ stream: TTY, env: {} }).animation).toBe(true);
    expect(detectCapabilities({ stream: PIPE, env: {} }).animation).toBe(false);
  });

  it("never animates in CI, under --no-animation, or when quiet", () => {
    expect(detectCapabilities({ stream: TTY, env: { CI: "1" } }).animation).toBe(false);
    expect(detectCapabilities({ stream: TTY, env: {}, noAnimation: true }).animation).toBe(false);
    expect(detectCapabilities({ stream: TTY, env: {}, quiet: true }).animation).toBe(false);
    expect(
      detectCapabilities({ stream: TTY, env: { SUTR_NO_ANIMATION: "1" } }).animation,
    ).toBe(false);
  });

  it("reports quiet and colour together", () => {
    const capabilities = detectCapabilities({
      stream: TTY,
      env: { NO_COLOR: "1" },
      quiet: true,
    });
    expect(capabilities).toMatchObject({ tty: true, color: false, quiet: true, animation: false });
  });
});

/**
 * What the terminal on the other end can actually do.
 *
 * Every decision the UI layer makes — colour, animation, which glyphs are safe
 * to emit — is resolved here once, from the stream and the environment, and
 * then passed around as data. Nothing downstream reads `process.env` again, so
 * a test can describe a CI box, a Windows console or a redirected pipe by
 * building the struct rather than mutating globals.
 */

export interface TerminalCapabilities {
  /** The stream is attached to a terminal. */
  tty: boolean;
  /** ANSI colour escapes are safe to emit. */
  color: boolean;
  /** Box-drawing and braille glyphs render; otherwise fall back to ASCII. */
  unicode: boolean;
  /** Redraw-in-place animation is appropriate (never in CI, pipes or logs). */
  animation: boolean;
  /** Suppress everything except warnings and failures. */
  quiet: boolean;
}

export interface CapabilityInput {
  stream: NodeJS.WriteStream | { isTTY?: boolean };
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  /** `--no-animation`, or any caller that wants static output. */
  noAnimation?: boolean;
  quiet?: boolean;
}

/** NO_COLOR is set by presence, not value — but an empty string means unset. */
function noColorRequested(env: NodeJS.ProcessEnv): boolean {
  const value = env.NO_COLOR;
  return value !== undefined && value !== "";
}

export function supportsColor(
  tty: boolean,
  env: NodeJS.ProcessEnv,
): boolean {
  if (noColorRequested(env)) return false;
  if (env.FORCE_COLOR !== undefined && env.FORCE_COLOR !== "" && env.FORCE_COLOR !== "0") {
    return true;
  }
  if (env.TERM === "dumb") return false;
  return tty;
}

export function supportsUnicode(
  env: NodeJS.ProcessEnv,
  platform: NodeJS.Platform,
): boolean {
  if (env.SUTR_ASCII !== undefined && env.SUTR_ASCII !== "" && env.SUTR_ASCII !== "0") {
    return false;
  }
  if (platform === "win32") {
    // The legacy conhost cannot draw these; Windows Terminal, VS Code and the
    // ConEmu family all announce themselves.
    return Boolean(env.WT_SESSION || env.ConEmuTask || env.TERM_PROGRAM);
  }
  const locale = env.LC_ALL || env.LC_CTYPE || env.LANG;
  if (locale) return /utf-?8/i.test(locale);
  // An unset locale on a POSIX terminal is the common case in containers,
  // where UTF-8 is still what gets rendered.
  return env.TERM !== "linux";
}

export function isCi(env: NodeJS.ProcessEnv): boolean {
  const value = env.CI;
  if (value !== undefined && value !== "" && value !== "0" && value !== "false") {
    return true;
  }
  return Boolean(env.GITHUB_ACTIONS || env.BUILDKITE || env.TEAMCITY_VERSION);
}

export function detectCapabilities(input: CapabilityInput): TerminalCapabilities {
  const env = input.env ?? process.env;
  const platform = input.platform ?? process.platform;
  const tty = input.stream.isTTY === true;
  const quiet = input.quiet === true;

  const envDisablesAnimation =
    env.SUTR_NO_ANIMATION !== undefined &&
    env.SUTR_NO_ANIMATION !== "" &&
    env.SUTR_NO_ANIMATION !== "0";

  return {
    tty,
    color: supportsColor(tty, env),
    unicode: supportsUnicode(env, platform),
    animation:
      tty && !quiet && !input.noAnimation && !envDisablesAnimation && !isCi(env),
    quiet,
  };
}

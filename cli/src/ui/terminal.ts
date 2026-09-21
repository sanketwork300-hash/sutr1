import { detectCapabilities, type TerminalCapabilities } from "./capabilities.js";
import { renderBanner } from "./banner.js";
import { Spinner, type Writable } from "./spinner.js";
import { symbolsFor, type SymbolSet } from "./symbols.js";
import { themeFor, type Theme } from "./theme.js";

export interface TerminalUiOptions {
  stream: Writable & { isTTY?: boolean; columns?: number };
  /** Where failures go. Defaults to `stream`; commands point it at stderr. */
  errorStream?: Writable;
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  noAnimation?: boolean;
  quiet?: boolean;
  /** Pad step labels to this width so their trailing details line up. */
  stepLabelWidth?: number;
}

export interface FailureDetails {
  reason: string;
  hint?: string;
}

/**
 * The presentation layer. Commands describe *what* happened — a step started, a
 * step succeeded, a run failed — and never decide how it is drawn, whether
 * colour is safe, or which stream it belongs on.
 */
export interface TerminalUi {
  readonly capabilities: TerminalCapabilities;
  readonly symbols: SymbolSet;
  readonly theme: Theme;
  banner(lines: readonly string[]): void;
  intro(text: string): void;
  /** Run one real operation behind one progress line. */
  step<T>(label: string, run: () => Promise<T>, detail?: (value: T) => string | undefined): Promise<T>;
  ready(text: string): void;
  info(text: string): void;
  warn(text: string): void;
  failure(title: string, details: FailureDetails): void;
  heading(text: string): void;
  line(text?: string): void;
  command(text: string): void;
  detail(label: string, value: string): void;
  /** Tear down any in-place drawing. Idempotent; call on exit and on SIGINT. */
  stop(): void;
}

const INDENT = "  ";

export function createTerminalUi(options: TerminalUiOptions): TerminalUi {
  const capabilities = detectCapabilities({
    stream: options.stream,
    env: options.env,
    platform: options.platform,
    noAnimation: options.noAnimation,
    quiet: options.quiet,
  });
  const symbols = symbolsFor(capabilities.unicode);
  const theme = themeFor(capabilities.color);
  const stream = options.stream;
  const errorStream = options.errorStream ?? options.stream;
  const spinner = new Spinner({
    stream,
    symbols,
    theme,
    animate: capabilities.animation,
  });

  const write = (text: string): void => {
    if (capabilities.quiet) return;
    stream.write(text);
  };

  return {
    capabilities,
    symbols,
    theme,

    banner(lines) {
      write(`\n${renderBanner(lines, symbols, theme, stream.columns)}\n\n`);
    },

    intro(text) {
      write(`${INDENT}${theme.accent(symbols.info)} ${text}\n\n`);
    },

    async step(label, run, detail) {
      if (capabilities.quiet) return await run();
      spinner.start(options.stepLabelWidth ? label.padEnd(options.stepLabelWidth) : label);
      try {
        const value = await run();
        spinner.succeed(detail?.(value));
        return value;
      } catch (error) {
        spinner.fail();
        throw error;
      }
    },

    ready(text) {
      write(`\n${INDENT}${theme.success(symbols.ready)} ${theme.bold(text)}\n`);
    },

    info(text) {
      write(`${INDENT}${text}\n`);
    },

    warn(text) {
      // Warnings survive --quiet: a silenced warning is a support ticket.
      stream.write(`${INDENT}${theme.warning(symbols.warning)} ${text}\n`);
    },

    failure(title, details) {
      spinner.stop();
      const lines = [
        "",
        `${INDENT}${theme.failure(symbols.failure)} ${theme.bold(title)}`,
        "",
        `${INDENT}${theme.bold("Reason:")} ${details.reason}`,
      ];
      if (details.hint) {
        lines.push("", `${INDENT}${theme.bold("Next step:")}`, `${INDENT}${details.hint}`);
      }
      lines.push("");
      errorStream.write(lines.join("\n") + "\n");
    },

    heading(text) {
      write(`\n${INDENT}${theme.bold(text)}\n`);
    },

    line(text = "") {
      write(text ? `${INDENT}${text}\n` : "\n");
    },

    command(text) {
      write(`${INDENT}${theme.accent(text)}\n`);
    },

    detail(label, value) {
      write(`${INDENT}${theme.dim(`${label}:`)} ${value}\n`);
    },

    stop() {
      spinner.stop();
    },
  };
}

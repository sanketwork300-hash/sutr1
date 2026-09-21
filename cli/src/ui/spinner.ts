import type { SymbolSet } from "./symbols.js";
import type { Theme } from "./theme.js";

const HIDE_CURSOR = "\x1b[?25l";
const SHOW_CURSOR = "\x1b[?25h";
const CLEAR_LINE = "\x1b[2K\r";
const FRAME_INTERVAL_MS = 80;

export interface Writable {
  write(chunk: string): unknown;
}

export interface SpinnerOptions {
  stream: Writable;
  symbols: SymbolSet;
  theme: Theme;
  /** False for pipes, CI and `--no-animation`: the line is printed once, at the end. */
  animate: boolean;
  intervalMs?: number;
}

/**
 * A single-line progress indicator that is honest about what it can do.
 *
 * With animation it redraws one line in place; without it, nothing is written
 * until the operation resolves, so a redirected log reads as a clean list of
 * outcomes rather than a smear of carriage returns. Either way the terminal is
 * left exactly as it was found — the cursor is restored even if the process is
 * interrupted mid-frame, which is what `stop()` is for.
 */
export class Spinner {
  private readonly stream: Writable;
  private readonly symbols: SymbolSet;
  private readonly theme: Theme;
  private readonly animate: boolean;
  private readonly intervalMs: number;

  private timer: NodeJS.Timeout | null = null;
  private frame = 0;
  private label = "";
  private active = false;

  constructor(options: SpinnerOptions) {
    this.stream = options.stream;
    this.symbols = options.symbols;
    this.theme = options.theme;
    this.animate = options.animate;
    this.intervalMs = options.intervalMs ?? FRAME_INTERVAL_MS;
  }

  get isActive(): boolean {
    return this.active;
  }

  start(label: string): void {
    this.stop();
    this.label = label;
    this.active = true;
    if (!this.animate) return;

    this.frame = 0;
    this.stream.write(HIDE_CURSOR);
    this.render();
    this.timer = setInterval(() => {
      this.frame = (this.frame + 1) % this.symbols.spinnerFrames.length;
      this.render();
    }, this.intervalMs);
    // Never hold the event loop open for an animation.
    this.timer.unref?.();
  }

  succeed(detail?: string): void {
    this.settle(this.theme.success(this.symbols.success), this.label, detail);
  }

  fail(detail?: string): void {
    this.settle(this.theme.failure(this.symbols.failure), this.label, detail);
  }

  /** Clear any in-place drawing and restore the cursor. Safe to call twice. */
  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (this.active && this.animate) {
      this.stream.write(CLEAR_LINE + SHOW_CURSOR);
    }
    this.active = false;
    this.label = "";
  }

  private settle(marker: string, label: string, detail?: string): void {
    const wasActive = this.active;
    this.stop();
    if (!wasActive) return;
    // Labels arrive padded so details line up; without a detail the padding is
    // just trailing whitespace in someone's log.
    const text = detail ? `${label} ${this.theme.dim(detail)}` : label.trimEnd();
    this.stream.write(`  ${marker} ${text}\n`);
  }

  private render(): void {
    const frame = this.symbols.spinnerFrames[this.frame];
    this.stream.write(`${CLEAR_LINE}  ${this.theme.accent(frame)} ${this.label}`);
  }
}

import { Chalk } from "chalk";

/**
 * Colour, resolved once. When the terminal cannot take ANSI — a pipe, a log
 * file, `NO_COLOR=1` — every function here is the identity, so call sites never
 * branch on whether colour is available.
 */
export interface Theme {
  bold: (text: string) => string;
  dim: (text: string) => string;
  success: (text: string) => string;
  failure: (text: string) => string;
  warning: (text: string) => string;
  accent: (text: string) => string;
  brand: (text: string) => string;
}

const identity = (text: string): string => text;

const PLAIN_THEME: Theme = {
  bold: identity,
  dim: identity,
  success: identity,
  failure: identity,
  warning: identity,
  accent: identity,
  brand: identity,
};

// chalk runs its own detection against stdout, which would strip the codes we
// deliberately asked for (a coloured stderr, FORCE_COLOR, a test double). The
// level is pinned so the decision stays in `capabilities.ts`.
const ansi = new Chalk({ level: 1 });

const COLOUR_THEME: Theme = {
  bold: (text) => ansi.bold(text),
  dim: (text) => ansi.dim(text),
  success: (text) => ansi.green(text),
  failure: (text) => ansi.red(text),
  warning: (text) => ansi.yellow(text),
  accent: (text) => ansi.cyan(text),
  brand: (text) => ansi.bold(ansi.cyan(text)),
};

export function themeFor(color: boolean): Theme {
  return color ? COLOUR_THEME : PLAIN_THEME;
}

/**
 * Two glyph sets for the same vocabulary. The Unicode one is the intent; the
 * ASCII one exists because a terminal that cannot render `✓` shows a replacement
 * box, and a progress list made of boxes is worse than one made of plus signs.
 */

export interface SymbolSet {
  success: string;
  failure: string;
  warning: string;
  info: string;
  ready: string;
  bullet: string;
  boxTopLeft: string;
  boxTopRight: string;
  boxBottomLeft: string;
  boxBottomRight: string;
  boxHorizontal: string;
  boxVertical: string;
  spinnerFrames: readonly string[];
}

export const UNICODE_SYMBOLS: SymbolSet = {
  success: "✓",
  failure: "✗",
  warning: "▲",
  info: "◆",
  ready: "✦",
  bullet: "•",
  boxTopLeft: "╭",
  boxTopRight: "╮",
  boxBottomLeft: "╰",
  boxBottomRight: "╯",
  boxHorizontal: "─",
  boxVertical: "│",
  spinnerFrames: ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
};

export const ASCII_SYMBOLS: SymbolSet = {
  success: "+",
  failure: "x",
  warning: "!",
  info: ">",
  ready: "*",
  bullet: "-",
  boxTopLeft: "+",
  boxTopRight: "+",
  boxBottomLeft: "+",
  boxBottomRight: "+",
  boxHorizontal: "-",
  boxVertical: "|",
  spinnerFrames: ["-", "\\", "|", "/"],
};

export function symbolsFor(unicode: boolean): SymbolSet {
  return unicode ? UNICODE_SYMBOLS : ASCII_SYMBOLS;
}

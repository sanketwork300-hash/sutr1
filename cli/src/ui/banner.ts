import type { SymbolSet } from "./symbols.js";
import type { Theme } from "./theme.js";

const PADDING = 2;
const INDENT = "  ";

/**
 * The branded box at the top of an interactive run.
 *
 * It is drawn only when it fits: a narrow terminal gets the same two lines
 * without a frame, because a box that wraps is noise rather than branding.
 */
export function renderBanner(
  lines: readonly string[],
  symbols: SymbolSet,
  theme: Theme,
  columns: number | undefined,
): string {
  const content = Math.max(...lines.map((line) => line.length));
  const width = content + PADDING * 2;

  if (columns !== undefined && columns < width + INDENT.length + 2) {
    return lines
      .map((line, index) => `${INDENT}${index === 0 ? theme.brand(line) : theme.dim(line)}`)
      .join("\n");
  }

  const horizontal = symbols.boxHorizontal.repeat(width);
  const top = theme.dim(`${symbols.boxTopLeft}${horizontal}${symbols.boxTopRight}`);
  const bottom = theme.dim(`${symbols.boxBottomLeft}${horizontal}${symbols.boxBottomRight}`);
  const edge = theme.dim(symbols.boxVertical);

  const body = lines.map((line, index) => {
    const padded = line.padEnd(content);
    const text = index === 0 ? theme.brand(padded) : theme.dim(padded);
    return `${INDENT}${edge}${" ".repeat(PADDING)}${text}${" ".repeat(PADDING)}${edge}`;
  });

  return [`${INDENT}${top}`, ...body, `${INDENT}${bottom}`].join("\n");
}

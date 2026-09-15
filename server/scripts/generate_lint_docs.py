"""Regenerate docs/pages/openapi-linting.md from the rule registry.

The rule reference is generated rather than hand-written so it cannot drift
from the rules that actually run — every finding's `documentation` field points
at an anchor on that page, and an anchor that does not exist is a broken
promise to the user reading the finding.

Run from `server/`:  uv run python scripts/generate_lint_docs.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sutr.openapi.linting import all_rules  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parents[2] / "docs" / "pages" / "openapi-linting.md"

HEADER = """# OpenAPI lint rules

Every specification imported through the MCP builder is linted. Linting is
**advisory**: structural validity against the official OpenAPI meta-schema is
what blocks an import, while lint findings tell you how good the generated
tools will be. A finding never stops you.

Findings are returned in full — all of them, every time — because fixing a
specification one error per round-trip is miserable. Each finding carries:

| Field | Meaning |
|---|---|
| `rule_id` | Stable identifier, used to suppress or track a finding |
| `severity` | `ERROR`, `WARNING`, or `INFO` |
| `message` | What is wrong, and what it means for the generated tool |
| `location` | Human-readable place, e.g. `GET /payments/{id}` |
| `json_pointer` | RFC 6901 pointer into your original document |
| `documentation` | An anchor on this page |
| `remediation` | What to change |

Rule ids follow [Spectral's](https://github.com/stoplightio/spectral) `oas`
ruleset wherever the same check exists, so findings are recognisable if you
already lint with Spectral. Rules with no Spectral equivalent carry a `sutr-`
prefix — these are checks about how well a specification converts into MCP
tools specifically.

## Severities

- **ERROR** — the specification is wrong, or the generated tool will be broken.
- **WARNING** — legal, but the generated tool will be worse than it could be.
- **INFO** — a best-practice observation with no effect on generation.

## Endpoints

```
GET  /api/openapi/lint/rules   → every rule, with severity and summary
POST /api/openapi/lint         → lint a document without importing it
```

The import and project-detail endpoints carry the same findings under
`findings` / `lint_findings`.

## Rules

"""


def main() -> None:
    lines = [HEADER]
    for group, title in (("ERROR", "Errors"), ("WARNING", "Warnings"), ("INFO", "Info")):
        entries = [r for r in all_rules() if r.severity.value == group]
        if not entries:
            continue
        lines.append(f"### {title}\n")
        for entry in entries:
            versions = ", ".join(entry.versions)
            lines.append(f"#### `{entry.id}`\n")
            lines.append(f"**{entry.severity.value}** · applies to OpenAPI {versions}\n")
            lines.append(f"{entry.summary}\n")
    lines.append(
        "\n---\n\nThis page is generated from the rule registry by "
        "`server/scripts/generate_lint_docs.py`. Edit the rules, not this file.\n"
    )
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} ({len(all_rules())} rules)")


if __name__ == "__main__":
    main()

# OpenAPI lint rules

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


### Errors

#### `no-$ref-siblings`

**ERROR** · applies to OpenAPI 3.0

$ref siblings are ignored in OpenAPI 3.0.

#### `oas3-api-servers`

**ERROR** · applies to OpenAPI 3.0, 3.1

A server URL is required to call the API.

#### `oas3-operation-security-defined`

**ERROR** · applies to OpenAPI 3.0, 3.1

Security requirements must resolve.

#### `operation-operationId-unique`

**ERROR** · applies to OpenAPI 3.0, 3.1

Duplicate operationIds collide.

#### `operation-parameters`

**ERROR** · applies to OpenAPI 3.0, 3.1

Parameters must be unique by (name, in).

#### `path-declarations-must-exist`

**ERROR** · applies to OpenAPI 3.0, 3.1

Empty path template segments are invalid.

#### `path-params`

**ERROR** · applies to OpenAPI 3.0, 3.1

Path template and declared path parameters must agree.

#### `sutr-api-key-location`

**ERROR** · applies to OpenAPI 3.0, 3.1

apiKey schemes need a name and a location.

#### `sutr-oauth2-flows`

**ERROR** · applies to OpenAPI 3.0, 3.1

OAuth2 schemes must declare their flows.

#### `sutr-path-param-required`

**ERROR** · applies to OpenAPI 3.0, 3.1

Path parameters are always required.

#### `sutr-security-scheme-type`

**ERROR** · applies to OpenAPI 3.0, 3.1

Security scheme types must be valid.

### Warnings

#### `duplicated-entry-in-enum`

**WARNING** · applies to OpenAPI 3.0, 3.1

Duplicate enum values are meaningless.

#### `info-description`

**WARNING** · applies to OpenAPI 3.0, 3.1

The API should describe itself.

#### `no-eval-in-markdown`

**WARNING** · applies to OpenAPI 3.0, 3.1

eval() in descriptions is unsafe.

#### `no-script-tags-in-markdown`

**WARNING** · applies to OpenAPI 3.0, 3.1

Script tags in descriptions are unsafe.

#### `operation-description`

**WARNING** · applies to OpenAPI 3.0, 3.1

Descriptions drive agent tool selection.

#### `operation-operationId`

**WARNING** · applies to OpenAPI 3.0, 3.1

operationId becomes the tool name.

#### `operation-operationId-valid-in-url`

**WARNING** · applies to OpenAPI 3.0, 3.1

operationIds should be URL-safe.

#### `operation-success-response`

**WARNING** · applies to OpenAPI 3.0, 3.1

An operation must describe success.

#### `operation-tag-defined`

**WARNING** · applies to OpenAPI 3.0, 3.1

Operation tags should be declared globally.

#### `path-keys-no-trailing-slash`

**WARNING** · applies to OpenAPI 3.0, 3.1

Trailing slashes create duplicate routes.

#### `sutr-parameter-schema`

**WARNING** · applies to OpenAPI 3.0, 3.1

A parameter without a schema is untyped.

#### `sutr-security-declared`

**WARNING** · applies to OpenAPI 3.0, 3.1

An API with no security is treated as public.

#### `typed-enum`

**WARNING** · applies to OpenAPI 3.0, 3.1

Enum values must match the declared type.

### Info

#### `info-contact`

**INFO** · applies to OpenAPI 3.0, 3.1

Consumers should know who owns the API.

#### `info-license`

**INFO** · applies to OpenAPI 3.0, 3.1

The API's licence should be stated.

#### `oas3-unused-component`

**INFO** · applies to OpenAPI 3.0, 3.1

Unreferenced components add noise.

#### `openapi-tags`

**INFO** · applies to OpenAPI 3.0, 3.1

Global tags let operations be grouped.

#### `operation-tags`

**INFO** · applies to OpenAPI 3.0, 3.1

Tags make selective import possible.

#### `sutr-parameter-description`

**INFO** · applies to OpenAPI 3.0, 3.1

Parameter descriptions reach the agent.

#### `tag-description`

**INFO** · applies to OpenAPI 3.0, 3.1

Declared tags should be described.


---

This page is generated from the rule registry by `server/scripts/generate_lint_docs.py`. Edit the rules, not this file.

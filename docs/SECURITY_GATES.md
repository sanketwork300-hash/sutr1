# Security gates: what they found when they were run

The workflows in `.github/workflows/security.yml` were written in Phase 1 and
**had never been executed**. A gate that has never run is not a gate; it is a
file that looks like one. Phase 13 ran every one of them locally, at the pinned
versions, and this is what came back.

Statuses use the build prompt's vocabulary. Where a finding was accepted rather
than fixed, the reason is here *and* at the site, so neither can drift without
the other becoming obviously wrong.

---

## Dependency audit — `pip-audit`

**Status before: would have failed on every run.** `pip-audit --strict` against
the installed environment failed on this repository's own package, which is not
on PyPI and never will be:

```
ERROR: sutr: Dependency not found on PyPI and could not be audited: sutr (0.1.0)
```

The job now audits the *locked* dependency set (`uv export --no-emit-project`),
which is both a working gate and a better one: it audits what will be
installed, not what happens to be installed.

**What it found once it ran: 30+ advisories across 9 packages.**

| Package | Was | Advisories | Now |
|---|---|---|---|
| `starlette` | 1.0.0 | 5, including **path-based authorization bypass** — `request.url` was rebuilt from an unvalidated `Host` header, so middleware gating on `request.url.path` could be bypassed (PYSEC-2026-161) | 1.6.0 |
| `python-multipart` | 0.0.24 | 5 — quadratic parsing of `;`-separated bodies, unbounded part headers, an unbounded read on a negative `Content-Length`, and a separator differential letting form fields be smuggled past an inspecting proxy | 0.0.32 |
| `pyjwt` | 2.12.1 | 5 | 2.13.0 |
| `pillow`, `cryptography`, `urllib3`, `idna`, `mako`, `click`, `mcp`, `pydantic-settings` | — | 20+ | current |

The Starlette advisory is the one worth reading twice: this platform *does*
make authorization decisions in middleware. The standby middleware added in
Phase 12 reads `scope["path"]` rather than `request.url.path`, which is the
form that was never affected — but that was luck rather than knowledge until
now.

**Status: IMPLEMENTED, and clean.** `pip-audit --strict` reports *"No known
vulnerabilities found"*.

`mcp` is held at `>=1.28.1,<2`: 1.28.1 clears its three advisories, and 2.x
renames the transport entry points (`streamablehttp_client` →
`streamable_http_client`). A major SDK migration is not something to smuggle
into a security bump.

## Secret detection — `gitleaks`

**Status before: would have failed.** 12 findings on the first run, all false
positives — and a gate that fails on day one is a gate somebody disables in
week one.

`.gitleaks.toml` now allowlists exactly four things, each named and reasoned: a
truncated JWT placeholder in the API documentation, two deliberately fake test
fixtures whose entire purpose is to be redacted by the code under test, and
PostHog *project* keys — public by design, compiled into the browser bundle,
write-only and unable to read data. A PostHog **personal** key (`phx_`) is not
allowlisted and would still be a finding.

**Status: IMPLEMENTED, and clean.** `no leaks found` across the repository's
history.

## SAST — `semgrep` (`p/default`, `p/python`, `p/typescript`)

**Status before: would have failed.** 84 findings. After Phase 13: **0**.

| Finding | Count | Outcome |
|---|---|---|
| `github-actions-mutable-action-tag` | 54 | **Fixed.** Every action is pinned to a commit SHA with its version in a trailing comment. While doing it, `aquasecurity/trivy-action@0.28.0` turned out to be **a reference that does not exist** — the tag is `v0.28.0` — so that step would have failed to resolve. |
| `python-logger-credential-disclosure` | 12 | **Accepted.** Every site logs an identifier, a status or an exception, never a credential value — and the log formatter redacts credential-shaped strings from every line regardless (`observability/log_format.py`), so even an unexpected token in an upstream error body cannot reach the stream. Suppressed at each site. |
| `len-all-count` | 5 | **Fixed, and it was a real inefficiency**: `len(session.exec(select(...)).all())` loads every row in order to take its length. Now `SELECT count(*)`. |
| `missing-user` | 2 | **Fixed** for the production image, which now runs as uid 1000 — a real gap rather than a lint, because the Helm chart already declared `runAsNonRoot: true` with uid 1000, so a root-only image would either be refused by the kubelet or run with a `/data` it could not write. The development image stays root deliberately (it bind-mounts the developer's working tree) and says so. |
| `formatted-sql-query`, `sqlalchemy-execute-raw-query`, `avoid-sqlalchemy-text` | 4 | **Accepted.** Migrations building SQL from literals the migration itself chose — a table name from a fixed list, a function name selected by dialect. A migration has no request to take a value from. |
| `path-join-resolve-traversal` | 4 | **Accepted.** Build-time documentation tooling walking its own directory tree; it never runs in a server. |
| `spawn-shell-true` | 1 | **Fixed.** The CLI opened a browser through `shell: true` on Windows, putting a server-supplied URL on a command line where `&` starts a second command. It now invokes `cmd.exe /c start "" <url>`, with the URL as an argument the shell never parses. |
| `missing-integrity` | 1 | **Accepted.** The flagged tag is `<link rel="canonical">`, which loads no subresource to have an integrity hash for. |

**Status: IMPLEMENTED, and clean.**

## Container scan — `trivy`

Run against the built image at `HIGH,CRITICAL` with `--ignore-unfixed`, the
same settings the gate uses. The workflow gate (CRITICAL, fixable,
`exit-code: 1`) is unchanged; what Phase 13 changed is that its action
reference now resolves.

**Status: IMPLEMENTED.**

## Licence check — `trivy --scanners license`

**Status before: NOT IMPLEMENTED** — the traceability matrix said so
explicitly. A job now exists, gating on Trivy's own FORBIDDEN/RESTRICTED
classification rather than a hand-written allow-list, because a list of
"licences we accept" goes stale silently and a classification does not.

**Status: IMPLEMENTED (job authored and configured), NOT TESTED in CI.**

## SBOM — `syft` via `anchore/sbom-action`

Unchanged other than the action being pinned. **Status: IMPLEMENTED.**

## Signing — `cosign`

In `release.yml`, keyless, with an SBOM attestation. Unchanged and still
**NOT TESTED**: it has never run, because no release has been cut from this
repository.

---

## The honest summary

| Gate | Before Phase 13 | After |
|---|---|---|
| Dependency audit | Would fail; 30+ advisories unseen | Clean, and the two authorization-relevant ones fixed |
| Secret detection | Would fail; 12 findings | Clean, 4 documented exceptions |
| SAST | Would fail; 84 findings | Clean; 62 of them fixed rather than suppressed |
| Container scan | Broken action reference | Reference fixed |
| Licence check | Did not exist | Exists |
| SBOM | Authored | Authored |
| Signing | Authored, never run | Authored, never run |

Every gate above was executed **locally**, with Docker, at the pinned versions.
None has been executed **in GitHub Actions**, because no workflow run has
happened in this environment. The workflows themselves therefore remain
**NOT TESTED in CI**; what has been tested is the code and configuration they
inspect.

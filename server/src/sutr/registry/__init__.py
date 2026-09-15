"""The Registry: the authoritative system of record for tools (LLD §3.7).

    Registry  →  events  →  Marketplace

Everything a tool *is* lives here — metadata, immutable versions, runtime
references, governance state — and nothing here is derived from anywhere else.
The marketplace is the other side of that arrangement: a projection built from
the events this service publishes, which never writes back.

Modules:

    lifecycle   the LLD's fourteen-state machine, its failure branches, and
                which transitions governance has to allow
    versions    immutable v1→v2→v3 with build hash, SBOM and manifest
    pricing     priced offers as history rather than as settings
    trust       an explainable 0–100 score over seven named inputs
    service     register, update, version, publish, deprecate, archive
    events      tool.registered/updated/deprecated/archived, version.created,
                tool.published, pricing.updated
"""

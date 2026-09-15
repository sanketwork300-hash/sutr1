"""MCP generation: IR + documentation knowledge + template → runtime artifact.

LLD §3.6. The service generates and validates; deploying what it produced is
the Runtime Manager's job, and the two are kept apart deliberately — an
artifact that has not passed validation must not be deployable, and that gate
only means something if the thing being gated is a stored, immutable artifact
rather than a zip built on the way to a deployment.

    knowledge  →┐
    IR         →┤ generate → validate → sign → store
    template   →┘

Modules:

    knowledge   documentation knowledge folded into a build, with citations
    manifest    the MCP manifest: what this artifact is, deterministically
    sbom        CycloneDX component inventory of what the package contains
    scanning    secret detection, dependency policy, optional external scanners
    signing     a detached Ed25519 signature over the build hash
    validation  the gate: compile, dependencies, MCP compliance, static
                analysis, secrets, security scan, generated tests
    artifacts   immutable storage and the determinism guarantee
    events      metadata.generated → generation.started → mcp.generated /
                generation.failed → validation.completed
    pipeline    the stages, in order, with their outcomes recorded
"""

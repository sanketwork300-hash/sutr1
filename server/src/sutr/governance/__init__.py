"""Governance, compliance and policy (ESDS LLD §5.2).

    Draft → Review → Approved → Published → Active → Deprecated → Archived

Continuous governance across both planes: *upload → validation → generation →
deployment → publication → invocation → monitoring*. Not all seven points are
wired, and `engine.describe()` says which are — a governance layer that claims
seven and covers one is worse than one that covers one and says so.

Modules:

    policies     versioned policies, their lifecycle, separation of duties
    compliance   named frameworks, evaluated against facts the platform holds
    risk         an explainable score per tool, where lower is better
    review       the six-stage approval workflow, sourced from real evidence
    exceptions   time-boxed exemptions that all expire
    engine       the governance points, and the fail-safe behaviours

The one rule the whole section rests on (§5.2): *"Governance failures never
silently permit unauthorized actions — fail closed for high-risk operations."*
Every fail-safe path here refuses or blocks; none of them shrugs and continues.
"""

"""Provisioning: identity, authorization, and scoped access passes.

LLD §2.3 names the service and its job in five words — *"Issue scoped access
passes (temp credentials)"* — and §4.3 says when: *"Issued by Provisioning
after the policy decision."* That ordering is the whole design. A pass is not a
credential somebody asks for and receives; it is the **record of a decision**,
signed and handed back.

    principal + resource → four authorization layers → decision → pass

Modules:

    identity    principal URNs, and the agent identities that had none
    rules       attribute-based rules, matched and explained
    pdp         the four layers, in order, producing a Decision
    passes      issuing, verifying and revoking scoped passes

The enforcement point is elsewhere, in `services/tool_pipeline.py`, and that
separation is the point: LLD §5.2 keeps the decision point apart from the place
that enforces it, so a decision can be asked for without acting on it — which is
what `POST /v1/provisioning/decisions` exists to do.
"""

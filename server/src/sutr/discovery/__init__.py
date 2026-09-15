"""Discovery: natural-language intent → a ranked, policy-filtered tool.

LLD §3.8. *"Read-optimized and latency-critical; it never invokes provider
APIs."* Both halves of that sentence are structural here: nothing in this
package writes a table, and nothing in it calls out to a provider.

    intent → candidates → policy filter → hybrid retrieval → ranking → cache

The order matters and is the LLD's: **the policy filter runs before ranking**.
Ranking a tool the caller may not use and then dropping it wastes the expensive
half of the request on an answer that was never available.

Modules:

    corpus      what is discoverable, and the text each candidate is indexed by
    policy      the eight pre-filter checks, versioned, each with a reason
    retrieval   keyword + vector + graph, fused by reciprocal rank
    ranking     the configurable, versioned ranking function
    cache       tenant + intent + policy version, invalidated by events
    quality     precision / recall / NDCG against supplied judgements
    service     the stages, the latency budget, and the four degradations
"""

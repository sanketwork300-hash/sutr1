# Custom API review — resolution status

Original review of the custom-API feature (stored token reuse, dispatch-time URL
safety, and update semantics). **All four findings are resolved and locked down
by regression tests** in `server/tests/test_security/test_review_findings.py`,
verified 2026-08-20 (Sutr Phase 13).

---

- **[P1] Keep stored test tokens on the saved endpoint** — `api/custom_api.py`
  **Resolved.** `/custom-api/test` injects the stored token only when the tested
  target matches the saved connection on all three of `base_url`,
  `token_header`, and `token_format` (`same_target`). Swapping `base_url` to an
  attacker-controlled domain while omitting `token` now sends no credential.
  Tests: `test_p1_stored_token_is_not_leaked_to_a_swapped_base_url` (secret
  absent from the dispatched headers) and
  `test_p1_stored_token_is_used_for_the_matching_target` (the legitimate case
  still authenticates — the mitigation did not just break the feature).

- **[P1] Revalidate custom API targets when dispatching** — `api_client.py`
  **Resolved for the check; DNS pinning remains a documented limitation.**
  `dispatch_api_tool` re-runs `validate_safe_url` on the fully-constructed URL
  immediately before connecting, so a hostname that passed validation at save
  time and later resolves to loopback/RFC1918/metadata is refused at call time.
  Test: `test_p1_dispatch_revalidates_the_full_url` asserts no connection is
  attempted and an error result is returned.
  *Still open:* the validated address is not pinned for the actual connection,
  so a rebind inside the window between our resolution and httpx's own remains
  theoretically possible. Closing it needs connect-to-IP with SNI preserved;
  tracked as a known limitation rather than silently claimed fixed.

- **[P2] Validate auth changes after merging both fields** — `api/custom_api.py`
  **Resolved.** The PATCH handler computes the final `(header, format)` pair and
  validates it once, so token-auth ⇄ no-auth transitions succeed. Tests cover
  both directions plus `test_p2_an_invalid_merged_pair_is_still_rejected`, so
  the fix did not turn into permissiveness.

- **[P3] Allow clearing custom API descriptions** — `api/custom_api.py`
  **Resolved in Phase 13** (this was still open when the phase began). The
  handler now distinguishes an absent field from an explicit `null` via
  pydantic's `model_fields_set`, so the builder's `description: null` clears the
  stored text while an omitted field leaves it untouched. Tests cover clear,
  omit, and replace.

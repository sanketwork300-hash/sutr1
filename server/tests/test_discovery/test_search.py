"""Discovery: intent in, ranked and policy-filtered tools out."""

from sutr.discovery import corpus, policy, service
from sutr.discovery.policy import Requirements
from sutr.marketplace import subscriptions
from sutr.registry import service as registry_service

from .conftest import drain, publish, register


async def _search(session, org_id, intent, **kwargs):
    return await service.search(session, org_id=org_id, intent=intent, **kwargs)


# ── The corpus ───────────────────────────────────────────────────────────────


async def test_a_published_tool_from_another_tenant_is_discoverable(
    session, test_org, published_tools
):
    result = await _search(session, test_org.id, "refund a customer payment")
    assert [entry.candidate.tool_key for entry in result.results][0] == "refunds-api"
    assert result.results[0].candidate.source == "marketplace"


async def test_your_own_draft_is_discoverable_to_you_and_to_nobody_else(
    session, test_org, provider_org
):
    draft = register(session, provider_org.id, tool_key="secret-api", name="Secret API")
    drain()

    mine = await _search(session, provider_org.id, "secret", use_cache=False)
    assert [entry.candidate.tool_key for entry in mine.results] == ["secret-api"]

    theirs = await _search(session, test_org.id, "secret", use_cache=False)
    assert theirs.results == []
    assert draft.lifecycle_state == "DRAFT"


async def test_your_own_record_wins_over_its_listing(session, provider_org, published_tools):
    """The record is authoritative; the projection may be an event behind."""
    candidates = corpus.collect(session, org_id=provider_org.id)
    refunds = next(c for c in candidates if c.tool_key == "refunds-api")
    assert refunds.source == corpus.SOURCE_OWN
    assert refunds.owned is True
    # But it still picks up what only the projection carries.
    assert refunds.subscriber_count == 0


async def test_nothing_is_discoverable_before_the_projection_runs(
    session, test_org, provider_org, test_user
):
    tool = register(session, provider_org.id)
    publish(session, tool, decided_by=test_user.id)
    # Deliberately not drained.
    result = await _search(session, test_org.id, "refund", use_cache=False)
    assert result.results == []


# ── The policy filter runs first ─────────────────────────────────────────────


async def test_an_archived_tool_is_gone_for_everyone_including_its_provider(
    session, test_org, provider_org, published_tools
):
    """Archiving delists it *and* the lifecycle check refuses it.

    Two mechanisms rather than one, and deliberately so: a consumer never sees
    it because the projection delisted it, and its own provider — who still has
    the registry record in their corpus — is refused by the filter. A retired
    tool is not a recommendation, even to the tenant that retired it.
    """
    registry_service.deprecate(session, published_tools["refunds"], note="gone")
    registry_service.archive(session, published_tools["refunds"])
    session.commit()
    drain()

    consumer = await _search(session, test_org.id, "refund", use_cache=False)
    assert [e.candidate.tool_key for e in consumer.results] == []
    # Delisted, so it never became a candidate at all.
    assert consumer.filter_summary["excluded_by"] == {}

    owner = await _search(session, provider_org.id, "refund", use_cache=False)
    assert [e.candidate.tool_key for e in owner.results] == []
    assert owner.filter_summary["excluded_by"]["lifecycle"] == 1
    assert any("archived" in exclusion.reason for exclusion in owner.suggestions)


async def test_a_deprecated_tool_is_hidden_from_new_consumers_but_findable_on_request(
    session, test_org, provider_org, published_tools
):
    registry_service.deprecate(session, published_tools["refunds"], note="Use v2.")
    session.commit()
    drain()

    hidden = await _search(session, test_org.id, "refund", use_cache=False)
    assert hidden.results == []

    asked = await _search(
        session,
        test_org.id,
        "refund",
        requirements=Requirements(include_deprecated=True),
        use_cache=False,
    )
    assert [e.candidate.tool_key for e in asked.results] == ["refunds-api"]


async def test_an_existing_subscriber_still_finds_a_deprecated_tool(
    session, test_org, provider_org, published_tools
):
    """Deprecation stops new adoption; it does not hide the tool from its users."""
    subscription = subscriptions.subscribe(
        session, org_id=test_org.id, tool=published_tools["refunds"]
    )
    subscriptions.provision(session, subscription)
    registry_service.deprecate(session, published_tools["refunds"], note="Use v2.")
    session.commit()
    drain()

    result = await _search(session, test_org.id, "refund", use_cache=False)
    assert [e.candidate.tool_key for e in result.results] == ["refunds-api"]


async def test_region_filters_against_the_providers_declaration(session, test_org, published_tools):
    inside = await _search(
        session,
        test_org.id,
        "shipping",
        requirements=Requirements(region="us-east-1"),
        use_cache=False,
    )
    assert [e.candidate.tool_key for e in inside.results] == ["shipping-api"]

    outside = await _search(
        session,
        test_org.id,
        "shipping",
        requirements=Requirements(region="eu-west-1"),
        use_cache=False,
    )
    assert outside.results == []
    assert outside.suggestions[0].check == "region"
    assert "us-east-1" in outside.suggestions[0].reason


async def test_a_tool_declaring_no_region_is_not_filtered_by_region(
    session, test_org, published_tools
):
    """An empty declaration is no claim, not a claim of nowhere."""
    result = await _search(
        session,
        test_org.id,
        "refund",
        requirements=Requirements(region="eu-west-1"),
        use_cache=False,
    )
    assert [e.candidate.tool_key for e in result.results] == ["refunds-api"]


async def test_compliance_requires_every_regime_asked_for(session, test_org, published_tools):
    ok = await _search(
        session,
        test_org.id,
        "shipping",
        requirements=Requirements(compliance=["soc2"]),
        use_cache=False,
    )
    assert [e.candidate.tool_key for e in ok.results] == ["shipping-api"]

    missing = await _search(
        session,
        test_org.id,
        "shipping",
        requirements=Requirements(compliance=["SOC2", "HIPAA"]),
        use_cache=False,
    )
    assert missing.results == []
    assert "HIPAA" in missing.suggestions[0].reason


async def test_entitled_only_excludes_what_you_have_not_subscribed_to(
    session, test_org, published_tools
):
    result = await _search(
        session,
        test_org.id,
        "refund",
        requirements=Requirements(entitled_only=True),
        use_cache=False,
    )
    assert result.results == []
    assert result.suggestions[0].check == "subscription"
    assert "not subscribed" in result.suggestions[0].reason

    subscription = subscriptions.subscribe(
        session, org_id=test_org.id, tool=published_tools["refunds"]
    )
    subscriptions.provision(session, subscription)
    session.commit()
    after = await _search(
        session,
        test_org.id,
        "refund",
        requirements=Requirements(entitled_only=True),
        use_cache=False,
    )
    assert [e.candidate.tool_key for e in after.results] == ["refunds-api"]


async def test_the_filter_runs_before_ranking(session, test_org, published_tools):
    """The stage record is the evidence: policy precedes retrieval and ranking."""
    result = await _search(session, test_org.id, "refund", use_cache=False)
    assert [stage.name for stage in result.stages] == [
        "candidates",
        "policy",
        "retrieval",
        "ranking",
    ]


async def test_declared_checks_that_cannot_run_say_so_rather_than_passing_quietly():
    described = policy.describe()
    by_name = {check["name"]: check for check in described["checks"]}
    assert by_name["provider_policy"]["enforced"] is False
    assert "NOT IMPLEMENTED" in by_name["provider_policy"]["detail"]
    assert by_name["runtime_status"]["enforced"] is False
    assert set(policy.CHECKS) == {
        "tenant",
        "subscription",
        "region",
        "compliance",
        "provider_policy",
        "runtime_status",
        "visibility",
        "lifecycle",
    }


# ── Suggestions when nothing matches (LLD §4.2) ──────────────────────────────


async def test_no_match_returns_suggestions_with_the_check_that_excluded_each(
    session, test_org, published_tools
):
    result = await _search(
        session,
        test_org.id,
        "refund",
        requirements=Requirements(entitled_only=True),
        use_cache=False,
    )
    assert result.results == []
    assert result.suggestions
    payload = result.as_dict()
    assert payload["suggestions"][0]["check"] == "subscription"
    assert "excluded by policy" in payload["suggestions_reason"]


async def test_an_intent_matching_nothing_at_all_returns_no_suggestions(
    session, test_org, published_tools
):
    """Suggestions are near misses, not the catalog."""
    result = await _search(session, test_org.id, "quantum tunnelling", use_cache=False)
    assert result.results == []
    assert result.suggestions == []


async def test_suggestions_are_absent_when_there_are_results(session, test_org, published_tools):
    result = await _search(session, test_org.id, "refund", use_cache=False)
    assert result.results
    assert result.suggestions == []

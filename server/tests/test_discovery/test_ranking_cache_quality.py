"""The ranking function, the query cache, and the quality metrics."""

import uuid
from datetime import datetime, timedelta, timezone

from sutr.discovery import cache, corpus, quality, ranking, service
from sutr.discovery.policy import POLICY_VERSION, Requirements
from sutr.models.registry_tool import PUBLISHED

from .conftest import drain

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def _candidate(**overrides) -> corpus.Candidate:
    fields = {
        "tool_id": uuid.uuid4(),
        "tool_key": "a-tool",
        "name": "A Tool",
        "summary": "",
        "description": "",
        "category": "Other",
        "tags": [],
        "regions": [],
        "compliance": [],
        "integration_id": None,
        "provider_org_id": uuid.uuid4(),
        "lifecycle_state": PUBLISHED,
        "visibility": "public",
        "trust_score": None,
        "version": 1,
        "deprecated": False,
        "deprecation_note": "",
        "subscriber_count": 0,
        "published_at": NOW,
        "source": corpus.SOURCE_LISTING,
    }
    fields.update(overrides)
    return corpus.Candidate(**fields)


# ── Ranking ──────────────────────────────────────────────────────────────────


def test_an_unscored_tool_is_ranked_as_if_trust_did_not_apply():
    """Phase 6 made an unmeasurable trust score null; ranking must not undo it."""
    unscored = _candidate(tool_key="unscored")
    scored_zero = _candidate(tool_key="scored-zero", trust_score=0)
    ranked, _version = ranking.rank(
        [unscored, scored_zero],
        relevance={unscored.tool_id: 1.0, scored_zero.tool_id: 1.0},
        now=NOW,
    )
    by_key = {entry.candidate.tool_key: entry for entry in ranked}
    assert by_key["unscored"].signals["trust"] is None
    assert "trust" not in by_key["unscored"].contributions
    assert by_key["scored-zero"].signals["trust"] == 0.0
    # Identical relevance, and the one nobody has measured is not ranked below
    # the one measured as bad.
    assert by_key["unscored"].score == by_key["scored-zero"].score


def test_trust_breaks_a_relevance_tie():
    low = _candidate(tool_key="low", trust_score=20)
    high = _candidate(tool_key="high", trust_score=90)
    ranked, _ = ranking.rank([low, high], relevance={low.tool_id: 1.0, high.tool_id: 1.0}, now=NOW)
    assert [entry.candidate.tool_key for entry in ranked] == ["high", "low"]


def test_relevance_dominates_trust_in_the_default_version():
    """A trusted tool that does not do what was asked is a worse answer."""
    relevant = _candidate(tool_key="relevant", trust_score=10)
    trusted = _candidate(tool_key="trusted", trust_score=100)
    ranked, version = ranking.rank(
        [relevant, trusted], relevance={relevant.tool_id: 1.0, trusted.tool_id: 0.2}, now=NOW
    )
    assert version.name == "balanced-1"
    assert [entry.candidate.tool_key for entry in ranked] == ["relevant", "trusted"]


def test_trust_first_reorders_the_same_inputs():
    relevant = _candidate(tool_key="relevant", trust_score=10)
    trusted = _candidate(tool_key="trusted", trust_score=100)
    ranked, _ = ranking.rank(
        [relevant, trusted],
        relevance={relevant.tool_id: 1.0, trusted.tool_id: 0.6},
        version="trust-first-1",
        now=NOW,
    )
    assert [entry.candidate.tool_key for entry in ranked] == ["trusted", "relevant"]


def test_deprecation_is_a_penalty():
    live = _candidate(tool_key="live")
    dying = _candidate(tool_key="dying", deprecated=True)
    ranked, _ = ranking.rank(
        [live, dying], relevance={live.tool_id: 1.0, dying.tool_id: 1.0}, now=NOW
    )
    assert [entry.candidate.tool_key for entry in ranked] == ["live", "dying"]
    assert ranked[1].contributions["deprecation"] < 0


def test_freshness_decays_and_stops_at_the_window():
    fresh = _candidate(tool_key="fresh", published_at=NOW)
    old = _candidate(tool_key="old", published_at=NOW - timedelta(days=ranking.FRESHNESS_DAYS + 10))
    ranked, _ = ranking.rank(
        [fresh, old], relevance={fresh.tool_id: 1.0, old.tool_id: 1.0}, now=NOW
    )
    by_key = {entry.candidate.tool_key: entry for entry in ranked}
    assert by_key["fresh"].signals["freshness"] == 1.0
    assert by_key["old"].signals["freshness"] == 0.0


def test_adoption_saturates():
    some = _candidate(tool_key="some", subscriber_count=ranking.ADOPTION_SATURATION)
    many = _candidate(tool_key="many", subscriber_count=ranking.ADOPTION_SATURATION * 100)
    ranked, _ = ranking.rank(
        [some, many], relevance={some.tool_id: 1.0, many.tool_id: 1.0}, now=NOW
    )
    assert {entry.signals["adoption"] for entry in ranked} == {1.0}


def test_every_result_carries_its_own_arithmetic():
    candidate = _candidate(trust_score=80, subscriber_count=5)
    ranked, _ = ranking.rank([candidate], relevance={candidate.tool_id: 1.0}, now=NOW)
    entry = ranked[0]
    assert abs(sum(entry.contributions.values()) - entry.score) < 1e-9
    assert "relevance" in entry.explain()
    assert entry.as_dict()["contributions"]["trust"] > 0


def test_ties_are_broken_deterministically_by_name():
    first = _candidate(tool_key="a", name="Alpha")
    second = _candidate(tool_key="b", name="Beta")
    for _ in range(3):
        ranked, _ = ranking.rank(
            [second, first], relevance={first.tool_id: 1.0, second.tool_id: 1.0}, now=NOW
        )
        assert [entry.candidate.name for entry in ranked] == ["Alpha", "Beta"]


def test_an_unknown_version_falls_back_to_the_default_rather_than_failing():
    assert ranking.get_version("nonsense").name == ranking.DEFAULT_VERSION
    assert ranking.get_version(None).name == ranking.DEFAULT_VERSION


def test_the_versions_are_described_for_a_client():
    described = ranking.describe()
    assert described["default"] == "balanced-1"
    assert described["fallback_on_timeout"] == "relevance-only-1"
    assert {version["name"] for version in described["versions"]} == set(ranking.VERSIONS)
    assert "no trust score" in described["notes"]["trust"]


# ── Cache ────────────────────────────────────────────────────────────────────


async def test_a_repeated_search_is_served_from_the_cache(session, test_org, published_tools):
    first = await service.search(session, org_id=test_org.id, intent="refund")
    second = await service.search(session, org_id=test_org.id, intent="refund")
    assert first.cached is False
    assert second.cached is True
    assert cache.stats().hits == 1


def test_the_key_is_tenant_plus_intent_plus_policy_version():
    org = uuid.uuid4()
    base = dict(
        org_id=org,
        intent="refund a payment",
        policy_version=POLICY_VERSION,
        ranking_version="balanced-1",
        requirements={},
        limit=10,
    )
    assert cache.key(**base) == cache.key(**{**base, "intent": "  Refund  A   Payment "})
    assert cache.key(**base) != cache.key(**{**base, "org_id": uuid.uuid4()})
    assert cache.key(**base) != cache.key(**{**base, "policy_version": POLICY_VERSION + 1})
    # And the two things the LLD does not name but that change the answer.
    assert cache.key(**base) != cache.key(**{**base, "ranking_version": "trust-first-1"})
    assert cache.key(**base) != cache.key(**{**base, "requirements": {"region": "eu-west-1"}})


async def test_different_requirements_are_different_answers(session, test_org, published_tools):
    wide = await service.search(session, org_id=test_org.id, intent="refund")
    narrow = await service.search(
        session,
        org_id=test_org.id,
        intent="refund",
        requirements=Requirements(entitled_only=True),
    )
    assert wide.results
    assert narrow.results == []
    assert narrow.cached is False


async def test_publishing_a_tool_invalidates_every_tenants_cache(
    session, test_org, provider_org, test_user, published_tools
):
    """The storefront is cross-tenant, so a publication changes everyone's answers."""
    from .conftest import publish, register

    await service.search(session, org_id=test_org.id, intent="invoice")
    assert (await service.search(session, org_id=test_org.id, intent="invoice")).cached is True

    newcomer = register(
        session,
        provider_org.id,
        tool_key="invoicing-plus",
        name="Invoicing Plus",
        summary="Better invoices.",
    )
    publish(session, newcomer, decided_by=test_user.id)
    drain()

    after = await service.search(session, org_id=test_org.id, intent="invoice")
    assert after.cached is False
    assert "invoicing-plus" in {e.candidate.tool_key for e in after.results}


async def test_a_policy_change_invalidates_everything(session, test_org, published_tools):
    await service.search(session, org_id=test_org.id, intent="refund")
    cache.invalidate_all()
    assert (await service.search(session, org_id=test_org.id, intent="refund")).cached is False


async def test_use_cache_false_never_reads_or_writes(session, test_org, published_tools):
    await service.search(session, org_id=test_org.id, intent="refund", use_cache=False)
    await service.search(session, org_id=test_org.id, intent="refund", use_cache=False)
    assert cache.stats().hits == 0
    assert cache.stats().entries == 0


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(cache, "MAX_ENTRIES", 3)
    org = uuid.uuid4()
    for index in range(6):
        cache.put(f"key-{index}", org, index)
    assert cache.stats().entries <= 3


def test_the_cache_describes_what_it_is():
    described = cache.describe()
    assert described["backend"] == "in-process"
    assert "tenant + intent + policy version" in described["key"]
    assert "per replica" in described["note"].lower()


# ── Quality ──────────────────────────────────────────────────────────────────


def test_precision_recall_and_ndcg_on_a_known_ordering():
    relevant = {uuid.UUID(int=1), uuid.UUID(int=2)}
    returned = [uuid.UUID(int=9), uuid.UUID(int=1), uuid.UUID(int=8), uuid.UUID(int=2)]
    assert quality.precision_at_k(returned, relevant, 4) == 0.5
    assert quality.recall_at_k(returned, relevant, 4) == 1.0
    assert quality.recall_at_k(returned, relevant, 2) == 0.5
    perfect = quality.ndcg_at_k([uuid.UUID(int=1), uuid.UUID(int=2)], relevant, 4)
    assert perfect == 1.0
    assert 0 < quality.ndcg_at_k(returned, relevant, 4) < 1


def test_ndcg_notices_where_a_hit_landed():
    relevant = {uuid.UUID(int=1)}
    early = quality.ndcg_at_k([uuid.UUID(int=1), uuid.UUID(int=9)], relevant, 5)
    late = quality.ndcg_at_k([uuid.UUID(int=9), uuid.UUID(int=1)], relevant, 5)
    assert early > late
    # Precision cannot tell them apart, which is why all three are reported.
    assert quality.precision_at_k([uuid.UUID(int=1), uuid.UUID(int=9)], relevant, 5) == (
        quality.precision_at_k([uuid.UUID(int=9), uuid.UUID(int=1)], relevant, 5)
    )


def test_recall_is_null_when_nothing_was_judged_relevant():
    """ "Found everything" and "there was nothing to find" are different results."""
    assert quality.recall_at_k([uuid.UUID(int=1)], set(), 5) is None


def test_an_evaluation_says_what_it_measured_against():
    judgement = quality.Judgement(intent="refund", relevant={uuid.UUID(int=1)})
    score = quality.score_query(judgement, [uuid.UUID(int=1)], k=5)
    payload = quality.Evaluation(k=5, queries=[score]).as_dict()
    assert payload["precision"] == 1.0
    assert "judgements supplied in this request" in payload["measured_against"]


def test_no_judgements_ship_and_the_description_says_so():
    described = quality.describe()
    assert described["judgements"]["shipped"] == 0
    assert "NOT_CONFIGURED" in described["judgements"]["reason"]

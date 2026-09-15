"""Discovery: natural-language intent → a ranked, policy-filtered recommendation.

LLD §3.8's interface. Three routes:

- `POST /v1/discovery/search` — the request itself.
- `GET /v1/discovery/capabilities` — which rankers this install has, which
  policy checks actually run, the ranking versions, and what the cache is.
- `POST /v1/discovery/evaluate` — precision, recall and NDCG against relevance
  judgements the caller supplies, because no judgements ship and metrics with
  nothing to measure against would be a fiction.

The search response always carries `degradations`, `policy` and `stages`. An
agent that gets a recommendation should be able to see what was skipped to
produce it, and a caller who gets nothing gets **suggestions** — the tools that
matched the intent and the policy check that excluded each one (LLD §4.2's
*"suggestions or 'tool not found'"*).

Nothing here writes a table, and nothing here calls a provider API.
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from sutr.authz import ensure_agent_can
from sutr.common import envelope
from sutr.common.errors import InvalidRequestError
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.discovery import cache, quality, ranking, service
from sutr.discovery.policy import Requirements

router = APIRouter(prefix="/v1/discovery", tags=["discovery"])

# Discovery is a read, and finding a tool is something every member should be
# able to do — it is how they decide what to ask for.
READ = "logs:read"

MAX_INTENT_LENGTH = 500


class SearchRequest(BaseModel):
    """The LLD §3.8 request: an intent, plus what the caller needs of a tool."""

    intent: str = Field(min_length=1, max_length=MAX_INTENT_LENGTH)
    limit: int = Field(default=10, ge=1, le=50)
    region: str | None = None
    compliance: list[str] = []
    categories: list[str] = []
    # Only tools the caller can invoke now. Off by default: discovery is how you
    # find something to subscribe *to*.
    entitled_only: bool = False
    include_deprecated: bool = False
    ranking_version: str | None = None
    # For a caller measuring the cache, or one that must not be served a
    # previous answer.
    use_cache: bool = True


class JudgementInput(BaseModel):
    intent: str = Field(min_length=1, max_length=MAX_INTENT_LENGTH)
    relevant_tool_ids: list[uuid.UUID]
    note: str = ""


class EvaluateRequest(BaseModel):
    judgements: list[JudgementInput] = Field(min_length=1, max_length=50)
    k: int = Field(default=quality.DEFAULT_K, ge=1, le=50)
    ranking_version: str | None = None


def _requirements(body: SearchRequest) -> Requirements:
    return Requirements(
        region=body.region,
        compliance=body.compliance,
        entitled_only=body.entitled_only,
        include_deprecated=body.include_deprecated,
        categories=body.categories,
    )


@router.get("/capabilities")
def capabilities(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """What this install can actually do to an intent.

    Worth reading before trusting a ranking: whether the vector ranker ran,
    which policy checks have anything to read, and whether the answer came from
    a cache are all things that change what a result means.
    """
    ensure_agent_can(session, agent_auth, READ)
    return envelope(service.describe())


@router.post("/search")
async def search(
    body: SearchRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    if body.ranking_version and body.ranking_version not in ranking.VERSIONS:
        raise InvalidRequestError(
            f"Unknown ranking version '{body.ranking_version}'. Known: "
            f"{', '.join(sorted(ranking.VERSIONS))}.",
            code="unknown_ranking_version",
        )
    result = await service.search(
        session,
        org_id=agent_auth.org.id,
        intent=body.intent,
        requirements=_requirements(body),
        ranking_version=body.ranking_version,
        limit=body.limit,
        use_cache=body.use_cache,
    )
    return envelope(result.as_dict())


@router.post("/evaluate")
async def evaluate(
    body: EvaluateRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Precision, recall and NDCG for a set of judgements you supply.

    Every judgement runs a real, uncached discovery query. The numbers describe
    the set you provided and nothing else, and the response says so.
    """
    ensure_agent_can(session, agent_auth, READ)
    scores = []
    for judgement in body.judgements:
        result = await service.search(
            session,
            org_id=agent_auth.org.id,
            intent=judgement.intent,
            ranking_version=body.ranking_version,
            limit=body.k,
            # Never cached: an evaluation that measured a previous answer would
            # be measuring the cache.
            use_cache=False,
        )
        returned = [entry.candidate.tool_id for entry in result.results]
        names = {entry.candidate.tool_id: entry.candidate.tool_key for entry in result.results}
        scores.append(
            quality.score_query(
                quality.Judgement(
                    intent=judgement.intent,
                    relevant=set(judgement.relevant_tool_ids),
                    note=judgement.note,
                ),
                returned,
                k=body.k,
                names=names,
            )
        )
    return envelope(quality.Evaluation(k=body.k, queries=scores).as_dict())


@router.post("/cache/invalidate", status_code=200)
def invalidate_cache(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Drop this tenant's cached answers.

    Registry events already invalidate the cache; this is for the case the
    events cannot see — a trust score that moved because a deployment failed or
    calls started erroring, neither of which the registry announces.
    """
    ensure_agent_can(session, agent_auth, "integrations:manage")
    cache.invalidate(agent_auth.org.id)
    return envelope({"invalidated": True, "cache": cache.describe()})

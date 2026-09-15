"""Fixtures for the discovery tests.

The corpus is built through the real registry and the real projection, so what
is tested is what a request would actually find — a hand-inserted listing would
let a broken publish path pass.
"""

import pytest
from sqlmodel import Session

from sutr.discovery import cache, retrieval
from sutr.documentation import embeddings
from sutr.events import relay
from sutr.marketplace import projection
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.user import User
from sutr.registry import service


@pytest.fixture(autouse=True)
def _discovery_globals():
    """Handlers and caches are process-wide; tests must not leak them."""
    projection.install()
    cache.install()
    cache.clear()
    retrieval.reset_vector_cache()
    embeddings.set_provider(embeddings.UnconfiguredProvider())
    yield
    projection.uninstall()
    cache.uninstall()
    cache.clear()
    retrieval.reset_vector_cache()
    embeddings.set_provider(embeddings.UnconfiguredProvider())


def drain(times: int = 4) -> int:
    total = 0
    for _ in range(times):
        moved = relay.drain_once()
        total += moved
        if not moved:
            break
    return total


def publish(session: Session, tool, *, decided_by=None, visibility="public"):
    """DRAFT → PUBLISHED through both governance gates."""
    from sutr.registry import lifecycle

    for target in (
        lifecycle.API_UPLOADED,
        lifecycle.TRANSLATING,
        lifecycle.IR_READY,
        lifecycle.METADATA_READY,
        lifecycle.GENERATING_MCP,
        lifecycle.VALIDATING,
        lifecycle.DEPLOYING,
        lifecycle.DEPLOYED,
    ):
        service.transition(session, tool, target)
    if visibility != tool.visibility:
        request = service.request_visibility(session, tool, visibility=visibility)
        service.decide(session, request, approve=True, decided_by_user_id=decided_by)
    review = service.transition(session, tool, lifecycle.UNDER_REVIEW)
    service.decide(session, review.change_request, approve=True, decided_by_user_id=decided_by)
    service.transition(session, tool, lifecycle.APPROVED)
    published = service.transition(session, tool, lifecycle.PUBLISHED)
    service.decide(session, published.change_request, approve=True, decided_by_user_id=decided_by)
    session.commit()
    session.refresh(tool)
    return tool


def register(session: Session, org_id, **overrides):
    body = {
        "tool_key": "refunds-api",
        "name": "Refunds API",
        "summary": "Issue and track refunds for card payments.",
        "description": "Refund issuance, status and reconciliation for card payments.",
        "category": "Finance",
        "tags": ["payments", "refunds"],
    }
    body.update(overrides)
    tool = service.register(session, org_id=org_id, **body)
    session.commit()
    session.refresh(tool)
    return tool


@pytest.fixture(name="provider_org")
def provider_org_fixture(session: Session, test_user: User):
    """A second organization that publishes tools into the marketplace."""
    org = Org(name="Acme Payments", slug="acme-payments", owner_user_id=test_user.id)
    session.add(org)
    session.flush()
    session.add(OrgMembership(user_id=test_user.id, org_id=org.id, role="owner"))
    session.commit()
    session.refresh(org)
    return org


@pytest.fixture(name="published_tools")
def published_tools_fixture(session: Session, provider_org: Org, test_user: User):
    """Three published, public tools from another tenant, plus one archived."""
    refunds = register(session, provider_org.id)
    invoices = register(
        session,
        provider_org.id,
        tool_key="invoices-api",
        name="Invoices API",
        summary="Create and send invoices.",
        description="Invoice creation, delivery and payment tracking.",
        tags=["billing", "invoices"],
    )
    shipping = register(
        session,
        provider_org.id,
        tool_key="shipping-api",
        name="Shipping API",
        summary="Book and track shipments.",
        description="Carrier booking, labels and tracking for parcels.",
        category="Logistics",
        tags=["logistics"],
        regions=["us-east-1"],
        compliance=["SOC2"],
    )
    for tool in (refunds, invoices, shipping):
        publish(session, tool, decided_by=test_user.id)
    drain()
    return {"refunds": refunds, "invoices": invoices, "shipping": shipping}


class StubEmbeddings(embeddings.EmbeddingProvider):
    """A deterministic stand-in, defined in the test suite where it belongs.

    It lives here rather than in the platform for the reason ADR-033 gives: a
    fake embedding provider that ships is a semantic search that is not one.
    """

    id = "stub"
    model = "stub-8"
    dimensions = 8

    def __init__(self, vectors: dict[str, list[float]] | None = None, fail: bool = False):
        self.vectors = vectors or {}
        self.fail = fail
        self.calls = 0

    def available(self):
        return True, None

    async def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("the embedding service is down")
        produced = []
        for text in texts:
            lowered = text.lower()
            # Eight crude topic dimensions. Enough to be a real similarity
            # signal, simple enough that a test can predict it.
            produced.append(
                [
                    float(lowered.count("refund")),
                    float(lowered.count("money") + lowered.count("payment")),
                    float(lowered.count("invoice")),
                    float(lowered.count("bill")),
                    float(lowered.count("ship")),
                    float(lowered.count("parcel") + lowered.count("carrier")),
                    float(lowered.count("card")),
                    1.0,
                ]
            )
        return embeddings.EmbeddingResult(vectors=produced, model=self.model, dimensions=8)

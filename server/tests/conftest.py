import hashlib
import secrets
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from sutr.db import get_session
from sutr.dependencies import (
    AgentAuth,
    get_agent_auth,
    get_current_org,
    get_current_user,
    get_impersonator,
)
from sutr.main import app
from sutr.models.access_pass import AccessPass  # noqa: F401
from sutr.models.access_rule import AccessRule  # noqa: F401
from sutr.models.agent_identity import AgentIdentity  # noqa: F401
from sutr.models.api_key import ApiKey  # noqa: F401
from sutr.models.api_source import ApiSource  # noqa: F401
from sutr.models.business_rule import BusinessRule  # noqa: F401
from sutr.models.chunk_embedding import ChunkEmbedding  # noqa: F401
from sutr.models.consumed_event import ConsumedEvent  # noqa: F401
from sutr.models.custom_api_integration import CustomApiIntegration  # noqa: F401
from sutr.models.deployment_revision import DeploymentRevision  # noqa: F401
from sutr.models.doc_workflow import DocWorkflow, GlossaryTerm  # noqa: F401
from sutr.models.document import Document  # noqa: F401
from sutr.models.document_chunk import DocumentChunk  # noqa: F401
from sutr.models.document_job import DocumentJob  # noqa: F401
from sutr.models.drift_report import DriftReport  # noqa: F401
from sutr.models.google_login_state import GoogleLoginState  # noqa: F401
from sutr.models.governance_exception import GovernanceException  # noqa: F401
from sutr.models.governance_policy import (  # noqa: F401
    GovernancePolicy,
    GovernancePolicyVersion,
)
from sutr.models.governance_review import GovernanceReview  # noqa: F401
from sutr.models.governance_run import ComplianceRun, RiskAssessment  # noqa: F401
from sutr.models.idempotency_key import IdempotencyKey  # noqa: F401
from sutr.models.integration import InstalledIntegration  # noqa: F401
from sutr.models.integration_credential import IntegrationCredential  # noqa: F401
from sutr.models.invoice import Invoice, InvoiceLine  # noqa: F401
from sutr.models.knowledge_graph import KnowledgeEdge, KnowledgeNode  # noqa: F401
from sutr.models.leader_lease import LeaderLease  # noqa: F401
from sutr.models.ledger_entry import LedgerEntry  # noqa: F401
from sutr.models.log import LogEntry  # noqa: F401
from sutr.models.marketplace_listing import MarketplaceListing  # noqa: F401
from sutr.models.marketplace_review import MarketplaceReview  # noqa: F401
from sutr.models.oauth import OAuthState  # noqa: F401
from sutr.models.oauth_client import OAuthClient  # noqa: F401
from sutr.models.oauth_connect_state import OAuthConnectState  # noqa: F401
from sutr.models.oauth_revoked_token import OAuthRevokedToken  # noqa: F401
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.outbox_event import OutboxEvent  # noqa: F401
from sutr.models.pricing_plan import PricingPlan  # noqa: F401
from sutr.models.provider_connection import ProviderConnection  # noqa: F401
from sutr.models.provider_profile import ProviderProfile  # noqa: F401
from sutr.models.quota import Quota  # noqa: F401
from sutr.models.registry_change_request import RegistryChangeRequest  # noqa: F401
from sutr.models.registry_pricing import RegistryPricing  # noqa: F401
from sutr.models.registry_tool import RegistryTool  # noqa: F401
from sutr.models.registry_version import RegistryVersion  # noqa: F401
from sutr.models.runtime_artifact import RuntimeArtifact  # noqa: F401
from sutr.models.secret import Secret  # noqa: F401
from sutr.models.settlement import PaymentAttempt, Settlement  # noqa: F401
from sutr.models.subscription import Subscription  # noqa: F401
from sutr.models.tool_approval_request import ToolApprovalRequest  # noqa: F401
from sutr.models.tool_cache import ToolCache  # noqa: F401
from sutr.models.tool_execution import ToolExecutionSetting  # noqa: F401
from sutr.models.tool_subscription import ToolSubscription  # noqa: F401
from sutr.models.user import User


@pytest.fixture(autouse=True)
def stub_token_validation(monkeypatch):
    async def _validate_token(url: str, token: str) -> None:
        return None

    monkeypatch.setattr("sutr.api.installed.validate_token", _validate_token)


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    from sutr.rate_limit import reset_all_rate_limiters

    reset_all_rate_limiters()
    yield
    reset_all_rate_limiters()


_ENGINE_CONSUMER_MODULES = (
    "sutr.db",
    "sutr.api.tool_approvals",
    "sutr.api_client",
    "sutr.integrations.registry",
    "sutr.mcp.asgi",
    "sutr.mcp.client",
    "sutr.mcp.management_tools",
    "sutr.mcp.oauth",
    "sutr.mcp.oauth_provider",
    "sutr.mcp.refresh",
    "sutr.mcp.server",
    "sutr.mcp.stdio",
    "sutr.services.tool_pipeline",
)


@pytest.fixture(name="session")
def session_fixture(monkeypatch):
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    for module in _ENGINE_CONSUMER_MODULES:
        monkeypatch.setattr(f"{module}.engine", test_engine, raising=False)
    with Session(test_engine) as session:
        yield session


@pytest.fixture(name="test_user")
def test_user_fixture(session):
    user = User(
        id=uuid.uuid4(),
        email="test@example.com",
        hashed_password="hashed",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture(name="test_org")
def test_org_fixture(session, test_user):
    org = Org(id=uuid.uuid4(), name="Test Org")
    session.add(org)
    session.commit()
    session.refresh(org)
    membership = OrgMembership(user_id=test_user.id, org_id=org.id, role="owner")
    session.add(membership)
    session.commit()
    return org


@pytest.fixture(name="client")
async def client_fixture(session, test_user, test_org):
    def override_session():
        yield session

    def override_user():
        return test_user

    def override_org():
        return test_org

    def override_agent_auth():
        return AgentAuth(org=test_org, user=test_user, api_key=None)

    def override_impersonator():
        return None

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_current_org] = override_org
    app.dependency_overrides[get_agent_auth] = override_agent_auth
    app.dependency_overrides[get_impersonator] = override_impersonator
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(name="unauthenticated_client")
async def unauthenticated_client_fixture(session):
    """A client with no credentials at all.

    The MCP transports authenticate from the raw ASGI scope rather than through
    FastAPI's dependency system, so their 401 path cannot be exercised with the
    `client` fixture's dependency overrides.
    """

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(name="api_key_record")
def api_key_record_fixture(session, test_user, test_org):
    """Creates a real ApiKey row. Returns (api_key_row, plain_key)."""
    plain_key = "ap_" + secrets.token_urlsafe(24)
    key_hash = hashlib.sha256(plain_key.encode()).hexdigest()
    api_key = ApiKey(
        org_id=test_org.id,
        created_by_user_id=test_user.id,
        name="test-key",
        key_prefix=plain_key[:12],
        key_hash=key_hash,
    )
    session.add(api_key)
    session.commit()
    session.refresh(api_key)
    return api_key, plain_key


@pytest.fixture(name="agent_key_client")
async def agent_key_client_fixture(session, test_user, test_org, api_key_record):
    """AsyncClient that authenticates via X-API-Key header (real dep, no override)."""
    api_key, plain_key = api_key_record

    def override_session():
        yield session

    def override_user():
        return test_user

    def override_org():
        return test_org

    def override_impersonator():
        return None

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_current_org] = override_org
    app.dependency_overrides[get_impersonator] = override_impersonator
    # get_agent_auth is NOT overridden — uses real implementation
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": plain_key},
    ) as c:
        yield c
    app.dependency_overrides.clear()


def served_paths(app) -> set[str]:
    """Every path the app serves, walking included routers.

    FastAPI stopped flattening included routers into `app.routes`, so a plain
    `{route.path for route in app.routes}` silently became a set of mount
    points — and every test that asked "is this a real route?" started
    answering "no" for all of them. Walking is version-independent and says
    what it means.
    """
    found: set[str] = set()

    def walk(routes) -> None:
        for route in routes:
            path = getattr(route, "path", None) or getattr(route, "path_format", None)
            if path:
                found.add(path)
            nested = getattr(route, "original_router", None) or getattr(route, "app", None)
            for attribute in ("routes",):
                child = getattr(nested, attribute, None)
                if child:
                    walk(child)

    walk(app.routes)
    return found

"""The Kubernetes provider, against a stub API server.

Every request is checked for path, method and body, because that is the only
thing that can be checked without a cluster. This provider is **NOT TESTED**
against a live API server, and these tests are not a substitute for that — what
they pin is that the objects Sutr sends carry the isolation the LLD requires
(§4.3.8) and that a template change cannot quietly drop one.
"""

import io
import json
import uuid
import zipfile

import httpx
import pytest

from sutr.config import settings
from sutr.deploy import kubernetes_provider as k8s
from sutr.deploy.base import DeploySpec, ProviderError, ProviderTarget

ORG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEPLOYMENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _package(files=None) -> bytes:
    files = files or {"server.py": "print('hi')\n", "tools.json": "{}"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class Recorder:
    """A stub API server that records what it was asked to do."""

    def __init__(self, responses=None):
        self.calls: list[httpx.Request] = []
        self.responses = responses or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = (request.method, request.url.path)
        if key in self.responses:
            status, payload = self.responses[key]
            return httpx.Response(status, json=payload)
        return httpx.Response(201, json={})

    def bodies(self, method: str, path_fragment: str) -> list[dict]:
        return [
            json.loads(call.content)
            for call in self.calls
            if call.method == method and path_fragment in call.url.path and call.content
        ]

    def paths(self, method: str | None = None) -> list[str]:
        return [call.url.path for call in self.calls if method is None or call.method == method]


@pytest.fixture(name="cluster")
def cluster_fixture(monkeypatch):
    monkeypatch.setattr(settings, "kubernetes_enabled", True)
    monkeypatch.setattr(settings, "kubernetes_api_server", "https://cluster.example:6443")
    monkeypatch.setattr(settings, "kubernetes_token", "a-token")
    monkeypatch.setattr(settings, "kubernetes_ca_cert_path", "")
    monkeypatch.setattr(settings, "kubernetes_runtime_image", "registry.example/mcp-base:1")
    monkeypatch.setattr(settings, "kubernetes_namespace_prefix", "sutr")

    recorder = Recorder()

    def make_client(cluster):
        return httpx.AsyncClient(
            base_url=cluster.base_url, transport=httpx.MockTransport(recorder.handler)
        )

    monkeypatch.setattr(k8s, "make_client", make_client)
    return recorder


def _spec(**overrides) -> DeploySpec:
    kwargs = {
        "deployment_id": DEPLOYMENT_ID,
        "org_id": ORG_ID,
        "name": "Petstore",
        "slug": "petstore",
        "package_zip": _package(),
        "env": {"PETSTORE_API_TOKEN": "sk-secret"},
        "revision": 1,
    }
    kwargs.update(overrides)
    return DeploySpec(**kwargs)


# ── Isolation (LLD §4.3.8) ───────────────────────────────────────────────────


async def test_a_deploy_creates_the_namespace_with_pod_security_standards(cluster):
    await k8s.KubernetesProvider().deploy(_spec())
    body = cluster.bodies("POST", "/api/v1/namespaces")[0]
    labels = body["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    assert labels["pod-security.kubernetes.io/warn"] == "restricted"
    assert body["metadata"]["name"] == k8s.namespace_for(ORG_ID)


async def test_two_tenants_get_two_namespaces(cluster):
    provider = k8s.KubernetesProvider()
    first = await provider.deploy(_spec())
    second = await provider.deploy(_spec(org_id=uuid.uuid4(), deployment_id=uuid.uuid4()))
    assert first["namespace"] != second["namespace"]


async def test_a_deploy_creates_a_resource_quota(cluster):
    await k8s.KubernetesProvider().deploy(_spec())
    quota = cluster.bodies("POST", "/resourcequotas")[0]
    assert quota["spec"]["hard"]["limits.memory"] == "4Gi"
    assert quota["spec"]["hard"]["pods"] == "20"


async def test_a_deploy_creates_default_deny_and_narrow_allowances(cluster):
    await k8s.KubernetesProvider().deploy(_spec())
    policies = {p["metadata"]["name"]: p for p in cluster.bodies("POST", "/networkpolicies")}

    deny = policies["sutr-default-deny"]
    assert deny["spec"]["podSelector"] == {}
    assert set(deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}

    runtime = policies["sutr-runtime"]
    egress = runtime["spec"]["egress"]
    # DNS, then the public internet with every private range excluded — which
    # is what stops a runtime reaching other workloads in the cluster.
    assert {"protocol": "UDP", "port": 53} in egress[0]["ports"]
    block = egress[1]["to"][0]["ipBlock"]
    assert block["cidr"] == "0.0.0.0/0"
    assert "10.0.0.0/8" in block["except"]
    assert "169.254.0.0/16" in block["except"]
    # Ingress only from a namespace the operator has labelled.
    assert runtime["spec"]["ingress"][0]["from"] == [
        {"namespaceSelector": {"matchLabels": {"sutr.io/ingress": "allowed"}}}
    ]


async def test_the_service_account_mounts_no_api_token(cluster):
    await k8s.KubernetesProvider().deploy(_spec())
    account = cluster.bodies("POST", "/serviceaccounts")[0]
    assert account["automountServiceAccountToken"] is False


async def test_the_pod_runs_non_root_read_only_with_no_capabilities(cluster):
    await k8s.KubernetesProvider().deploy(_spec())
    deployment = cluster.bodies("POST", "/deployments")[0]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["automountServiceAccountToken"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert container["resources"]["limits"]["memory"] == "512Mi"


# ── The package and the credential ───────────────────────────────────────────


async def test_the_credential_goes_into_a_secret_not_the_pod_spec(cluster):
    await k8s.KubernetesProvider().deploy(_spec())
    secret = cluster.bodies("POST", "/secrets")[0]
    import base64

    assert base64.b64decode(secret["data"]["PETSTORE_API_TOKEN"]).decode() == "sk-secret"
    deployment = json.dumps(cluster.bodies("POST", "/deployments")[0])
    assert "sk-secret" not in deployment
    assert "secretRef" in deployment


async def test_a_deployment_without_a_credential_creates_no_secret(cluster):
    await k8s.KubernetesProvider().deploy(_spec(env={}))
    assert cluster.bodies("POST", "/secrets") == []
    deployment = cluster.bodies("POST", "/deployments")[0]
    assert "envFrom" not in deployment["spec"]["template"]["spec"]["containers"][0]


async def test_the_package_is_delivered_as_an_immutable_per_revision_configmap(cluster):
    await k8s.KubernetesProvider().deploy(_spec(revision=3))
    configmap = cluster.bodies("POST", "/configmaps")[0]
    assert configmap["immutable"] is True
    assert configmap["metadata"]["name"].endswith("-r3")
    assert configmap["data"]["server.py"] == "print('hi')\n"


async def test_a_package_too_large_for_a_configmap_is_refused_with_the_reason(cluster):
    big = _package({"blob.txt": "x" * (k8s.CONFIGMAP_LIMIT_BYTES + 1)})
    with pytest.raises(ProviderError) as excinfo:
        await k8s.KubernetesProvider().deploy(_spec(package_zip=big))
    assert "ConfigMap limit" in str(excinfo.value)


async def test_the_returned_url_says_it_is_in_cluster_only(cluster):
    state = await k8s.KubernetesProvider().deploy(_spec())
    assert state["url"].endswith("/mcp")
    assert ".svc.cluster.local" in state["url"]
    assert "No Ingress is created" in state["reachability"]


# ── Lifecycle ────────────────────────────────────────────────────────────────


async def test_status_reports_ready_replicas(cluster):
    state = await k8s.KubernetesProvider().deploy(_spec())
    cluster.responses[
        ("GET", f"/apis/apps/v1/namespaces/{state['namespace']}/deployments/{state['name']}")
    ] = (200, {"spec": {"replicas": 1}, "status": {"readyReplicas": 1}})
    result = await k8s.KubernetesProvider().status(state, ProviderTarget())
    assert result.state == "running"
    assert result.detail == "1/1 ready"


async def test_status_reports_a_scaled_down_deployment_as_stopped(cluster):
    state = await k8s.KubernetesProvider().deploy(_spec())
    cluster.responses[
        ("GET", f"/apis/apps/v1/namespaces/{state['namespace']}/deployments/{state['name']}")
    ] = (200, {"spec": {"replicas": 0}, "status": {}})
    result = await k8s.KubernetesProvider().status(state, ProviderTarget())
    assert result.state == "stopped"


async def test_status_surfaces_the_reason_a_replica_is_not_ready(cluster):
    state = await k8s.KubernetesProvider().deploy(_spec())
    cluster.responses[
        ("GET", f"/apis/apps/v1/namespaces/{state['namespace']}/deployments/{state['name']}")
    ] = (
        200,
        {
            "spec": {"replicas": 1},
            "status": {
                "conditions": [
                    {"status": "False", "message": "ImagePullBackOff on registry.example"}
                ]
            },
        },
    )
    result = await k8s.KubernetesProvider().status(state, ProviderTarget())
    assert result.state == "error"
    assert "ImagePullBackOff" in result.detail


async def test_a_deleted_deployment_reads_as_not_found(cluster):
    state = await k8s.KubernetesProvider().deploy(_spec())
    cluster.responses[
        ("GET", f"/apis/apps/v1/namespaces/{state['namespace']}/deployments/{state['name']}")
    ] = (404, {"message": "not found"})
    result = await k8s.KubernetesProvider().status(state, ProviderTarget())
    assert result.state == "not_found"


async def test_stop_and_start_scale_the_deployment(cluster):
    provider = k8s.KubernetesProvider()
    state = await provider.deploy(_spec())
    await provider.stop(state, ProviderTarget())
    await provider.start(state, ProviderTarget())
    scales = cluster.bodies("PATCH", "/scale")
    assert [body["spec"]["replicas"] for body in scales] == [0, 1]
    patch = next(call for call in cluster.calls if call.method == "PATCH")
    # application/json is refused by the API server with 415.
    assert patch.headers["content-type"] == k8s.MERGE_PATCH


async def test_remove_deletes_the_workload_but_keeps_the_shared_namespace(cluster):
    provider = k8s.KubernetesProvider()
    state = await provider.deploy(_spec())
    await provider.remove(state, ProviderTarget())
    deleted = cluster.paths("DELETE")
    assert any("/deployments/" in path for path in deleted)
    assert any("/services/" in path for path in deleted)
    assert any("/secrets/" in path for path in deleted)
    assert any(path.endswith("/configmaps") for path in deleted)
    assert any("/serviceaccounts/" in path for path in deleted)
    # The namespace belongs to the tenant, not to one deployment.
    assert not any(path == f"/api/v1/namespaces/{state['namespace']}" for path in deleted)


async def test_configmaps_are_deleted_by_the_recorded_selector(cluster):
    provider = k8s.KubernetesProvider()
    state = await provider.deploy(_spec())
    await provider.remove(state, ProviderTarget())
    call = next(
        call
        for call in cluster.calls
        if call.method == "DELETE" and call.url.path.endswith("/configmaps")
    )
    assert call.url.params["labelSelector"] == f"sutr.io/deployment={state['selector']}"


async def test_logs_are_read_from_the_deployments_pods(cluster):
    provider = k8s.KubernetesProvider()
    state = await provider.deploy(_spec())
    namespace = state["namespace"]
    cluster.responses[("GET", f"/api/v1/namespaces/{namespace}/pods")] = (
        200,
        {"items": [{"metadata": {"name": "mcp-abc-1"}}]},
    )
    cluster.responses[("GET", f"/api/v1/namespaces/{namespace}/pods/mcp-abc-1/log")] = (
        200,
        {},
    )
    output = await provider.logs(state, ProviderTarget(), tail=50)
    assert "mcp-abc-1" in output


async def test_logs_say_so_when_no_pod_is_running(cluster):
    provider = k8s.KubernetesProvider()
    state = await provider.deploy(_spec())
    cluster.responses[("GET", f"/api/v1/namespaces/{state['namespace']}/pods")] = (
        200,
        {"items": []},
    )
    assert "no logs" in await provider.logs(state, ProviderTarget())


# ── Metrics ──────────────────────────────────────────────────────────────────


async def test_metrics_report_usage_when_metrics_server_is_installed(cluster):
    provider = k8s.KubernetesProvider()
    state = await provider.deploy(_spec())
    namespace = state["namespace"]
    cluster.responses[
        ("GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/{state['name']}")
    ] = (200, {"spec": {"replicas": 1}, "status": {"readyReplicas": 1}})
    cluster.responses[("GET", f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods")] = (
        200,
        {"items": [{"containers": [{"usage": {"cpu": "250m", "memory": "128Mi"}}]}]},
    )
    metrics = await provider.metrics(state, ProviderTarget())
    assert metrics.cpu_percent == 25.0
    assert metrics.memory_bytes == 128 * 1024 * 1024
    assert metrics.replicas == 1


async def test_a_cluster_without_metrics_server_says_so_rather_than_reporting_zero(cluster):
    provider = k8s.KubernetesProvider()
    state = await provider.deploy(_spec())
    namespace = state["namespace"]
    cluster.responses[
        ("GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/{state['name']}")
    ] = (200, {"spec": {"replicas": 1}, "status": {"readyReplicas": 1}})
    cluster.responses[("GET", f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods")] = (
        404,
        {"message": "the server could not find the requested resource"},
    )
    metrics = await provider.metrics(state, ProviderTarget())
    assert metrics.cpu_percent is None
    assert "metrics-server" in metrics.unavailable_reason
    assert metrics.replicas == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [("250m", 0.25), ("1", 1.0), ("500000u", 0.5), ("1500n", 1.5e-6), ("", None), ("x", None)],
)
def test_cpu_quantities_are_parsed(value, expected):
    assert k8s.parse_cpu(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("128Mi", 134217728), ("1Gi", 1073741824), ("500K", 500000), ("1024", 1024), ("", None)],
)
def test_memory_quantities_are_parsed(value, expected):
    assert k8s.parse_memory(value) == expected


# ── Availability ─────────────────────────────────────────────────────────────


async def test_the_provider_is_unavailable_without_a_base_image(cluster, monkeypatch):
    monkeypatch.setattr(settings, "kubernetes_runtime_image", "")
    ok, reason = await k8s.KubernetesProvider().available()
    assert ok is False
    assert "NOT_CONFIGURED" in reason


async def test_the_provider_is_unavailable_without_cluster_credentials(monkeypatch):
    monkeypatch.setattr(settings, "kubernetes_enabled", True)
    monkeypatch.setattr(settings, "kubernetes_api_server", "")
    monkeypatch.setattr(settings, "kubernetes_token", "")
    monkeypatch.setattr(k8s, "IN_CLUSTER_TOKEN", k8s.Path("/nonexistent/token"))
    ok, reason = await k8s.KubernetesProvider().available()
    assert ok is False
    assert "NOT_CONFIGURED" in reason


async def test_the_provider_is_available_when_the_api_server_answers(cluster):
    cluster.responses[("GET", "/version")] = (200, {"gitVersion": "v1.30.0"})
    ok, reason = await k8s.KubernetesProvider().available()
    assert ok is True
    assert reason is None


async def test_an_unreachable_api_server_is_reported_not_swallowed(cluster, monkeypatch):
    def failing(clusterconf):
        def handler(request):
            raise httpx.ConnectError("no route to host")

        return httpx.AsyncClient(
            base_url=clusterconf.base_url, transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(k8s, "make_client", failing)
    ok, reason = await k8s.KubernetesProvider().available()
    assert ok is False
    assert "Could not reach the Kubernetes API server" in reason


async def test_a_forbidden_response_explains_the_service_account_permission(cluster):
    cluster.responses[("GET", "/version")] = (403, {"message": "forbidden"})
    ok, reason = await k8s.KubernetesProvider().available()
    assert ok is False
    assert "may lack permission" in reason


def test_the_provider_is_registered_and_disabled_by_default(monkeypatch):
    from sutr.deploy.registry import list_providers, provider_enabled

    monkeypatch.setattr(settings, "kubernetes_enabled", False)
    entry = next(p for p in list_providers() if p["id"] == "kubernetes")
    assert entry["enabled"] is False
    assert "KUBERNETES_ENABLED=false" in provider_enabled("kubernetes")[1]


async def test_an_object_that_already_exists_is_patched_rather_than_recreated(cluster):
    """An update is a new revision of the same objects, never a delete-recreate."""
    namespace = k8s.namespace_for(ORG_ID)
    name = k8s.workload_name(DEPLOYMENT_ID)
    cluster.responses[("POST", f"/apis/apps/v1/namespaces/{namespace}/deployments")] = (
        409,
        {"message": "already exists"},
    )
    await k8s.KubernetesProvider().deploy(_spec(revision=2))
    patched = [
        call
        for call in cluster.calls
        if call.method == "PATCH" and call.url.path.endswith(f"/deployments/{name}")
    ]
    assert len(patched) == 1
    body = json.loads(patched[0].content)
    assert body["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"].endswith("-r2")

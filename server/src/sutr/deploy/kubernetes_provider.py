"""Kubernetes deployment provider, with the LLD's runtime isolation built in.

LLD §4.3.8 is specific about what a runtime must be wrapped in: *"Dedicated
namespace per provider, NetworkPolicy, ServiceAccount, resource quotas, Pod
Security Standards. A runtime can never read another provider's secrets, talk
to unrelated runtimes, or bypass the Gateway."* Those are not optional extras
here — creating a Deployment without them would be a provider that satisfies
the deployment requirement and violates the isolation one, which is the more
important of the two.

So every deploy ensures, in order:

    Namespace          one per tenant, labelled for Pod Security Standards
                       at `restricted`
    ResourceQuota      a ceiling on what one tenant's runtimes can consume
    NetworkPolicy      default-deny, then two narrow allowances: DNS, and
                       egress to public addresses only — which is what stops a
                       runtime reaching other workloads in the cluster
    ServiceAccount     per deployment, with no API token mounted
    Secret             the credential, mounted as env, never in the pod spec
    ConfigMap          the generated package, one immutable object per revision
    Deployment         non-root, read-only root filesystem, all capabilities
                       dropped, seccomp RuntimeDefault
    Service            ClusterIP

**How the code gets into the pod.** There is no image build step here: this
provider has no builder and no registry to push to, and inventing one would be
inventing infrastructure. The generated package is delivered as a ConfigMap
mounted at /app over a base image the operator names
(`KUBERNETES_RUNTIME_IMAGE`), which must have the package's dependencies
installed. That is a real, documented Kubernetes mechanism with a real limit —
a ConfigMap caps at about 1 MiB — and the limit is checked rather than
discovered at apply time.

**No Ingress is created.** The returned URL is the in-cluster Service DNS name.
An Ingress needs an ingress class, a hostname and a TLS story that belong to
the cluster, and a guessed one would hand back a URL that does not resolve.

**NOT TESTED against a live cluster.** Every request in this module is built
against the documented Kubernetes API (core/v1, apps/v1, networking.k8s.io/v1,
metrics.k8s.io/v1beta1) and is exercised in tests against a stub transport that
asserts the exact paths, methods and bodies. No part of it has been run against
a real API server, and it is labelled NOT TESTED in the traceability matrix
until it has been.
"""

import io
import uuid
import zipfile
from pathlib import Path
from typing import Any

import httpx

from sutr.config import settings
from sutr.deploy.base import (
    DeploymentProvider,
    DeploySpec,
    ProviderError,
    ProviderMetrics,
    ProviderStatus,
    ProviderTarget,
)

# The in-cluster service account, mounted into every pod by the kubelet.
SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
IN_CLUSTER_TOKEN = SERVICE_ACCOUNT_DIR / "token"
IN_CLUSTER_CA = SERVICE_ACCOUNT_DIR / "ca.crt"

# ConfigMap objects are limited to roughly 1 MiB by etcd's value size limit.
# Checked with headroom for the rest of the object rather than at the edge.
CONFIGMAP_LIMIT_BYTES = 900_000

CONTAINER_PORT = 8000
SERVICE_PORT = 80

NOT_CONFIGURED = (
    "NOT_CONFIGURED: the Kubernetes provider needs KUBERNETES_ENABLED=true, an API server "
    "(in-cluster credentials or KUBERNETES_API_SERVER + KUBERNETES_TOKEN) and a base runtime "
    "image (KUBERNETES_RUNTIME_IMAGE) that has the generated package's dependencies installed."
)

# Private ranges a runtime has no business reaching: the cluster's own pods and
# services, the node network, and the cloud metadata endpoint. Everything else
# is allowed, because calling the provider's public API is the whole job.
_PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")

# A namespace may reach a runtime only if it carries this label. The gateway's
# namespace must be labelled by the operator; without it nothing outside the
# tenant namespace can reach the runtime, which is the intended default.
INGRESS_LABEL = {"sutr.io/ingress": "allowed"}


class _Cluster:
    """Where the API server is and how to authenticate to it."""

    def __init__(self, base_url: str, token: str, verify: bool | str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.verify = verify


def cluster_config() -> _Cluster | None:
    """Resolve cluster credentials: explicit settings first, then in-cluster."""
    if settings.kubernetes_api_server and settings.kubernetes_token:
        verify: bool | str = settings.kubernetes_verify_tls
        if settings.kubernetes_ca_cert_path:
            verify = settings.kubernetes_ca_cert_path
        return _Cluster(settings.kubernetes_api_server, settings.kubernetes_token, verify)
    try:
        if IN_CLUSTER_TOKEN.is_file():
            token = IN_CLUSTER_TOKEN.read_text().strip()
            verify = str(IN_CLUSTER_CA) if IN_CLUSTER_CA.is_file() else True
            return _Cluster("https://kubernetes.default.svc", token, verify)
    except OSError:
        return None
    return None


def make_client(cluster: _Cluster) -> httpx.AsyncClient:
    """The HTTP client for one call.

    A module-level function rather than an inline constructor so tests can
    replace it with one carrying a stub transport, and so every request in this
    module goes through the same timeout and TLS settings.
    """
    return httpx.AsyncClient(
        base_url=cluster.base_url,
        headers={"Authorization": f"Bearer {cluster.token}", "Accept": "application/json"},
        verify=cluster.verify,
        timeout=settings.kubernetes_request_timeout_seconds,
        follow_redirects=False,
    )


def _message(payload: Any, status: int) -> str:
    """The human part of a Kubernetes Status object."""
    if isinstance(payload, dict):
        for key in ("message", "reason"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return f"HTTP {status}"


# A PATCH body must declare which patch semantics it uses; the API server
# rejects application/json with 415. Merge patch is the right one here: these
# bodies are the desired shape of the fields Sutr owns, not a list of
# operations.
MERGE_PATCH = "application/merge-patch+json"


async def request(
    method: str,
    path: str,
    *,
    body: Any = None,
    params: dict[str, Any] | None = None,
    tolerate: tuple[int, ...] = (),
    raw: bool = False,
) -> Any:
    """One call to the API server, with Kubernetes' error shape unwrapped."""
    cluster = cluster_config()
    if cluster is None:
        raise ProviderError(NOT_CONFIGURED)
    headers = {"Content-Type": MERGE_PATCH} if method.upper() == "PATCH" else None
    try:
        async with make_client(cluster) as client:
            response = await client.request(method, path, json=body, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderError(f"Could not reach the Kubernetes API server: {exc}")

    if response.status_code in tolerate:
        return None
    if response.status_code in (401, 403):
        raise ProviderError(
            f"The Kubernetes API server refused the request ({response.status_code}). The "
            "service account may lack permission for this operation. Details: "
            f"{_message(_json(response), response.status_code)}"
        )
    if not response.is_success:
        raise ProviderError(
            f"The Kubernetes API server returned {response.status_code}: "
            f"{_message(_json(response), response.status_code)}"
        )
    if raw:
        return response.text
    return _json(response) if response.content else {}


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"message": response.text[:500]}


# ── Names ────────────────────────────────────────────────────────────────────
#
# Every name is derived from an id, never from a user-supplied string: a
# Kubernetes object name has a syntax, and a deployment called "My API!" must
# not be able to produce an invalid or colliding one.


def namespace_for(org_id: uuid.UUID) -> str:
    return f"{settings.kubernetes_namespace_prefix}-{str(org_id).replace('-', '')[:16]}"


def workload_name(deployment_id: uuid.UUID) -> str:
    return f"mcp-{str(deployment_id).replace('-', '')[:16]}"


def configmap_name(deployment_id: uuid.UUID, revision: int) -> str:
    return f"{workload_name(deployment_id)}-r{revision}"


def _labels(org_id: str, deployment_id: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "sutr-mcp-runtime",
        "app.kubernetes.io/managed-by": "sutr",
        "app.kubernetes.io/instance": workload_name(uuid.UUID(deployment_id)),
        "sutr.io/org": org_id.replace("-", "")[:32],
        "sutr.io/deployment": deployment_id.replace("-", "")[:32],
    }


def unpack(package_zip: bytes) -> dict[str, str]:
    """The generated package's files, for the ConfigMap."""
    files: dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(package_zip)) as archive:
            for name in sorted(archive.namelist()):
                files[name] = archive.read(name).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise ProviderError(f"The generated package could not be read: {exc}")
    return files


# ── Object bodies ────────────────────────────────────────────────────────────


def namespace_body(namespace: str, org_id: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "sutr",
                "sutr.io/org": org_id.replace("-", "")[:32],
                # Pod Security Standards, enforced rather than merely audited.
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
                "pod-security.kubernetes.io/audit": "restricted",
                "pod-security.kubernetes.io/warn": "restricted",
            },
        },
    }


def quota_body(namespace: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "sutr-runtime-quota", "namespace": namespace},
        "spec": {
            "hard": {
                "requests.cpu": "2",
                "requests.memory": "2Gi",
                "limits.cpu": "4",
                "limits.memory": "4Gi",
                "pods": "20",
                "count/deployments.apps": "20",
                "services": "20",
            }
        },
    }


def deny_all_policy_body(namespace: str) -> dict:
    """Default-deny, both directions. Everything else is an exception to this."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "sutr-default-deny", "namespace": namespace},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
    }


def runtime_policy_body(namespace: str) -> dict:
    """The two things a runtime legitimately needs: DNS, and the public internet.

    Egress to private ranges is excluded, which is what stops a compromised
    runtime from reaching another tenant's pods, the cluster's own services, or
    the cloud metadata endpoint. Ingress is allowed only from namespaces the
    operator has labelled — the gateway's — so a runtime cannot be called
    directly by another workload that happens to share the cluster.
    """
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "sutr-runtime", "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/managed-by": "sutr"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    "from": [{"namespaceSelector": {"matchLabels": INGRESS_LABEL}}],
                    "ports": [{"protocol": "TCP", "port": CONTAINER_PORT}],
                }
            ],
            "egress": [
                {
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ]
                },
                {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(_PRIVATE_RANGES)}}]},
            ],
        },
    }


def service_account_body(namespace: str, name: str, labels: dict[str, str]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        # A generated MCP server has no reason to talk to the Kubernetes API,
        # and a mounted token is the first thing a compromised process reaches
        # for.
        "automountServiceAccountToken": False,
    }


def secret_body(namespace: str, name: str, labels: dict[str, str], env: dict[str, str]) -> dict:
    import base64

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "type": "Opaque",
        "data": {
            key: base64.b64encode(value.encode("utf-8")).decode()
            for key, value in sorted(env.items())
        },
    }


def configmap_body(
    namespace: str, name: str, labels: dict[str, str], files: dict[str, str]
) -> dict:
    total = sum(len(key) + len(value.encode("utf-8")) for key, value in files.items())
    if total > CONFIGMAP_LIMIT_BYTES:
        raise ProviderError(
            f"The generated package is {total} bytes, over the {CONFIGMAP_LIMIT_BYTES}-byte "
            "ConfigMap limit. This provider delivers the package as a ConfigMap and cannot "
            "carry one this large; a provider that builds an image is needed for it."
        )
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        # One ConfigMap per revision, never edited: that is what makes a
        # rollback a re-reference of something retained rather than a rebuild.
        "immutable": True,
        "data": dict(sorted(files.items())),
    }


def deployment_body(
    *,
    namespace: str,
    name: str,
    labels: dict[str, str],
    image: str,
    configmap: str,
    service_account: str,
    secret_name: str | None,
    revision: int,
    replicas: int = 1,
) -> dict:
    container: dict[str, Any] = {
        "name": "mcp",
        "image": image,
        "command": [
            "python",
            "/app/server.py",
            "--transport",
            "http",
            "--port",
            str(CONTAINER_PORT),
        ],
        "ports": [{"containerPort": CONTAINER_PORT, "name": "http"}],
        "volumeMounts": [
            {"name": "package", "mountPath": "/app", "readOnly": True},
            # The root filesystem is read-only, so anything that must write
            # gets an explicit, empty, in-memory place to do it.
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "resources": {
            "requests": {"cpu": "50m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi"},
        },
        "readinessProbe": {
            "httpGet": {"path": "/health", "port": CONTAINER_PORT},
            "initialDelaySeconds": 3,
            "periodSeconds": 10,
        },
        "livenessProbe": {
            "httpGet": {"path": "/health", "port": CONTAINER_PORT},
            "initialDelaySeconds": 15,
            "periodSeconds": 30,
        },
    }
    if secret_name:
        container["envFrom"] = [{"secretRef": {"name": secret_name}}]

    pod_labels = {**labels, "sutr.io/revision": str(revision)}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"sutr.io/deployment": labels["sutr.io/deployment"]}},
            "template": {
                "metadata": {"labels": pod_labels},
                "spec": {
                    "serviceAccountName": service_account,
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [container],
                    "volumes": [
                        {"name": "package", "configMap": {"name": configmap}},
                        {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}},
                    ],
                },
            },
        },
    }


def service_body(namespace: str, name: str, labels: dict[str, str]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "type": "ClusterIP",
            "selector": {"sutr.io/deployment": labels["sutr.io/deployment"]},
            "ports": [{"name": "http", "port": SERVICE_PORT, "targetPort": CONTAINER_PORT}],
        },
    }


# ── Quantities ───────────────────────────────────────────────────────────────

_CPU_SUFFIXES = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
_MEMORY_SUFFIXES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def parse_cpu(value: str) -> float | None:
    """Kubernetes CPU quantity → cores."""
    text = (value or "").strip()
    if not text:
        return None
    for suffix, factor in _CPU_SUFFIXES.items():
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * factor
            except ValueError:
                return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_memory(value: str) -> int | None:
    """Kubernetes memory quantity → bytes."""
    text = (value or "").strip()
    if not text:
        return None
    for suffix, factor in _MEMORY_SUFFIXES.items():
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * factor)
            except ValueError:
                return None
    try:
        return int(float(text))
    except ValueError:
        return None


class KubernetesProvider(DeploymentProvider):
    id = "kubernetes"
    display_name = "Kubernetes"
    connection_provider = None
    config_fields = ()
    creates = (
        "A namespace per tenant with Pod Security Standards, a resource quota and default-deny "
        "network policies, then a Deployment, Service, ServiceAccount, Secret and per-revision "
        "ConfigMap for this server."
    )
    supports_update = True
    supports_metrics = True

    async def available(self, target: ProviderTarget | None = None) -> tuple[bool, str | None]:
        if not settings.kubernetes_enabled:
            return False, "The Kubernetes provider is disabled (KUBERNETES_ENABLED=false)."
        if cluster_config() is None:
            return False, NOT_CONFIGURED
        if not settings.kubernetes_runtime_image.strip():
            return False, NOT_CONFIGURED
        try:
            await request("GET", "/version")
        except ProviderError as exc:
            return False, str(exc)
        return True, None

    async def deploy(self, spec: DeploySpec) -> dict:
        # One namespace per tenant (LLD §4.3.8), so the org is the input. A
        # deployment created without one falls back to its own id, which still
        # isolates it — just more narrowly than intended.
        owner = spec.org_id or spec.deployment_id
        namespace = namespace_for(owner)
        name = workload_name(spec.deployment_id)
        labels = _labels(str(owner), str(spec.deployment_id))
        configmap = configmap_name(spec.deployment_id, spec.revision)
        secret_name = f"{name}-env" if spec.env else None

        await self._ensure_namespace(namespace, str(owner))
        await _apply(
            f"/api/v1/namespaces/{namespace}/serviceaccounts",
            f"/api/v1/namespaces/{namespace}/serviceaccounts/{name}",
            service_account_body(namespace, name, labels),
        )
        if secret_name:
            await _apply(
                f"/api/v1/namespaces/{namespace}/secrets",
                f"/api/v1/namespaces/{namespace}/secrets/{secret_name}",
                secret_body(namespace, secret_name, labels, spec.env),
            )
        # The per-revision ConfigMap is immutable, so it is created and never
        # replaced. A revision that already exists is the same bytes by
        # construction.
        await _create_if_absent(
            f"/api/v1/namespaces/{namespace}/configmaps",
            configmap_body(namespace, configmap, labels, unpack(spec.package_zip)),
        )
        await _apply(
            f"/apis/apps/v1/namespaces/{namespace}/deployments",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
            deployment_body(
                namespace=namespace,
                name=name,
                labels=labels,
                image=settings.kubernetes_runtime_image,
                configmap=configmap,
                service_account=name,
                secret_name=secret_name,
                revision=spec.revision,
            ),
        )
        await _apply(
            f"/api/v1/namespaces/{namespace}/services",
            f"/api/v1/namespaces/{namespace}/services/{name}",
            service_body(namespace, name, labels),
            # A Service holds an allocated clusterIP; replacing it wholesale
            # would try to reassign one. Only the spec Sutr owns is patched.
            patch=True,
        )

        url = f"http://{name}.{namespace}.svc.cluster.local:{SERVICE_PORT}"
        return {
            "namespace": namespace,
            "name": name,
            "configmap": configmap,
            "secret": secret_name,
            "selector": labels["sutr.io/deployment"],
            "revision": spec.revision,
            "url": f"{url}/mcp",
            "health_url": f"{url}/health",
            "metrics_url": f"{url}/metrics",
            # Said plainly on the record rather than in a docstring nobody
            # reading a deployment will open.
            "reachability": (
                "In-cluster only. No Ingress is created: the hostname, ingress class and TLS "
                "are the cluster's to decide."
            ),
        }

    async def _ensure_namespace(self, namespace: str, org_id: str) -> None:
        await _create_if_absent("/api/v1/namespaces", namespace_body(namespace, org_id))
        await _apply(
            f"/api/v1/namespaces/{namespace}/resourcequotas",
            f"/api/v1/namespaces/{namespace}/resourcequotas/sutr-runtime-quota",
            quota_body(namespace),
        )
        for policy in (deny_all_policy_body(namespace), runtime_policy_body(namespace)):
            await _apply(
                f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies",
                f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies/"
                f"{policy['metadata']['name']}",
                policy,
            )

    async def status(self, state: dict, target: ProviderTarget) -> ProviderStatus:
        namespace, name = _located(state)
        body = await request(
            "GET",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
            tolerate=(404,),
        )
        if body is None:
            return ProviderStatus(state="not_found")
        spec_replicas = (body.get("spec") or {}).get("replicas", 0)
        status = body.get("status") or {}
        ready = status.get("readyReplicas") or 0
        if spec_replicas == 0:
            return ProviderStatus(state="stopped", detail="Scaled to zero replicas.")
        if ready:
            return ProviderStatus(state="running", detail=f"{ready}/{spec_replicas} ready")
        return ProviderStatus(
            state="error",
            detail=_unready_reason(status) or "No replica has become ready yet.",
        )

    async def start(self, state: dict, target: ProviderTarget) -> None:
        await self._scale(state, 1)

    async def stop(self, state: dict, target: ProviderTarget) -> None:
        await self._scale(state, 0)

    async def _scale(self, state: dict, replicas: int) -> None:
        namespace, name = _located(state)
        await request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale",
            body={"spec": {"replicas": replicas}},
            tolerate=(404,),
        )

    async def remove(self, state: dict, target: ProviderTarget) -> None:
        """Delete everything this deployment created. Idempotent.

        The namespace is left alone: it belongs to the tenant, not to one
        deployment, and deleting it would take every other runtime in it with
        it.
        """
        namespace, name = _located(state)
        selector = _selector(state)
        await request(
            "DELETE",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
            tolerate=(404,),
        )
        await request("DELETE", f"/api/v1/namespaces/{namespace}/services/{name}", tolerate=(404,))
        if state.get("secret"):
            await request(
                "DELETE",
                f"/api/v1/namespaces/{namespace}/secrets/{state['secret']}",
                tolerate=(404,),
            )
        await request(
            "DELETE",
            f"/api/v1/namespaces/{namespace}/configmaps",
            params={"labelSelector": selector},
            tolerate=(404,),
        )
        await request(
            "DELETE",
            f"/api/v1/namespaces/{namespace}/serviceaccounts/{name}",
            tolerate=(404,),
        )

    async def logs(self, state: dict, target: ProviderTarget, tail: int = 100) -> str:
        namespace, _name = _located(state)
        pods = await request(
            "GET",
            f"/api/v1/namespaces/{namespace}/pods",
            params={"labelSelector": _selector(state)},
            tolerate=(404,),
        )
        items = (pods or {}).get("items") or []
        if not items:
            return "No pod is running for this deployment, so there are no logs to read."
        chunks = []
        for pod in items[:3]:
            pod_name = (pod.get("metadata") or {}).get("name", "")
            text = await request(
                "GET",
                f"/api/v1/namespaces/{namespace}/pods/{pod_name}/log",
                params={"tailLines": tail, "container": "mcp"},
                tolerate=(400, 404),
                raw=True,
            )
            chunks.append(f"── {pod_name} ──\n{text or '(no output)'}")
        return "\n\n".join(chunks)

    async def metrics(self, state: dict, target: ProviderTarget) -> ProviderMetrics:
        namespace, name = _located(state)
        deployment = await request(
            "GET",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
            tolerate=(404,),
        )
        if deployment is None:
            return ProviderMetrics(
                source=self.id, unavailable_reason="The deployment no longer exists."
            )
        status = deployment.get("status") or {}
        replicas = status.get("readyReplicas") or 0

        # metrics.k8s.io is served by metrics-server, which is an add-on. Its
        # absence is reported as such rather than as zero usage.
        usage = await request(
            "GET",
            f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods",
            params={"labelSelector": _selector(state)},
            tolerate=(404, 503),
        )
        if usage is None:
            return ProviderMetrics(
                source=self.id,
                replicas=replicas,
                healthy=bool(replicas),
                unavailable_reason=(
                    "metrics.k8s.io is not served by this cluster, so CPU and memory are not "
                    "reported. Install metrics-server to enable them."
                ),
            )
        cpu = 0.0
        memory = 0
        for pod in usage.get("items") or []:
            for container in pod.get("containers") or []:
                container_usage = container.get("usage") or {}
                cpu += parse_cpu(container_usage.get("cpu", "")) or 0.0
                memory += parse_memory(container_usage.get("memory", "")) or 0
        return ProviderMetrics(
            cpu_percent=round(cpu * 100, 2),
            memory_bytes=memory or None,
            replicas=replicas,
            healthy=bool(replicas),
            source=f"{self.id}/metrics.k8s.io",
        )


def _located(state: dict) -> tuple[str, str]:
    namespace = state.get("namespace")
    name = state.get("name")
    if not namespace or not name:
        raise ProviderError("This deployment has no Kubernetes namespace recorded.")
    return namespace, name


def _selector(state: dict) -> str:
    """The label selector for one deployment's objects.

    Read from the state the provider recorded rather than derived from the
    object name: the name is truncated differently from the label, and a
    selector reconstructed by string surgery would silently match nothing.
    """
    value = state.get("selector")
    if not value:
        raise ProviderError("This deployment has no Kubernetes label selector recorded.")
    return f"sutr.io/deployment={value}"


async def _create_if_absent(collection: str, body: dict) -> None:
    """Create an object, treating "already there" as success."""
    await request("POST", collection, body=body, tolerate=(409,))


async def _apply(collection: str, item: str, body: dict, *, patch: bool = False) -> None:
    """Create the object, or bring an existing one to this shape.

    A strategic-merge PATCH rather than a PUT for the update: a PUT needs the
    resourceVersion of the object being replaced, which means a read-then-write
    race, and it would also discard fields the cluster itself owns.
    """
    created = await request("POST", collection, body=body, tolerate=(409,))
    if created is not None:
        return
    await request(
        "PATCH",
        item,
        body=body,
        params={"fieldManager": "sutr"} if patch else None,
    )


def _unready_reason(status: dict) -> str | None:
    for condition in status.get("conditions") or []:
        if condition.get("status") == "False" and condition.get("message"):
            return str(condition["message"])
    return None

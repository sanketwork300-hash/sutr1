"""Local Docker deployment provider.

Runs generated MCP server packages as containers on the host's Docker daemon
via the `docker` CLI (universally present wherever a daemon is; no SDK
dependency). Containers are labeled `sutr.deployment=<id>`, bound to
127.0.0.1 on an ephemeral host port, and resource-capped.

Security posture (local provider, documented):
- The MCP endpoint binds to 127.0.0.1 only — it has no auth of its own.
- Runtime secrets are passed as container env vars; anyone with access to
  the local Docker daemon can read them (`docker inspect`) — which is the
  same trust domain as the Sutr server process itself on this host.
- This provider is disabled on cloud (multi-tenant) instances: running
  tenant-supplied containers on the API host is not a safe multi-tenant
  operation. See registry.py.
"""

import asyncio
import io
import shutil
import tempfile
import zipfile
from pathlib import Path

from sutr.deploy.base import (
    DeploymentProvider,
    DeploySpec,
    ProviderError,
    ProviderMetrics,
    ProviderStatus,
    ProviderTarget,
)

_BUILD_TIMEOUT = 900  # first build pulls the python base image
_CMD_TIMEOUT = 60


class DockerProvider(DeploymentProvider):
    id = "docker"
    display_name = "Local Docker"
    connection_provider = None
    creates = "A container on this host's Docker daemon, bound to 127.0.0.1."
    supports_update = True
    supports_metrics = True

    async def _run(self, *args: str, timeout: int = _CMD_TIMEOUT) -> str:
        """Run a docker CLI command; returns stdout, raises ProviderError."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise ProviderError("The docker CLI is not installed on the server host.")
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ProviderError(f"docker {args[0]} timed out after {timeout}s")
        if proc.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
            raise ProviderError(f"docker {args[0]} failed: {detail[:2000]}")
        return stdout.decode("utf-8", errors="replace").strip()

    async def available(self, target: ProviderTarget | None = None) -> tuple[bool, str | None]:
        try:
            await self._run("version", "--format", "{{.Server.Version}}", timeout=20)
            return True, None
        except ProviderError as exc:
            return False, str(exc)

    async def deploy(self, spec: DeploySpec) -> dict:
        short = str(spec.deployment_id)[:8]
        # Per-revision tag: the previous revision's image stays on the host, so
        # a rollback re-runs a retained artifact rather than rebuilding it.
        image_tag = f"sutr-deploy-{spec.slug}:{spec.artifact_tag}"
        container_name = f"sutr-{spec.slug}-{short}"
        # Keep the published port across an update so the deployment's URL —
        # which the user may already have configured in a client — survives.
        previous_port = (spec.previous_state or {}).get("host_port")
        port_binding = (
            f"127.0.0.1:{previous_port}:{spec.internal_port}"
            if previous_port
            else f"127.0.0.1:0:{spec.internal_port}"
        )

        build_dir = Path(tempfile.mkdtemp(prefix="sutr-deploy-"))
        try:
            with zipfile.ZipFile(io.BytesIO(spec.package_zip)) as archive:
                archive.extractall(build_dir)
            await self._run("build", "-t", image_tag, str(build_dir), timeout=_BUILD_TIMEOUT)
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)

        # A stale container from a failed prior attempt would collide on name.
        await self._silent("rm", "-f", container_name)

        run_args = [
            "run",
            "-d",
            "--name",
            container_name,
            "--label",
            f"sutr.deployment={spec.deployment_id}",
            "--restart",
            "unless-stopped",
            "--memory",
            "256m",
            "--cpus",
            "1",
            # Loopback only: the MCP endpoint has no auth of its own unless
            # the package is run with GOVERNANCE_MODE=platform.
            "-p",
            port_binding,
        ]
        for key, value in spec.env.items():
            run_args += ["-e", f"{key}={value}"]
        run_args += [
            image_tag,
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            str(spec.internal_port),
        ]
        container_id = await self._run(*run_args)

        port_line = await self._run("port", container_name, f"{spec.internal_port}/tcp")
        # e.g. "127.0.0.1:52341" (possibly multiple lines; take the first)
        host_port = port_line.splitlines()[0].rsplit(":", 1)[-1].strip()

        return {
            "container_id": container_id,
            "container_name": container_name,
            "image_tag": image_tag,
            "revision": spec.revision,
            "host_port": host_port,
            "url": f"http://127.0.0.1:{host_port}/mcp",
            "health_url": f"http://127.0.0.1:{host_port}/health",
        }

    async def metrics(self, state: dict, target: ProviderTarget) -> ProviderMetrics:
        """CPU and memory from `docker stats`, plus the container's health.

        Request counts and latencies are not reported: Docker does not see
        them, and inventing them from log lines would be a guess presented as
        a measurement.
        """
        name = state.get("container_name")
        if not name:
            return ProviderMetrics(
                source="docker", unavailable_reason="The deployment has no container."
            )
        try:
            line = await self._run(
                "stats",
                "--no-stream",
                "--format",
                "{{.CPUPerc}}|{{.MemUsage}}",
                name,
                timeout=30,
            )
        except ProviderError as exc:
            return ProviderMetrics(source="docker", unavailable_reason=str(exc))

        cpu_percent, memory_bytes, memory_limit = _parse_stats(line)
        healthy = None
        try:
            status = await self._run("inspect", "-f", "{{.State.Running}}", name, timeout=20)
            healthy = status.strip().lower() == "true"
        except ProviderError:
            healthy = None
        return ProviderMetrics(
            cpu_percent=cpu_percent,
            memory_bytes=memory_bytes,
            memory_limit_bytes=memory_limit,
            replicas=1 if healthy else 0,
            healthy=healthy,
            source="docker stats",
        )

    async def _silent(self, *args: str) -> None:
        try:
            await self._run(*args)
        except ProviderError:
            pass

    async def status(self, state: dict, target: ProviderTarget) -> ProviderStatus:
        name = state.get("container_name")
        if not name:
            return ProviderStatus(state="not_found", detail="No container recorded.")
        try:
            raw = await self._run("inspect", "-f", "{{.State.Status}}|{{.State.Error}}", name)
        except ProviderError as exc:
            if "No such object" in str(exc) or "No such container" in str(exc):
                return ProviderStatus(state="not_found", detail="Container no longer exists.")
            return ProviderStatus(state="error", detail=str(exc))
        docker_state, _, docker_error = raw.partition("|")
        if docker_state == "running":
            return ProviderStatus(state="running")
        if docker_state in ("exited", "created", "paused", "dead"):
            return ProviderStatus(
                state="stopped", detail=docker_error or f"container is {docker_state}"
            )
        return ProviderStatus(state="error", detail=f"unexpected state '{docker_state}'")

    async def start(self, state: dict, target: ProviderTarget) -> None:
        await self._run("start", state["container_name"])

    async def stop(self, state: dict, target: ProviderTarget) -> None:
        await self._run("stop", state["container_name"])

    async def remove(self, state: dict, target: ProviderTarget) -> None:
        name = state.get("container_name")
        if name:
            await self._silent("rm", "-f", name)
        image = state.get("image_tag")
        if image:
            await self._silent("rmi", image)

    async def logs(self, state: dict, target: ProviderTarget, tail: int = 100) -> str:
        name = state.get("container_name")
        if not name:
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "logs",
                "--tail",
                str(tail),
                name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_CMD_TIMEOUT)
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            raise ProviderError(f"docker logs failed: {exc}")
        return stdout.decode("utf-8", errors="replace")


_MEM_UNITS = {
    "b": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
}


def _parse_size(text: str) -> int | None:
    """Parse a `docker stats` size such as "12.3MiB" into bytes."""
    text = text.strip().lower()
    for unit, factor in sorted(_MEM_UNITS.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(unit):
            try:
                return int(float(text[: -len(unit)]) * factor)
            except ValueError:
                return None
    return None


def _parse_stats(line: str) -> tuple[float | None, int | None, int | None]:
    """Parse "12.34%|45MiB / 256MiB" into (cpu %, used bytes, limit bytes)."""
    cpu_text, _, memory_text = line.partition("|")
    try:
        cpu = float(cpu_text.strip().rstrip("%"))
    except ValueError:
        cpu = None
    used_text, _, limit_text = memory_text.partition("/")
    return cpu, _parse_size(used_text), _parse_size(limit_text)

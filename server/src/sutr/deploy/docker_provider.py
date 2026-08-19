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

from sutr.deploy.base import DeploymentProvider, DeploySpec, ProviderError, ProviderStatus

_BUILD_TIMEOUT = 900  # first build pulls the python base image
_CMD_TIMEOUT = 60


class DockerProvider(DeploymentProvider):
    id = "docker"
    display_name = "Local Docker"

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

    async def available(self) -> tuple[bool, str | None]:
        try:
            await self._run("version", "--format", "{{.Server.Version}}", timeout=20)
            return True, None
        except ProviderError as exc:
            return False, str(exc)

    async def deploy(self, spec: DeploySpec) -> dict:
        short = str(spec.deployment_id)[:8]
        image_tag = f"sutr-deploy-{spec.slug}:{short}"
        container_name = f"sutr-{spec.slug}-{short}"

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
            # Loopback only: the MCP endpoint has no auth of its own.
            "-p",
            f"127.0.0.1:0:{spec.internal_port}",
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
            "host_port": host_port,
            "url": f"http://127.0.0.1:{host_port}/mcp",
            "health_url": f"http://127.0.0.1:{host_port}/health",
        }

    async def _silent(self, *args: str) -> None:
        try:
            await self._run(*args)
        except ProviderError:
            pass

    async def status(self, state: dict) -> ProviderStatus:
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

    async def start(self, state: dict) -> None:
        await self._run("start", state["container_name"])

    async def stop(self, state: dict) -> None:
        await self._run("stop", state["container_name"])

    async def remove(self, state: dict) -> None:
        name = state.get("container_name")
        if name:
            await self._silent("rm", "-f", name)
        image = state.get("image_tag")
        if image:
            await self._silent("rmi", image)

    async def logs(self, state: dict, tail: int = 100) -> str:
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

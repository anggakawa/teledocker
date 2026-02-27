"""Async Docker API wrapper using aiodocker.

Encapsulates all container operations so the routers and background tasks
never interact with aiodocker directly. This makes the API surface testable
with a mock and hides aiodocker-specific quirks.
"""

import logging
from collections.abc import AsyncGenerator

import aiodocker
from aiodocker.containers import DockerContainer

logger = logging.getLogger(__name__)


def _is_not_found(exc: Exception) -> bool:
    """Check if an aiodocker exception is a 404 Not Found.

    aiodocker.DockerError stores the HTTP status in its .status attribute.
    Using getattr avoids a hard import dependency on the exception class.
    """
    return getattr(exc, "status", None) == 404


# Resource limits applied to every user container.
_CPU_QUOTA = 100_000   # 100ms per 100ms period = 1 core
_CPU_PERIOD = 100_000
_MEMORY_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
_PIDS_LIMIT = 256


class DockerClient:
    """Async wrapper around aiodocker for container lifecycle operations."""

    def __init__(self):
        self._docker: aiodocker.Docker | None = None

    async def connect(self) -> None:
        """Open the Docker socket connection. Called once at app startup."""
        self._docker = aiodocker.Docker()
        logger.info("Connected to Docker daemon")

    async def disconnect(self) -> None:
        """Close the Docker socket connection. Called at app shutdown."""
        if self._docker is not None:
            await self._docker.close()

    def _docker_or_raise(self) -> aiodocker.Docker:
        if self._docker is None:
            raise RuntimeError("DockerClient not connected. Call connect() first.")
        return self._docker

    async def create_container(
        self,
        container_name: str,
        user_id: str,
        env_vars: dict[str, str],
        agent_image: str,
        workspace_base_path: str,
        agent_network: str | None = None,
    ) -> str:
        """Create and start a user container. Returns the container ID.

        Security hardening applied:
        - Runs as non-root user (uid 1000).
        - No new privileges flag set.
        - PID limit enforced.
        - CPU and memory limits enforced.
        - Containers only join the agent-net bridge network (no access to
          internal services like postgres, redis, or api-server).
        """
        docker = self._docker_or_raise()

        env_list = [f"{key}={value}" for key, value in env_vars.items()]
        workspace_volume_name = f"workspace-{user_id}"

        container_config = {
            "Image": agent_image,
            "Hostname": container_name,
            "Env": env_list,
            "User": "1000:1000",
            # Expose the agent bridge port only to the agent-net Docker network.
            "ExposedPorts": {"9100/tcp": {}},
            "HostConfig": {
                "CpuQuota": _CPU_QUOTA,
                "CpuPeriod": _CPU_PERIOD,
                "Memory": _MEMORY_BYTES,
                "PidsLimit": _PIDS_LIMIT,
                "SecurityOpt": ["no-new-privileges:true"],
                # Writable workspace volume; /tmp as tmpfs.
                "Binds": [f"{workspace_volume_name}:/workspace"],
                "Tmpfs": {"/tmp": "size=256m"},
                "RestartPolicy": {"Name": "no"},
            },
            "Labels": {
                "chatops.user_id": user_id,
                "chatops.managed": "true",
            },
        }

        container: DockerContainer = await docker.containers.create(
            config=container_config,
            name=container_name,
        )
        await container.start()

        # Connect the container to the agent network so container-manager
        # can reach the agent-bridge WebSocket on port 9100.
        if agent_network:
            network = await docker.networks.get(agent_network)
            await network.connect({"Container": container_name})

        container_info = await container.show()
        logger.info("Created container %s for user %s", container_name, user_id)
        return container_info["Id"]

    async def get_container_name(self, container_id: str) -> str:
        """Resolve a container ID to its name (used as DNS hostname)."""
        docker = self._docker_or_raise()
        container = docker.containers.container(container_id)
        info = await container.show()
        # Docker returns /container_name, strip the leading slash.
        return info["Name"].lstrip("/")

    async def stop_container(self, container_id: str) -> None:
        """Gracefully stop a container (SIGTERM, then SIGKILL after 10s)."""
        docker = self._docker_or_raise()
        container = docker.containers.container(container_id)
        await container.stop(t=10)
        logger.info("Stopped container %s", container_id)

    async def restart_container(self, container_id: str) -> None:
        """Restart a container."""
        docker = self._docker_or_raise()
        container = docker.containers.container(container_id)
        await container.restart(t=10)
        logger.info("Restarted container %s", container_id)

    async def remove_container(self, container_id: str, with_volume: bool = True) -> None:
        """Remove a container and optionally its associated volumes."""
        docker = self._docker_or_raise()
        container = docker.containers.container(container_id)
        try:
            await container.stop(t=5)
        except Exception:
            pass  # Container may already be stopped.
        try:
            await container.delete(v=with_volume, force=True)
        except Exception as exc:
            if _is_not_found(exc):
                logger.warning("Container %s already removed, skipping.", container_id)
                return
            raise
        logger.info("Removed container %s (with_volume=%s)", container_id, with_volume)

    async def pause_container(self, container_id: str) -> None:
        """Freeze all processes in a container using the cgroups freezer."""
        docker = self._docker_or_raise()
        container = docker.containers.container(container_id)
        await container.pause()
        logger.info("Paused container %s", container_id)

    async def unpause_container(self, container_id: str) -> None:
        """Resume a paused container — typically completes in < 1 second."""
        docker = self._docker_or_raise()
        container = docker.containers.container(container_id)
        await container.unpause()
        logger.info("Unpaused container %s", container_id)

    async def exec_command(
        self, container_id: str, command: str
    ) -> AsyncGenerator[str, None]:
        """Run a shell command inside the container and stream stdout/stderr.

        Yields each line of output as it arrives so callers can stream SSE.
        """
        docker = self._docker_or_raise()
        container = docker.containers.container(container_id)

        exec_instance = await container.exec(
            cmd=["/bin/sh", "-c", command],
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
        )
        async with exec_instance.start(detach=False) as stream:
            async for _, data in stream:
                if data:
                    # Decode bytes; strip trailing newline for clean SSE lines.
                    yield data.decode("utf-8", errors="replace").rstrip("\n")

    async def get_container_status(self, container_id: str) -> str:
        """Return the container state string (running, paused, exited, etc.)."""
        docker = self._docker_or_raise()
        container = docker.containers.container(container_id)
        info = await container.show()
        return info["State"]["Status"]

    async def get_container_stats(self, container_id: str) -> dict:
        """Return a snapshot of CPU%, memory, and basic info for the container."""
        docker = self._docker_or_raise()
        container = docker.containers.container(container_id)
        # stream=False returns a single stats snapshot instead of a continuous stream.
        stats = await container.stats(stream=False)
        if not stats:
            return {}
        snapshot = stats[0] if isinstance(stats, list) else stats

        cpu_delta = (
            snapshot["cpu_stats"]["cpu_usage"]["total_usage"]
            - snapshot["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        system_delta = (
            snapshot["cpu_stats"]["system_cpu_usage"]
            - snapshot["precpu_stats"]["system_cpu_usage"]
        )
        num_cpus = snapshot["cpu_stats"].get("online_cpus", 1)
        cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0 if system_delta > 0 else 0.0

        memory_usage = snapshot["memory_stats"].get("usage", 0)
        memory_limit = snapshot["memory_stats"].get("limit", 1)
        memory_percent = (memory_usage / memory_limit) * 100.0

        return {
            "cpu_percent": round(cpu_percent, 2),
            "memory_usage_mb": round(memory_usage / 1024 / 1024, 1),
            "memory_limit_mb": round(memory_limit / 1024 / 1024, 1),
            "memory_percent": round(memory_percent, 2),
        }

"""Docker stack control for the IMURUN full stack.

The UI can build and start the FISH engine, the Python API and the frontend
without leaving the browser. Every command runs in the project root that owns
``docker-compose.yml`` and is bounded by a timeout so a stuck daemon never
wedges a request thread.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def locate_root(start: Path | None = None) -> Path:
    """Find the directory that owns docker-compose.yml."""
    override = os.getenv("IMURUN_ROOT")
    if override:
        candidate = Path(override).expanduser().resolve()
        if (candidate / "docker-compose.yml").is_file():
            return candidate

    here = (start or Path(__file__)).resolve()
    for directory in [here, *here.parents]:
        if (directory / "docker-compose.yml").is_file():
            return directory
        if (directory / "imurun" / "docker-compose.yml").is_file():
            return directory / "imurun"

    # No compose file anywhere: report a directory (``here`` is stack.py
    # itself by default, which gave ".../stack.py/docker-compose.yml").
    return here.parent if here.is_file() else here


class StackController:
    """Build, start, stop and inspect the compose stack."""

    def __init__(self, root: Path | str | None = None, fish_health_url: str | None = None) -> None:
        self.root = Path(root).resolve() if root else locate_root()
        self.compose_file = self.root / "docker-compose.yml"
        self.fish_health_url = fish_health_url or os.getenv(
            "FISH_HEALTH_URL", "http://127.0.0.1:8731/fish/health"
        )
        self._last_action: dict | None = None
        # If the native FISH engine is available, docker is not needed for the
        # engine. The stack controller will still manage the other services.
        self._fish_native = None

    def set_native_fish(self, client) -> None:
        """Register an in-process FISH client so ensure() skips the container."""
        self._fish_native = client

    # ------------------------------------------------------------- helpers

    def docker_path(self) -> str | None:
        return shutil.which("docker")

    def _run(self, args: list[str], timeout: float) -> dict:
        docker = self.docker_path()
        if docker is None:
            return {
                "ok": False,
                "error": "docker is not installed or not on PATH",
                "command": "docker " + " ".join(args),
            }
        if not self.compose_file.is_file():
            return {
                "ok": False,
                "error": f"docker-compose.yml not found under {self.root}",
                "command": "docker " + " ".join(args),
            }
        try:
            completed = subprocess.run(
                [docker, "compose", *args],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"docker compose {' '.join(args)} timed out after {timeout:g}s",
                "command": "docker " + " ".join(args),
            }
        except OSError as exc:
            return {
                "ok": False,
                "error": f"could not run docker: {exc}",
                "command": "docker " + " ".join(args),
            }

        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": (completed.stdout or "")[-8000:],
            "stderr": (completed.stderr or "")[-8000:],
            "command": "docker " + " ".join(args),
        }

    def _fish_healthy(self) -> bool:
        # Check native in-process engine first
        if self._fish_native is not None and getattr(self._fish_native, "health", lambda: False)():
            return True
        # Fall back to HTTP health check for docker-based engine
        try:
            with urllib.request.urlopen(self.fish_health_url, timeout=2.0) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _services(self) -> list[dict]:
        result = self._run(["ps", "--format", "json"], timeout=20.0)
        if not result.get("ok"):
            return []
        services = []
        for line in (result.get("stdout") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                services.append(json.loads(line))
            except ValueError:
                continue
        return services

    # -------------------------------------------------------------- actions

    def status(self) -> dict:
        docker = self.docker_path()
        services = self._services() if docker else []
        running = [
            service for service in services
            if str(service.get("State", "")).lower() == "running"
        ]
        return {
            "docker": bool(docker),
            "compose_file": str(self.compose_file),
            "root": str(self.root),
            "services": services,
            "running": len(running),
            "total": len(services),
            "fish_healthy": self._fish_healthy(),
            "last_action": self._last_action,
        }

    def build(self, timeout: float = 1800.0, service: str = "fish-engine") -> dict:
        args = ["build", "--parallel"] + ([service] if service else [])
        result = self._run(args, timeout=timeout)
        self._last_action = {"action": "build", "at": time.time(), "ok": result.get("ok", False)}
        return result

    def start(self, timeout: float = 300.0, service: str = "fish-engine") -> dict:
        # Only the engine starts by default: the Python API already runs on the
        # host (start.py). ``service=None`` starts the whole compose stack for
        # the container-only deployment.
        args = ["up", "-d", "--remove-orphans"] + ([service] if service else [])
        result = self._run(args, timeout=timeout)
        self._last_action = {"action": "start", "at": time.time(), "ok": result.get("ok", False)}
        return result

    def stop(self, timeout: float = 120.0) -> dict:
        result = self._run(["down"], timeout=timeout)
        self._last_action = {"action": "stop", "at": time.time(), "ok": result.get("ok", False)}
        return result

    def ensure(self, wait_seconds: float = 60.0) -> dict:
        """Build when images are missing, start, then wait for FISH health."""
        # If the native FISH engine is running in-process, the engine is already
        # available and docker is not needed for it.
        if self._fish_native is not None and getattr(self._fish_native, "health", lambda: False)():
            return {
                "ok": True,
                "status": "native",
                "fish_healthy": True,
            }

        if self._fish_healthy():
            return {
                "ok": True,
                "status": "already-running",
                "fish_healthy": True,
            }

        # ``--build`` is a no-op when the cached layers are current, and it
        # makes the first run work without a separate build click.
        started = self._run(
            ["up", "-d", "--build", "--remove-orphans", "fish-engine"],
            timeout=1800.0,
        )
        self._last_action = {
            "action": "ensure",
            "at": time.time(),
            "ok": started.get("ok", False),
        }
        if not started.get("ok"):
            return {
                "ok": False,
                "status": "start-failed",
                "start": started,
            }

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if self._fish_healthy():
                return {
                    "ok": True,
                    "status": "started",
                    "fish_healthy": True,
                }
            time.sleep(2.0)

        return {
            "ok": True,
            "status": "starting",
            "fish_healthy": False,
            "detail": "containers started; the engine is still warming up",
        }

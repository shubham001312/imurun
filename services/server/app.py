"""Compose and run the loopback backend.

This is the only place that wires concrete stores. The model is injected: until
the FISH engine is present, a turn answers 503 instead of pretending to think.
The bearer token goes to a private runtime file, never to a log.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from ..connectors.registry import ConnectorRegistry
from ..persona.store import PersonaStore
from ..prompts.queue import PromptQueue
from ..sessions.transcript import TranscriptStore
from ..skills.store import SkillsStore
from .api import ApiServer


def default_root() -> Path:
    return Path(os.path.expanduser("~/.imurun"))


def default_static_root() -> Path | None:
    root = Path(__file__).resolve().parents[2] / "frontend" / "web"
    return root if root.is_dir() else None


def runtime_path(root: Path | str | None = None) -> Path:
    return (Path(root) if root else default_root()) / "runtime" / "api.json"


def _private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        if os.name != "nt":
            raise


def write_runtime(
    root: Path | str | None,
    port: int,
    token: str,
    *,
    pid: int | None = None,
) -> Path:
    """Write the token bootstrap file for the TUI, readable only by the user."""
    if not token:
        raise ValueError("token is required")
    if not 0 < port < 65536:
        raise ValueError("port is out of range")
    path = runtime_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _private(path.parent, 0o700)
    data = {
        "url": f"http://127.0.0.1:{port}/",
        "token": token,
        "pid": pid if pid is not None else os.getpid(),
        "started_at": time.time(),
    }
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data), encoding="utf-8")
        _private(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path


def clear_runtime(root: Path | str | None = None) -> None:
    runtime_path(root).unlink(missing_ok=True)


def build_server(
    *,
    root: Path | str | None = None,
    model=None,
    connectors: ConnectorRegistry | None = None,
    oauth_flows: dict | None = None,
    static_root: Path | str | None = None,
    port: int = 0,
    token: str | None = None,
    kill=None,
    persona=None,
    mark=None,
) -> ApiServer:
    home = Path(root) if root else default_root()
    sessions = home / "sessions"
    return ApiServer(
        persona_store=PersonaStore(home / "persona.json"),
        connectors=connectors or ConnectorRegistry(),
        queue=PromptQueue(),
        sessions_root=sessions,
        skills=SkillsStore(home / "skills"),
        transcripts=TranscriptStore(sessions),
        static_root=static_root if static_root is not None else default_static_root(),
        model=model,
        oauth_flows=oauth_flows,
        token=token,
        port=port,
        kill=kill,
        persona=persona,
        mark=mark,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="imurun-server",
        description="Run the IMURUN loopback API (127.0.0.1 only).",
    )
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--root", default=None, help="data root (default ~/.imurun)")
    parser.add_argument("--static", default=None, help="static shell directory")
    args = parser.parse_args(argv)

    home = Path(args.root).expanduser() if args.root else default_root()
    server = build_server(
        root=home,
        static_root=args.static,
        port=args.port,
    )
    server.start()
    path = write_runtime(home, server.port, server.token)
    print(f"IMURUN API: http://127.0.0.1:{server.port}/")
    print(f"runtime: {path}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        clear_runtime(home)
        server.stop()
    return 0

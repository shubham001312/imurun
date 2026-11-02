"""Compose and run the loopback backend.

This is the only place that wires concrete stores. The model is injected: the
FISH engine provides the model client via HTTP. The bearer token goes to a
private runtime file, never to a log.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from ..config import ServerConfig, load_config
from ..connectors.registry import ConnectorRegistry
from ..persona.store import PersonaStore
from ..prompts.queue import PromptQueue
from ..sessions.transcript import TranscriptStore
from ..skills.store import SkillsStore
from .api import ApiServer
try:  # run as ``imurun.services.server.app``
    from ..model.fish_client import SyncFishModelClient
    from ..model.engine_client import EngineModelClient
except ImportError:  # run as ``services.server.app`` from the imurun directory
    from model.fish_client import SyncFishModelClient
    from model.engine_client import EngineModelClient

# Optional FastAPI models API (requires huggingface_hub, fastapi)
try:
    from .models_api import router as models_router
    _HAS_MODELS_API = True
except ImportError:
    models_router = None
    _HAS_MODELS_API = False


def default_root() -> Path:
    return Path(os.path.expanduser("~/.imurun"))


def default_static_root() -> Path | None:
    root = Path(__file__).resolve().parents[2] / "frontend" / "design-lab"
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
    config=None,
    tools=None,
) -> ApiServer:
    cfg = config or load_config()
    home = Path(root) if root else cfg.data_root or default_root()
    sessions = home / "sessions"

    # Single compute path: the native FISH engine only.
    # No llama.cpp fallback.
    if model is None:
        model = EngineModelClient(
            fish=SyncFishModelClient(cfg.fish),
        )

    # The live-fact tools: the clock, the weather, web search, page reading
    # and the user's locality. They are created once and shared; each one
    # reports its own availability.
    if tools is None:
        try:
            from ..tools import ToolRunner, default_registry

            tools = ToolRunner(default_registry())
        except Exception as exc:  # noqa: BLE001 - the shell still works without them
            print(f"tools: unavailable ({exc})", file=sys.stderr)
            tools = None

    server = ApiServer(
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
        # Port 0 means "any free port". The command line resolves the
        # configured port itself, so a caller that does not care never steals
        # the port of a running shell.
        port=port,
        kill=kill,
        persona=persona,
        mark=mark,
        data_root=home,
        tools=tools,
    )

    # Research engine: model-issued searches publish research.* events, and
    # the planner can use the raw model for its routing generation even on
    # paths that do not go through the agent loop (manual tool runs).
    if tools is not None:
        try:
            from ..research.events import set_publisher

            set_publisher(server._events.publish)
            tools.publish = server._events.publish
            tools.planner_model = model
            tools.policy = getattr(getattr(server, "_executor", None), "policy", None)
        except Exception as exc:  # noqa: BLE001 - research is optional
            print(f"research: not wired ({exc})", file=sys.stderr)

    # NVIDIA-hosted models: the engine facade gets its remote client and the
    # user's last hosted choice is restored (services/remote/nvidia.py).
    try:
        from ..remote.nvidia import attach as attach_remote

        attach_remote(server, model, home)
    except Exception as exc:  # noqa: BLE001 - hosted models are optional
        print(f"remote models: not wired ({type(exc).__name__})", file=sys.stderr)

    # Wire the native FISH client into the stack controller so it knows the
    # engine is already available in-process and skips docker-compose.
    if hasattr(model, "_fish") and model._fish is not None:
        server._stack.set_native_fish(model._fish)
    elif hasattr(model, "_async_client") and hasattr(model._async_client, "_native"):
        server._stack.set_native_fish(model._async_client._native)

    # Start the idle-unload watchdog (no warm-start — model loads on first use).
    def _start_idle_watchdog(cfg: ServerConfig) -> None:
        import threading
        import time

        fish_client = None
        if hasattr(model, "_fish") and model._fish is not None:
            fish_client = model._fish
        elif hasattr(model, "_async_client") and hasattr(model._async_client, "_native"):
            fish_client = model._async_client._native

        if fish_client is None:
            return

        def watchdog() -> None:
            idle_timeout = getattr(cfg.fish, "idle_timeout_seconds", 300.0)
            check_interval = 30.0
            while True:
                time.sleep(check_interval)
                try:
                    if hasattr(fish_client, "unload_if_idle"):
                        fish_client.unload_if_idle(idle_timeout)
                except Exception:  # noqa: BLE001 - watchdog must never crash
                    pass

        threading.Thread(target=watchdog, name="imurun-idle-watchdog", daemon=True).start()

    _start_idle_watchdog(cfg)

    # Production composition registers the eight connectors and any OAuth flow
    # whose client id is configured. Tests that inject their own registry keep
    # exactly that registry.
    if connectors is None:
        try:
            from ..connectors.wiring import wire_server

            wire_server(server, home)
        except Exception as exc:  # noqa: BLE001 - a missing keychain must not stop the shell
            print(f"connectors: not wired ({exc})", file=sys.stderr)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="imurun",
        description="Start IMURUN: loopback API, shell, browser and FISH engine.",
    )
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--root", default=None, help="data root (default ~/.imurun)")
    parser.add_argument("--static", default=None, help="static shell directory")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--no-stack", action="store_true", help="do not start the FISH engine")
    args = parser.parse_args(argv)

    cfg = load_config()
    home = Path(args.root).expanduser() if args.root else cfg.data_root or default_root()
    server = build_server(
        root=home,
        static_root=args.static,
        port=args.port or cfg.port,
        config=cfg,
    )
    server.start()
    path = write_runtime(home, server.port, server.token)
    url = f"http://127.0.0.1:{server.port}/"
    print(f"IMURUN: {url}")
    print(f"runtime: {path}")

    if not args.no_stack and os.getenv("IMURUN_AUTOSTART", "1") != "0":
        def bring_up_engine() -> None:
            try:
                result = server._stack.ensure()
                print(f"engine: {result.get('status', 'unknown')}")
            except Exception as exc:  # noqa: BLE001 - the UI reports the state
                print(f"engine: unavailable ({exc})")

        threading.Thread(target=bring_up_engine, name="imurun-stack", daemon=True).start()

    if not args.no_browser and os.getenv("IMURUN_NO_BROWSER", "0") != "1":
        def open_shell() -> None:
            time.sleep(0.6)
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - a missing browser must not stop the server
                pass

        threading.Thread(target=open_shell, name="imurun-browser", daemon=True).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        clear_runtime(home)
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

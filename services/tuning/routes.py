"""HTTP handlers for ``/api/tuning/*`` (dispatched from services/server/api.py).

Every handler takes the ``ApiServer`` and returns ``(status, payload)``;
``None`` from ``handle_get`` means "not my route".

GET  /api/tuning/status             stack, hardware, plan for the selected model, samples, jobs
GET  /api/tuning/settings           the `tuning` section + whether an HF token is set
POST /api/tuning/settings           update `tuning` (validated) and/or `hf_token` (write-only)
GET  /api/tuning/plan/{model_id}    the feasibility plan for one model
POST /api/tuning/jobs               start now {model_id?, allow_slow?, confirm?, resume?, epochs?,
                                    max_steps?, min_samples?, quant?, max_hours?}
GET  /api/tuning/jobs               all jobs, newest first
GET  /api/tuning/jobs/{id}          one job + metrics.jsonl rows
POST /api/tuning/jobs/{id}/cancel
POST /api/tuning/feedback           {turn_id, rating: up|down|teach|clear, correction?, session_id?}
GET  /api/tuning/datasets           user files, sample counts, drop reasons, manifest, built sets
POST /api/tuning/setup              install the training venv in the background (`tuning.setup` events)
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from . import data as data_mod
from . import feasibility, registry as registry_mod, setup as setup_mod, stack
from .backend import SubprocessBackend
from .catalogue import BASES, base_for, catalogue_id_for_filename, parent_id
from .feedback import FeedbackStore
from .paths import TuningPaths
from .scheduler import Scheduler, validate_settings

Reply = tuple[int, dict]
MASK = "••••••••"
PREFIX = "/api/tuning"


class TuningService:
    """Everything the routes need, built once per ApiServer."""

    def __init__(self, server: Any, *, backend=None, tick_seconds: float = 60.0) -> None:
        home = Path(getattr(server, "_home", None) or Path.home() / ".imurun")
        self.server = server
        self.paths = TuningPaths.at(home / "tuning")
        sessions_root = getattr(server, "_sessions_root", None) or home / "sessions"
        self.feedback = FeedbackStore(sessions_root)
        self.started_at = time.time()
        self.setup_state: dict = {"running": False}
        self._setup_lock = threading.Lock()
        self.scheduler = Scheduler(
            self.paths,
            settings=getattr(server, "_settings", None),
            feedback=self.feedback,
            transcripts=getattr(server, "_transcripts", None),
            backend=backend or SubprocessBackend(self.paths),
            publish=self._publish,
            is_busy=self.is_busy,
            last_activity=self.last_activity,
            selected_model=self.selected_model,
            renderer_for=self.renderer_for,
            tick_seconds=tick_seconds,
            resources=getattr(server, "_governor", None),
        )
        governor = getattr(server, "_governor", None)
        if governor is not None:
            # The trainer's CPU is IMURUN's own load, not the user's.
            def trainer_pids(backend=self.scheduler.backend):
                proc = getattr(backend, "proc", None)
                return [proc.pid] if proc is not None and proc.poll() is None else []

            governor.add_pids(trainer_pids)
        try:
            registry_mod.register_all(self.scheduler.registry)
        except Exception:  # noqa: BLE001 - a broken registry must not stop the server
            pass

    # ------------------------------------------------------------ engine hooks
    def _publish(self, kind: str, payload: dict) -> None:
        events = getattr(self.server, "_events", None)
        if events is not None:
            events.publish(kind, payload)

    def _turns(self) -> list:
        turns = getattr(self.server, "_turns", None)
        try:
            return list(turns.list()) if turns is not None else []
        except Exception:  # noqa: BLE001
            return []

    def is_busy(self) -> bool:
        """True while any chat turn is generating (training pauses)."""
        return any(getattr(t, "state", "") == "running" for t in self._turns())

    def last_activity(self) -> float:
        times = [self.started_at]
        for turn in self._turns():
            times.append(float(getattr(turn, "finished_at", 0) or getattr(turn, "started_at", 0) or 0))
        return max(times)

    def selected_model(self) -> str:
        model = getattr(self.server, "_model", None)
        name = ""
        getter = getattr(model, "selected_name", None)
        if callable(getter):
            try:
                name = str(getter() or "")
            except Exception:  # noqa: BLE001
                name = ""
        found = catalogue_id_for_filename(name)
        if found:
            return found
        try:
            from services.config import load_last_model

            return str(load_last_model(Path(self.server._home)) or "")
        except Exception:  # noqa: BLE001
            return ""

    def renderer_for(self, parent: str):
        """The engine's own chat template, when the engine has this model family loaded.

        Read-only use of ``fish_chat_prompt`` under ``_gen_lock`` (a short hold;
        the scheduler only builds datasets when no turn is running). Returns
        None when the engine is not loaded or serves a different model, so the
        trainer falls back to the Hugging Face template.
        """
        model = getattr(self.server, "_model", None)
        native = getattr(model, "_native", None)
        if native is None or not getattr(native, "loaded", False):
            return None
        path = getattr(native, "model_path", None)
        loaded = catalogue_id_for_filename(Path(path).name if path else "")
        if not loaded or parent_id(loaded) != parent:
            return None
        lock = getattr(native, "_gen_lock", None)

        def render(messages: list[dict]) -> str:
            if lock is not None and not lock.acquire(timeout=5):
                raise RuntimeError("engine busy")
            try:
                return native.chat_prompt(messages)
            finally:
                if lock is not None:
                    lock.release()

        return render

    # ------------------------------------------------------------ views
    def status(self) -> dict:
        sched = self.scheduler
        cfg = sched.config()
        stack_view = sched.backend.stack()
        selected = self.selected_model()
        target = sched.resolve_model(None)
        plan = None
        samples = {"usable": 0}
        if target and base_for(target) is not None:
            parent = parent_id(target)
            kept, dropped, sources = sched.samples(parent)
            samples = {"usable": len(kept), "min_samples": cfg["min_samples"], "dropped": dropped,
                       "sources": {k: v for k, v in sources.items() if k != "files_found"}}
            plan = sched.plan_for(parent).view()
        active = sched.active()
        jobs = sched.jobs()
        return {
            "enabled": bool(cfg["enabled"]), "auto": cfg["auto"],
            "stack": stack_view,
            "hardware": feasibility.probe_hardware(self.paths.root).view(),
            "selected_model": selected, "target_model": target,
            "trainable": bool(target and base_for(target) is not None),
            "plan": plan, "samples": samples,
            "feedback": self.feedback.counts(),
            "active_job": active.view() if active else None,
            "last_job": jobs[0] if jobs and not active else None,
            "tuned_models": sched.registry.entries(),
            "automation": {"last": sched.last_auto, "due": sched.auto_due(cfg)[1]},
            "setup": dict(self.setup_state),
            "paths": {"root": str(self.paths.root), "datasets": str(self.paths.datasets)},
        }

    def settings_view(self) -> dict:
        cfg = self.scheduler.config()
        return {"tuning": cfg, "hf_token": MASK if self.scheduler.hf_token() else ""}

    # ------------------------------------------------------------ setup
    def start_setup(self, payload: dict) -> Reply:
        with self._setup_lock:
            if self.setup_state.get("running"):
                return 409, {"error": "setup is already running", "setup": dict(self.setup_state)}
            self.setup_state = {"running": True, "stage": "starting", "percent": 0.0,
                                "started_at": time.time()}

        def progress(stage: str, percent: float, message: str) -> None:
            self.setup_state.update(stage=stage, percent=round(float(percent), 1), message=message)
            self._publish("tuning.setup", dict(self.setup_state))

        def run() -> None:
            try:
                record = setup_mod.run_setup(self.paths, progress=progress,
                                             torch_index=payload.get("torch_index") or None,
                                             skip_bnb=bool(payload.get("skip_bnb")))
                self.setup_state.update(running=False, ok=record.get("ok"), error=record.get("error"),
                                        venv_bytes=record.get("venv_bytes"))
            except Exception as exc:  # noqa: BLE001
                self.setup_state.update(running=False, ok=False, error=f"{type(exc).__name__}: {exc}")
            self._publish("tuning.setup", dict(self.setup_state))

        snapshot = dict(self.setup_state)
        threading.Thread(target=run, name="tuning-setup", daemon=True).start()
        return 202, {"setup": snapshot}


# ---------------------------------------------------------------- wiring

def attach(server: Any, **kwargs) -> TuningService | None:
    """Create the service and start the automation ticker (idle unless enabled)."""
    if getattr(server, "_tuning", None) is not None:
        return server._tuning
    try:
        service = TuningService(server, **kwargs)
        service.scheduler.start()
    except Exception:  # noqa: BLE001 - tuning must never stop the server from starting
        return None
    server._tuning = service
    return service


def detach(server: Any) -> None:
    service = getattr(server, "_tuning", None)
    if service is not None:
        try:
            service.scheduler.stop()
        except Exception:  # noqa: BLE001
            pass


def _service(server: Any) -> TuningService | None:
    return getattr(server, "_tuning", None) or attach(server)


def is_post_route(path: str) -> bool:
    return path.startswith(PREFIX + "/")


def handle_get(server: Any, path: str, query: dict | None = None) -> Reply | None:
    if not path.startswith(PREFIX + "/"):
        return None
    service = _service(server)
    if service is None:
        return 500, {"error": "the tuning channel failed to start"}
    sched = service.scheduler
    parts = [p for p in path.split("/") if p][2:]
    try:
        if parts == ["status"]:
            return 200, service.status()
        if parts == ["feedback"]:
            # Ratings of one session, latest mark per turn (frontend request 4).
            values = (query or {}).get("session_id") or []
            session_id = str(values[0] if values else "").strip()
            if not session_id:
                return 400, {"error": "session_id is required"}
            marks = service.feedback.for_session(session_id)
            return 200, {"session_id": session_id, "turns": {
                turn_id: {"rating": fb.rating, "consented": fb.consented,
                          **({"correction": fb.correction} if fb.correction else {})}
                for turn_id, fb in marks.items()}}
        if parts == ["settings"]:
            return 200, service.settings_view()
        if parts == ["jobs"]:
            return 200, {"jobs": sched.jobs()}
        if len(parts) == 2 and parts[0] == "jobs":
            job = sched.get(parts[1])
            if job is None:
                return 404, {"error": "unknown job"}
            return 200, {"job": job.view(), "metrics": sched.metrics(job.id)}
        if len(parts) == 2 and parts[0] == "plan":
            model_id = parts[1]
            if base_for(model_id) is None:
                return 404, {"error": "no trainable original is known for this model",
                             "known": sorted(k for k, b in BASES.items() if b.repo)}
            return 200, {"plan": sched.plan_for(parent_id(model_id)).view()}
        if parts == ["datasets"]:
            target = sched.resolve_model(None)
            parent = parent_id(target) if target else ""
            kept, dropped, sources = sched.samples(parent) if parent else ([], {}, {})
            built = []
            if service.paths.built.is_dir():
                for summary in sorted(service.paths.built.glob("*/summary.json"), reverse=True)[:20]:
                    try:
                        import json

                        built.append(json.loads(summary.read_text(encoding="utf-8")))
                    except (OSError, ValueError):
                        continue
            return 200, {
                "folder": str(service.paths.datasets),
                "files": data_mod.list_files(service.paths.datasets),
                "model": parent, "usable": len(kept), "dropped": dropped, "sources": sources,
                "preview": [{"source": s.source, "prompt": s.messages[-1]["content"][:200],
                             "completion": s.completion[:200], "hash": s.hash[:12]} for s in kept[:20]],
                "manifest": sched.manifest.view(), "built": built,
            }
    except Exception as exc:  # noqa: BLE001 - a status read must not 500 the shell
        return 500, {"error": f"{type(exc).__name__}: {exc}"}
    return 404, {"error": "not found"}


def handle_post(server: Any, path: str, payload: dict) -> Reply:
    service = _service(server)
    if service is None:
        return 500, {"error": "the tuning channel failed to start"}
    sched = service.scheduler
    parts = [p for p in path.split("/") if p][2:]
    payload = dict(payload or {})
    if parts == ["settings"]:
        token = payload.pop("hf_token", None)
        values = payload.get("tuning", payload)
        try:
            clean = validate_settings(values if isinstance(values, dict) else {})
        except ValueError as exc:
            return 400, {"error": str(exc)}
        settings = getattr(server, "_settings", None)
        if settings is None:
            return 501, {"error": "settings are not configured"}
        if clean:
            settings.write("tuning", clean)
        if token is not None and token != MASK:
            settings.write("huggingface", {"hf_token": str(token).strip()})
        return 200, service.settings_view()
    if parts == ["jobs"]:
        overrides = {k: payload[k] for k in ("epochs", "max_steps", "min_samples", "quant", "max_hours",
                                             "grad_accum", "learning_rate") if k in payload}
        return sched.submit(str(payload.get("model_id") or "") or None, trigger="manual",
                            allow_slow=bool(payload.get("allow_slow")), confirm=bool(payload.get("confirm")),
                            overrides=overrides, resume=str(payload.get("resume") or ""))
    if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
        return sched.cancel(parts[1])
    if parts == ["feedback"]:
        turn_id = str(payload.get("turn_id") or "").strip()
        if not turn_id:
            return 400, {"error": "turn_id is required"}
        session_id = str(payload.get("session_id") or "").strip() or service.feedback.find_session(turn_id)
        if not session_id:
            return 404, {"error": "no transcript holds that turn"}
        try:
            item = service.feedback.record(session_id, turn_id, str(payload.get("rating") or ""),
                                           str(payload.get("correction") or ""))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        service._publish("tuning.feedback", {"turn_id": turn_id, "session_id": session_id,
                                             "rating": item.rating, "consented": item.consented})
        return 200, {"feedback": {"turn_id": item.turn_id, "session_id": item.session_id,
                                  "rating": item.rating, "consented": item.consented},
                     "counts": service.feedback.counts()}
    if parts == ["setup"]:
        return service.start_setup(payload)
    return 404, {"error": "not found"}


__all__ = ["attach", "detach", "handle_get", "handle_post", "is_post_route", "TuningService", "stack"]

"""The channel: a background job runner that turns consented usage into a tuned model.

    queued -> preparing -> training -> evaluating -> exporting -> done
                 |            |            |             |
                 +------------+------------+-------------+--> failed | rejected | cancelled

* preparing  : stack check, dataset build (data.py, rendered with the engine's
               chat template when the engine has this model loaded), the
               feasibility gate with the real token count, base-weight download
* training   : train.py in the tuning venv; paused while the user is chatting
               (any running turn), stopped gracefully at ``max_hours``
* evaluating : held-out loss vs the untuned base + the sanity prompt set;
               a regression ends in ``rejected`` (no export, data not consumed)
* exporting  : merged GGUF at ``tuning.quant`` into models/ + GGUF LoRA adapter
* done       : manifest records the samples, the model joins the catalogue,
               ``tuning.ready`` offers "Use it" (POST /api/models/select). The
               active model is never switched without that click.

Automation (``tuning.auto``): ``off`` | ``nightly`` (at ``nightly_hour``) |
``when-idle`` (no turn for ``idle_minutes``) | ``every-N-approved-samples``
(``min_samples`` new usable samples). Every mode also needs ``enabled``,
``min_samples`` usable samples, no running turn, and a feasibility verdict of
``ok`` (or ``slow`` with ``allow_8b_slow``). Automatic runs only download base
weights when ``auto_download`` is on; otherwise the first run must be started
by hand so the user sees the download size.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import data as data_mod
from . import feasibility, registry as registry_mod
from .base import complete as base_complete
from .catalogue import base_for, parent_id
from .feedback import FeedbackStore
from .paths import TuningPaths

TERMINAL = ("done", "failed", "rejected", "cancelled")
STATES = ("queued", "preparing", "training", "evaluating", "exporting") + TERMINAL
AUTO_MODES = ("off", "nightly", "when-idle", "every-N-approved-samples")
QUANTS = ("Q4_K_M", "Q5_K_M", "Q6_K", "Q8_0", "F16")

DEFAULTS = {
    "enabled": False,
    "auto": "off",
    "min_samples": 200,
    "max_hours": 4.0,
    "allow_8b_slow": False,
    "quant": "Q4_K_M",
    "model": "selected",
    "epochs": 2,
    "idle_minutes": 15,
    "nightly_hour": 2,
    "include_pii": False,
    "auto_download": False,
    "max_seq_len": 512,
}
# Overall progress bands per phase.
BANDS = {"queued": (0, 0), "preparing": (0, 10), "training": (10, 85), "evaluating": (85, 90),
         "exporting": (90, 100)}


def validate_settings(values: dict) -> dict:
    """Clean a partial settings update; raise ValueError on bad values."""
    out: dict = {}
    for key, value in dict(values or {}).items():
        if key not in DEFAULTS:
            continue
        if key in ("enabled", "allow_8b_slow", "include_pii", "auto_download"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            out[key] = value
        elif key == "auto":
            if value not in AUTO_MODES:
                raise ValueError(f"auto must be one of {', '.join(AUTO_MODES)}")
            out[key] = value
        elif key == "quant":
            if str(value).upper() not in QUANTS:
                raise ValueError(f"quant must be one of {', '.join(QUANTS)}")
            out[key] = str(value).upper()
        elif key == "model":
            out[key] = str(value or "selected")
        else:
            limits = {"min_samples": (1, 1_000_000, int), "max_hours": (0.05, 168.0, float),
                      "epochs": (1, 3, int), "idle_minutes": (1, 1440, int),
                      "nightly_hour": (0, 23, int), "max_seq_len": (128, 4096, int)}[key]
            low, high, kind = limits
            try:
                number = kind(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number") from None
            if isinstance(value, bool) or not low <= number <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            out[key] = number
    return out


@dataclass
class Job:
    id: str
    model_id: str                 # what the user asked for (may be a tuned id)
    parent: str                   # the catalogue base id
    trigger: str = "manual"
    state: str = "queued"
    progress: float = 0.0         # overall %, 0-100
    phase_progress: float = 0.0
    message: str = ""
    eta_s: float | None = None
    loss: float | None = None
    eval_loss: float | None = None
    base_eval_loss: float | None = None
    step: int = 0
    max_steps: int = 0
    paused: bool = False
    allow_slow: bool = False
    confirm_download: bool = False
    overrides: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    build: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    export: dict = field(default_factory=dict)
    new_model_id: str = ""
    error: str = ""
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel_requested: bool = False

    def view(self) -> dict:
        data = asdict(self)
        result = dict(data.get("result") or {})
        for key in ("sanity_base", "sanity_tuned"):
            if key in result:
                result[key] = {p: {"answer": v.get("answer"), "pass": v.get("pass")}
                               for p, v in (result[key] or {}).items()}
        data["result"] = result
        data["progress"] = round(self.progress, 1)
        return data


class Scheduler:
    def __init__(
        self,
        paths: TuningPaths,
        *,
        settings,
        feedback: FeedbackStore,
        transcripts,
        backend,
        publish: Callable[[str, dict], object] = lambda t, d: None,
        is_busy: Callable[[], bool] = lambda: False,
        last_activity: Callable[[], float] = lambda: 0.0,
        selected_model: Callable[[], str] = lambda: "",
        renderer_for: Callable[[str], object] = lambda model_id: None,
        catalogue: dict | None = None,
        clock: Callable[[], float] = time.time,
        tick_seconds: float = 60.0,
        hardware: Callable[[], feasibility.Hardware] | None = None,
    ) -> None:
        self.paths = paths.ensure()
        self.settings = settings
        self.feedback = feedback
        self.transcripts = transcripts
        self.backend = backend
        self.publish = publish
        self.is_busy = is_busy
        self.last_activity = last_activity
        self.selected_model = selected_model
        self.renderer_for = renderer_for
        self.catalogue = catalogue
        self.clock = clock
        self.tick_seconds = tick_seconds
        self.hardware = hardware or (lambda: feasibility.probe_hardware(self.paths.root))
        self.manifest = data_mod.Manifest(paths.manifest)
        self.registry = registry_mod.Registry(paths.registry)
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._worker: threading.Thread | None = None
        self._ticker: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_publish = 0.0
        self.last_auto: dict = {}
        self._load_jobs()

    # ------------------------------------------------------------ settings
    def config(self) -> dict:
        values = dict(DEFAULTS)
        try:
            stored = self.settings.read("tuning") if self.settings is not None else {}
        except Exception:  # noqa: BLE001 - a broken file means defaults
            stored = {}
        for key, value in (stored or {}).items():
            if key in DEFAULTS:
                values[key] = value
        return values

    def hf_token(self) -> str:
        try:
            token = str((self.settings.read("huggingface") or {}).get("hf_token") or "").strip()
        except Exception:  # noqa: BLE001
            token = ""
        return "" if token.startswith("•") else token

    # ------------------------------------------------------------ persistence
    def _job_path(self, job_id: str) -> Path:
        return self.paths.run_dir(job_id) / "job.json"

    def _save(self, job: Job) -> None:
        path = self._job_path(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(job), indent=2, default=str), encoding="utf-8")
        tmp.replace(path)

    def _load_jobs(self) -> None:
        if not self.paths.runs.is_dir():
            return
        for path in self.paths.runs.glob("*/job.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                job = Job(**{k: v for k, v in raw.items() if k in Job.__dataclass_fields__})
            except (OSError, ValueError, TypeError):
                continue
            if job.state not in TERMINAL:
                # The server stopped mid-run. Checkpoints are kept: starting
                # the job again with {"resume": id} continues from them.
                job.state = "failed"
                job.error = "interrupted: the server stopped during this run (resume it to continue)"
                job.finished_at = job.finished_at or self.clock()
                self._save(job)
            self._jobs[job.id] = job

    # ------------------------------------------------------------ views
    def jobs(self) -> list[dict]:
        with self._lock:
            items = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.view() for j in items]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs.values() if j.state not in TERMINAL), None)

    def metrics(self, job_id: str, limit: int = 2000) -> list[dict]:
        path = self.paths.run_dir(job_id) / "metrics.jsonl"
        if not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    # ------------------------------------------------------------ model resolution
    def resolve_model(self, requested: str | None) -> str:
        value = (requested or "").strip() or str(self.config().get("model") or "selected")
        if value == "selected":
            value = self.selected_model() or ""
        return value

    def samples(self, model_key: str) -> tuple[list, dict, dict]:
        cfg = self.config()
        return data_mod.collect(
            feedback=self.feedback, transcripts=self.transcripts,
            datasets_dir=self.paths.datasets, manifest=self.manifest,
            model_key=model_key, include_pii=bool(cfg.get("include_pii")),
        )

    def plan_for(self, model_id: str, *, tokens: int | None = None) -> feasibility.Plan:
        cfg = self.config()
        parent = parent_id(model_id)
        if tokens is None:
            kept, _dropped, _src = self.samples(parent)
            tokens = sum(data_mod.estimate_tokens(s.completion) + data_mod.estimate_tokens(
                " ".join(m.get("content", "") for m in s.messages)) for s in kept)
            # Before any data exists, estimate for min_samples typical turns.
            tokens = tokens or int(cfg["min_samples"]) * 300
        stack_view = self.backend.stack()
        from .stack import plan_caps

        return feasibility.plan(
            parent, train_tokens=tokens, epochs=int(cfg["epochs"]), max_hours=float(cfg["max_hours"]),
            hardware=self.hardware(), caps=plan_caps(stack_view),
            throughput=feasibility.measured_throughput(self.paths.hardware),
            base_downloaded=base_complete(self.paths.base_dir(parent)),
        )

    # ------------------------------------------------------------ submit
    def submit(self, model_id: str | None = None, *, trigger: str = "manual", allow_slow: bool = False,
               confirm: bool = False, overrides: dict | None = None, resume: str = "") -> tuple[int, dict]:
        """Start a job now. Returns (http status, payload)."""
        cfg = self.config()
        overrides = dict(overrides or {})
        with self._lock:
            running = self.active()
            if running is not None:
                return 409, {"error": "a tuning job is already running", "job": running.view()}
        stack_view = self.backend.stack()
        if not stack_view.get("installed"):
            return 503, {"error": "training_stack_missing", "message": stack_view.get("message"),
                         "stack": stack_view}
        previous = self.get(resume) if resume else None
        if resume and previous is None:
            return 404, {"error": "unknown job to resume"}
        target = previous.model_id if previous else self.resolve_model(model_id)
        if not target:
            return 400, {"error": "no model selected: pass model_id or select a model first"}
        base = base_for(target)
        if base is None:
            return 400, {"error": "this model cannot be tuned",
                         "message": "no original (trainable) weights are known for it; "
                                    "GGUF weights cannot be fine-tuned"}
        parent = base.catalogue_id
        kept, dropped, sources = self.samples(parent)
        minimum = int(overrides.get("min_samples") or cfg["min_samples"])
        if len(kept) < minimum and not previous:
            return 400, {"error": "not_enough_samples", "samples": len(kept), "min_samples": minimum,
                         "dropped": dropped, "sources": sources,
                         "message": f"{len(kept)} usable samples; {minimum} needed. Rate answers "
                                    "thumbs-up / teach corrections, or add files to the datasets folder."}
        tokens = sum(data_mod.estimate_tokens(s.completion) + data_mod.estimate_tokens(
            " ".join(m.get("content", "") for m in s.messages)) for s in kept)
        plan = self.plan_for(parent, tokens=tokens)
        view = plan.view()
        if plan.verdict == "impossible":
            return 400, {"error": "not_feasible", "message": plan.reason, "plan": view}
        if plan.verdict == "slow" and not allow_slow:
            return 409, {"error": "needs_allow_slow", "needs": ["allow_slow"], "plan": view,
                         "message": f"{plan.reason}. Estimated {plan.est_total_hours:.1f} h. "
                                    "Send allow_slow: true to run it anyway."}
        if plan.download_gb > 0 and not confirm:
            return 409, {"error": "needs_confirm_download", "needs": ["confirm"], "plan": view,
                         "message": f"the original weights ({plan.download_gb:.1f} GB) must be "
                                    f"downloaded from Hugging Face ({base.repo}). Send confirm: true."}
        now = self.clock()
        job_id = previous.id if previous else (time.strftime("r%Y%m%d%H%M", time.localtime(now))
                                               + uuid.uuid4().hex[:3])
        job = previous or Job(id=job_id, model_id=target, parent=parent, created_at=now)
        job.trigger, job.state, job.progress, job.error, job.message = trigger, "queued", 0.0, "", "queued"
        job.allow_slow, job.confirm_download, job.overrides, job.plan = allow_slow, confirm, overrides, view
        job.cancel_requested, job.finished_at, job.paused = False, 0.0, False
        with self._lock:
            self._jobs[job.id] = job
            self._save(job)
            self._worker = threading.Thread(target=self._run, args=(job,), name=f"tuning-{job.id}",
                                            daemon=True)
            self._worker.start()
        self._emit(job, force=True)
        return 202, {"job": job.view()}

    def cancel(self, job_id: str) -> tuple[int, dict]:
        job = self.get(job_id)
        if job is None:
            return 404, {"error": "unknown job"}
        if job.state in TERMINAL:
            return 409, {"error": f"job already {job.state}", "job": job.view()}
        job.cancel_requested = True
        self._control(job, cancel=True)
        job.message = "cancelling"
        self._emit(job, force=True)
        return 200, {"job": job.view()}

    def wait(self, timeout: float = 30.0) -> None:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    # ------------------------------------------------------------ the run
    def _set(self, job: Job, state: str | None = None, *, phase: float | None = None,
             message: str | None = None, force: bool = False) -> None:
        if state is not None and state != job.state:
            job.state = state
            force = True
        if phase is not None:
            job.phase_progress = max(0.0, min(100.0, phase))
        low, high = BANDS.get(job.state, (100, 100))
        if job.state in TERMINAL:
            job.progress = 100.0 if job.state == "done" else job.progress
        else:
            job.progress = low + (high - low) * job.phase_progress / 100.0
        if message is not None:
            job.message = message
        self._save(job)
        self._emit(job, force=force)

    def _emit(self, job: Job, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_publish < 1.0:
            return
        self._last_publish = now
        try:
            self.publish("tuning.job", job.view())
        except Exception:  # noqa: BLE001 - events are observability
            pass

    def _control(self, job: Job, **values) -> None:
        path = self.paths.run_dir(job.id) / "control.json"
        current = {}
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = {}
        current.update(values)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current), encoding="utf-8")

    def _check_cancel(self, job: Job) -> None:
        if job.cancel_requested:
            raise _Cancelled()

    def _run(self, job: Job) -> None:
        job.started_at = self.clock()
        run_dir = self.paths.run_dir(job.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._control(job, pause=False, stop=False, cancel=False)
        cfg = self.config()
        base = base_for(job.parent)
        try:
            # ---------------- preparing
            self._set(job, "preparing", phase=0, message="building the dataset")
            build_dir = run_dir / "data"
            if (build_dir / "train.jsonl").is_file() and job.build:
                build = job.build
            else:
                kept, dropped, sources = self.samples(job.parent)
                renderer = None
                try:
                    renderer = self.renderer_for(job.parent)
                except Exception:  # noqa: BLE001 - fall back to the HF template
                    renderer = None
                made = data_mod.build(kept, out_root=self.paths.built, model_key=job.parent,
                                      renderer=renderer, dropped=dropped, sources=sources)
                build_dir.mkdir(parents=True, exist_ok=True)
                for name in ("train.jsonl", "eval.jsonl", "summary.json"):
                    src = Path(made.directory) / name
                    (build_dir / name).write_bytes(src.read_bytes())
                (build_dir / "hashes.json").write_text(json.dumps(made.hashes), encoding="utf-8")
                build = made.view()
                job.build = build
            self._set(job, phase=20, message=f"{build.get('kept', 0)} samples "
                                             f"({build.get('train', 0)} train / {build.get('eval', 0)} eval)")
            self._check_cancel(job)

            plan = self.plan_for(job.parent, tokens=int(build.get("tokens") or 0) or None)
            job.plan = plan.view()
            if plan.verdict == "impossible":
                raise _Failed(f"not feasible on this machine: {plan.reason}")
            if plan.verdict == "slow" and not job.allow_slow:
                raise _Failed(f"needs allow_slow: {plan.reason}")

            base_dir = self.paths.base_dir(job.parent)
            if not base_complete(base_dir):
                if not job.confirm_download:
                    raise _Failed(f"the original weights ({plan.download_gb:.1f} GB from {base.repo}) are "
                                  "not downloaded; start a job by hand once and confirm the download")
                self._set(job, phase=25, message=f"downloading {base.repo} ({plan.download_gb:.1f} GB)")

                def dl_progress(message, pct, eta):
                    job.eta_s = eta
                    self._set(job, phase=25 + 0.75 * float(pct or 0), message=message)

                self.backend.ensure_base(base, base_dir, self.hf_token() or None, dl_progress,
                                         lambda: job.cancel_requested)
            self._check_cancel(job)

            # ---------------- training
            self._set(job, "training", phase=0, message=f"training ({plan.method})")
            max_hours = float(job.overrides.get("max_hours") or cfg["max_hours"])
            config = {
                "run_dir": str(run_dir), "base_dir": str(base_dir),
                "train_file": str(build_dir / "train.jsonl"), "eval_file": str(build_dir / "eval.jsonl"),
                "method": plan.method, "max_seq_len": int(cfg["max_seq_len"]),
                "epochs": int(job.overrides.get("epochs") or cfg["epochs"]),
                "max_steps": int(job.overrides.get("max_steps") or 0),
                "grad_accum": int(job.overrides.get("grad_accum") or 4),
                "learning_rate": float(job.overrides.get("learning_rate") or 2e-4),
                "lora": {"r": 16, "alpha": 32, "dropout": 0.05, "targets": list(base.targets)},
                "init_adapter": self.registry.latest_adapter(job.parent),
                "deadline": self.clock() + max_hours * 3600,
                "resume": True,
            }

            def on_event(event: dict) -> None:
                kind = event.get("event")
                if kind == "progress":
                    job.step = int(event.get("step") or 0)
                    job.max_steps = int(event.get("max_steps") or 0)
                    job.loss = event.get("loss") if event.get("loss") is not None else job.loss
                    job.eta_s = event.get("eta_s")
                    pct = 100.0 * job.step / job.max_steps if job.max_steps else 0.0
                    self._set(job, phase=pct, message=f"step {job.step}/{job.max_steps}")
                elif kind == "eval":
                    if event.get("base"):
                        job.base_eval_loss = event.get("eval_loss")
                    else:
                        job.eval_loss = event.get("eval_loss")
                    self._set(job, force=True)
                elif kind == "phase":
                    if event.get("phase") == "evaluating":
                        self._set(job, "evaluating", phase=0, message=event.get("message", ""))
                    else:
                        self._set(job, message=event.get("message", ""))
                elif kind == "paused":
                    job.paused = True
                    self._set(job, message="paused while you chat", force=True)
                elif kind == "resumed":
                    job.paused = False
                    self._set(job, message="resumed", force=True)

            def tick() -> str:
                if job.cancel_requested:
                    self._control(job, cancel=True)
                    return "cancel"
                busy = False
                try:
                    busy = bool(self.is_busy())
                except Exception:  # noqa: BLE001
                    busy = False
                self._control(job, pause=busy)
                return ""

            result = self.backend.train(config, run_dir, on_event, tick)
            if result.get("status") == "cancelled" or job.cancel_requested:
                raise _Cancelled()
            job.result = result
            job.eval_loss = result.get("eval_loss", job.eval_loss)
            job.base_eval_loss = result.get("base_eval_loss", job.base_eval_loss)
            if result.get("tok_s"):
                feasibility.record_throughput(self.paths.hardware, plan.method, float(result["tok_s"]),
                                              base.params_b)
            self._set(job, "evaluating", phase=100, message=result.get("reason", ""))
            if not result.get("accepted"):
                job.error = result.get("reason") or "rejected by evaluation"
                job.finished_at = self.clock()
                self._set(job, "rejected", message=job.error, force=True)
                return

            # ---------------- exporting
            self._check_cancel(job)
            quant = str(job.overrides.get("quant") or cfg["quant"]).upper()
            self._set(job, "exporting", phase=0, message=f"exporting GGUF ({quant})")
            parent_info = (self.catalogue or _catalogue()).get(job.parent)
            parent_filename = getattr(parent_info, "filename", f"{job.parent}.gguf")
            exported = self.backend.export(
                run_dir, base_dir, Path(result.get("adapter_dir") or run_dir / "adapter"),
                parent_filename, job.id, quant,
                lambda message, pct: self._set(job, phase=pct, message=message),
            )
            job.export = exported
            new_id = f"{job.parent}-tuned-{job.id}"
            entry = self.registry.add({
                "id": new_id, "parent": job.parent, "run_id": job.id, "quant": quant,
                "path": exported.get("gguf", ""), "adapter_gguf": exported.get("adapter_gguf", ""),
                "adapter_dir": result.get("adapter_dir", ""),
                "name": f"{getattr(parent_info, 'name', job.parent)} - tuned {job.id}",
                "eval": _eval_summary(result),
            })
            registry_mod.register(entry, self.catalogue)
            hashes_path = build_dir / "hashes.json"
            hashes = json.loads(hashes_path.read_text(encoding="utf-8")) if hashes_path.is_file() else []
            self.manifest.mark(job.parent, hashes, job.id)
            job.new_model_id = new_id
            job.finished_at = self.clock()
            self._set(job, "done", phase=100, message="a tuned model is ready", force=True)
            try:
                self.publish("tuning.ready", {
                    "job_id": job.id, "model_id": new_id, "parent": job.parent,
                    "name": entry["name"], "path": entry["path"], "quant": quant,
                    "size_bytes": _size_of(entry.get("path")),
                    "eval": entry["eval"],
                    "action": {"label": "Use it", "method": "POST", "path": "/api/models/select",
                               "body": {"id": new_id}},
                })
            except Exception:  # noqa: BLE001
                pass
        except _Cancelled:
            job.finished_at = self.clock()
            job.error = ""
            self._set(job, "cancelled", message="cancelled", force=True)
        except _Failed as exc:
            job.error = str(exc)
            job.finished_at = self.clock()
            self._set(job, "failed", message=job.error, force=True)
        except Exception as exc:  # noqa: BLE001 - a job failure is a state, never a crash
            job.error = f"{type(exc).__name__}: {exc}"[:2000]
            job.finished_at = self.clock()
            self._set(job, "failed", message=job.error, force=True)

    # ------------------------------------------------------------ automation
    def start(self) -> "Scheduler":
        if self._ticker is None:
            self._ticker = threading.Thread(target=self._tick_loop, name="tuning-ticker", daemon=True)
            self._ticker.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        job = self.active()
        if job is not None:
            # Let the trainer checkpoint and stop; a later resume continues it.
            self._control(job, stop=True)

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            try:
                self.auto_tick()
            except Exception as exc:  # noqa: BLE001 - the ticker must survive
                self.last_auto = {"at": self.clock(), "error": f"{type(exc).__name__}: {exc}"}

    def auto_due(self, cfg: dict | None = None) -> tuple[bool, str]:
        """Whether the automatic policy wants a run now (without sample counting)."""
        cfg = cfg or self.config()
        if not cfg.get("enabled"):
            return False, "tuning is disabled"
        mode = cfg.get("auto", "off")
        if mode == "off":
            return False, "automatic runs are off"
        if self.active() is not None:
            return False, "a job is running"
        if self.is_busy():
            return False, "the user is chatting"
        now = self.clock()
        idle_for = now - (self.last_activity() or 0.0)
        if mode == "when-idle" and idle_for < int(cfg["idle_minutes"]) * 60:
            return False, f"idle for {idle_for / 60:.0f} of {cfg['idle_minutes']} minutes"
        if mode == "nightly":
            local = time.localtime(now)
            if local.tm_hour != int(cfg["nightly_hour"]):
                return False, f"waiting for {int(cfg['nightly_hour']):02d}:00"
            today = time.strftime("%Y-%m-%d", local)
            if self.last_auto.get("day") == today and self.last_auto.get("started"):
                return False, "already ran tonight"
        if mode == "every-N-approved-samples" and idle_for < 120:
            return False, "waiting for two idle minutes"
        return True, mode

    def auto_tick(self) -> dict:
        cfg = self.config()
        due, why = self.auto_due(cfg)
        record = {"at": self.clock(), "due": due, "why": why,
                  "day": time.strftime("%Y-%m-%d", time.localtime(self.clock()))}
        if due:
            status, payload = self.submit(None, trigger=cfg["auto"], allow_slow=bool(cfg["allow_8b_slow"]),
                                          confirm=bool(cfg["auto_download"]))
            record.update(status=status, started=status == 202,
                          result=payload.get("error") or payload.get("job", {}).get("id"))
        self.last_auto = record
        return record


class _Cancelled(Exception):
    pass


class _Failed(Exception):
    pass


def _catalogue() -> dict:
    try:
        from services.model_manager import RECOMMENDED_MODELS

        return RECOMMENDED_MODELS
    except Exception:  # noqa: BLE001
        return {}


def _eval_summary(result: dict) -> dict:
    tuned = result.get("sanity_tuned") or {}
    return {
        "base_eval_loss": result.get("base_eval_loss"),
        "eval_loss": result.get("eval_loss"),
        "sanity_passed": sum(1 for v in tuned.values() if v.get("pass")),
        "sanity_total": len(tuned),
        "samples": result.get("samples"),
        "steps": result.get("steps"),
        "train_seconds": result.get("train_seconds"),
        "reason": result.get("reason"),
    }


def _size_of(path) -> int:
    """File size for the "Use it" card; 0 when unknown (frontend request 5)."""
    try:
        import os

        return int(os.path.getsize(str(path))) if path else 0
    except OSError:
        return 0

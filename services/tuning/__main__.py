"""Command line for the tuning channel (works without the server running).

    python -m services.tuning status
    python -m services.tuning plan  [--model ID] [--tokens N]
    python -m services.tuning build [--model ID]           # dataset dry run, nothing trains
    python -m services.tuning run   [--model ID] [--allow-slow] [--confirm] [--max-steps N]
                                    [--min-samples N] [--quant Q4_K_M]
    python -m services.tuning setup ...                    # = python -m services.tuning.setup

``run`` shows the plan (method, memory, time estimate, download size) first
and refuses a ``slow`` plan without ``--allow-slow`` and a base-weight
download without ``--confirm``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _scheduler(home: Path, model: str | None):
    from services.server.settings import SettingsStore
    from services.sessions.transcript import TranscriptStore

    from .backend import SubprocessBackend
    from .feedback import FeedbackStore
    from .paths import TuningPaths
    from .scheduler import Scheduler

    paths = TuningPaths.at(home / "tuning")

    def selected() -> str:
        try:
            from services.config import load_last_model

            return str(load_last_model(home) or "")
        except Exception:  # noqa: BLE001
            return ""

    def show(kind, payload):
        if kind == "tuning.job":
            eta = payload.get("eta_s")
            print(f"[{payload['progress']:5.1f}%] {payload['state']:10s} {payload.get('message', '')}"
                  + (f"  loss={payload['loss']:.4f}" if payload.get("loss") is not None else "")
                  + (f"  eta={eta / 60:.1f} min" if eta else ""), flush=True)
        elif kind == "tuning.ready":
            print(f"READY: {payload['model_id']} -> {payload['path']}", flush=True)

    return Scheduler(paths, settings=SettingsStore(home / "settings"),
                     feedback=FeedbackStore(home / "sessions"),
                     transcripts=TranscriptStore(home / "sessions"),
                     backend=SubprocessBackend(paths), publish=show,
                     selected_model=selected)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["setup"]:
        from .setup import main as setup_main

        return setup_main(argv[1:])
    parser = argparse.ArgumentParser(prog="python -m services.tuning")
    parser.add_argument("command", choices=("status", "plan", "build", "run"))
    parser.add_argument("--home", default=str(Path.home() / ".imurun"))
    parser.add_argument("--model")
    parser.add_argument("--tokens", type=int)
    parser.add_argument("--allow-slow", action="store_true")
    parser.add_argument("--confirm", action="store_true", help="confirm the base-weight download")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--min-samples", type=int)
    parser.add_argument("--epochs", type=int)
    parser.add_argument("--quant")
    args = parser.parse_args(argv)
    home = Path(args.home)
    sched = _scheduler(home, args.model)
    model = sched.resolve_model(args.model)
    from .catalogue import parent_id

    if args.command == "status":
        print(json.dumps({"stack": sched.backend.stack(), "model": model,
                          "feedback": sched.feedback.counts(), "jobs": sched.jobs()[:5],
                          "tuned": sched.registry.entries()}, indent=2, default=str))
        return 0
    if not model:
        print("no model: pass --model <catalogue id>", file=sys.stderr)
        return 2
    if args.command == "plan":
        print(json.dumps(sched.plan_for(parent_id(model), tokens=args.tokens).view(), indent=2))
        return 0
    if args.command == "build":
        kept, dropped, sources = sched.samples(parent_id(model))
        print(json.dumps({"model": parent_id(model), "usable": len(kept), "dropped": dropped,
                          "sources": sources}, indent=2))
        return 0
    overrides = {k: v for k, v in (("max_steps", args.max_steps), ("min_samples", args.min_samples),
                                   ("epochs", args.epochs), ("quant", args.quant)) if v}
    status, payload = sched.submit(model, allow_slow=args.allow_slow, confirm=args.confirm,
                                   overrides=overrides)
    if status != 202:
        print(json.dumps(payload, indent=2, default=str))
        return 1
    plan = payload["job"]["plan"]
    print(f"plan: {plan['method']} ({plan['need_gb']} of {plan['have_gb']} GB), "
          f"~{plan['est_total_hours']:.2f} h, download {plan['download_gb']} GB", flush=True)
    while True:
        job = sched.get(payload["job"]["id"])
        if job is None or job.state in ("done", "failed", "rejected", "cancelled"):
            break
        time.sleep(2)
    print(json.dumps(job.view() if job else {}, indent=2, default=str))
    return 0 if job and job.state == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())

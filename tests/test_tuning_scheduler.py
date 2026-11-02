"""The channel's state machine with a fake trainer (no real training)."""
from __future__ import annotations

import json
import time

import pytest

from services import model_manager as mm
from services.server.settings import SettingsStore
from services.tuning.paths import TuningPaths
from services.tuning.scheduler import Scheduler, validate_settings
from tests.tuning_fakes import FakeBackend, fixture_sessions, laptop


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "FISH_COMPATIBLE_IDS", set(mm.FISH_COMPATIBLE_IDS))
    catalogue = dict(mm.RECOMMENDED_MODELS)
    store, fb = fixture_sessions(tmp_path)
    settings = SettingsStore(tmp_path / "settings")
    settings.write("tuning", {"min_samples": 1, "model": "llama-3.2-1b-q4"})
    events: list[tuple[str, dict]] = []
    state = {"busy": False}
    backend = FakeBackend(tmp_path / "models")
    sched = Scheduler(
        TuningPaths.at(tmp_path / "tuning"), settings=settings, feedback=fb, transcripts=store,
        backend=backend, publish=lambda kind, data: events.append((kind, data)),
        is_busy=lambda: state["busy"], catalogue=catalogue, hardware=laptop,
    )
    return sched, backend, events, state, catalogue, settings, tmp_path


def run(sched, **kw):
    status, payload = sched.submit(**kw)
    assert status == 202, payload
    sched.wait(10)
    return sched.get(payload["job"]["id"])


def test_full_run_reaches_done_and_offers_use_it(env):
    sched, backend, events, _state, catalogue, _settings, tmp = env
    job = run(sched, confirm=True)
    assert job.state == "done", job.error
    assert backend.calls == ["ensure_base", "train", "export"]
    states = [d["state"] for k, d in events if k == "tuning.job"]
    for expected in ("queued", "preparing", "training", "evaluating", "exporting", "done"):
        assert expected in states
    # States only move forward.
    order = ["queued", "preparing", "training", "evaluating", "exporting", "done"]
    seen = [order.index(s) for s in states]
    assert seen == sorted(seen)
    ready = [d for k, d in events if k == "tuning.ready"]
    assert len(ready) == 1
    new_id = ready[0]["model_id"]
    assert new_id == f"llama-3.2-1b-q4-tuned-{job.id}" == job.new_model_id
    assert ready[0]["action"] == {"label": "Use it", "method": "POST", "path": "/api/models/select",
                                  "body": {"id": new_id}}
    # Registered as a local catalogue model that /api/models/select accepts.
    info = catalogue[new_id]
    assert info.source == "local" and info.filename.endswith(f"-tuned-{job.id}-Q4_K_M.gguf")
    assert (tmp / "models" / info.filename).is_file() and new_id in mm.FISH_COMPATIBLE_IDS
    # The samples are recorded so they never train again.
    assert sched.manifest.view()["models"] == {"llama-3.2-1b-q4": 3}
    config = backend.configs[0]
    assert config["method"] == "qlora-gpu" and config["lora"]["r"] == 16
    assert config["lora"]["alpha"] == 32 and config["lora"]["dropout"] == 0.05
    rows = (tmp / "tuning" / "runs" / job.id / "data" / "train.jsonl").read_text().splitlines()
    assert rows and all("completion" in json.loads(r) for r in rows)
    assert job.progress == 100.0 and job.base_eval_loss == 2.0 and job.eval_loss == 1.5
    # A second run has nothing new to learn.
    status, payload = sched.submit(confirm=True)
    assert status == 400 and payload["error"] == "not_enough_samples"


def test_second_run_continues_from_the_accepted_adapter(env):
    sched, backend, *_rest, tmp = env
    first = run(sched, confirm=True)
    (tmp / "tuning" / "datasets" / "more.jsonl").write_text(json.dumps(
        {"prompt": "Explain recursion", "completion": "Recursion is when a function calls itself on a "
                                                      "smaller input until it reaches a base case."}))
    run(sched)
    assert backend.configs[1]["init_adapter"].endswith(f"{first.id}\\adapter") or \
        backend.configs[1]["init_adapter"].endswith(f"{first.id}/adapter")


def test_rejected_run_does_not_export_or_consume_data(env):
    sched, backend, events, *_ = env
    backend.accepted = False
    job = run(sched, confirm=True)
    assert job.state == "rejected" and "sanity" in job.error
    assert "export" not in backend.calls
    assert sched.manifest.view()["models"] == {}
    assert not [e for e in events if e[0] == "tuning.ready"]


def test_training_pauses_while_the_user_chats(env):
    sched, backend, events, state, *_ = env
    backend.on_step = lambda step: state.update(busy=step == 2)
    job = run(sched, confirm=True)
    control = json.loads((sched.paths.run_dir(job.id) / "control.json").read_text())
    assert job.state == "done" and control["pause"] is False
    assert backend.ticks.count("") == backend.steps


def test_pause_flag_is_written_when_busy(env):
    sched, backend, _events, state, *_ = env
    seen = []

    def step(n):
        state["busy"] = n == 2
        if n == 3:
            seen.append(json.loads((sched.paths.runs / next(iter(sched._jobs)) / "control.json").read_text()))
    backend.on_step = step
    run(sched, confirm=True)
    assert seen and seen[0]["pause"] is True


def test_cancel_mid_training(env):
    sched, backend, *_ = env
    holder = {}
    backend.on_step = lambda n: n == 2 and sched.cancel(holder["id"])
    status, payload = sched.submit(confirm=True)
    holder["id"] = payload["job"]["id"]
    sched.wait(10)
    job = sched.get(holder["id"])
    assert job.state == "cancelled" and "export" not in backend.calls
    assert sched.cancel(job.id)[0] == 409


def test_gates_before_a_job_starts(env):
    sched, backend, _events, _state, _cat, settings, _tmp = env
    status, payload = sched.submit()
    assert status == 409 and payload["needs"] == ["confirm"] and payload["plan"]["download_gb"] > 0
    settings.write("tuning", {"max_hours": 0.05})        # 3B export alone takes longer
    status, payload = sched.submit(model_id="llama-3.2-3b-q4", confirm=True)
    assert status == 409 and payload["needs"] == ["allow_slow"] and payload["plan"]["est_total_hours"] > 0.05
    settings.write("tuning", {"max_hours": 4.0})
    # 8B with 3 short samples: CPU 4-bit, under a week -> slow, estimate shown.
    status, payload = sched.submit(model_id="deepseek-r1-distill-llama-8b-q4", confirm=True)
    assert status == 409 and payload["needs"] == ["allow_slow"] and payload["plan"]["est_total_hours"] > 1
    status, payload = sched.submit(model_id="llama-3.1-70b-q4", confirm=True)
    assert status == 400 and payload["error"] == "not_feasible"
    status, payload = sched.submit(model_id="llama-3.2-8b-q4", confirm=True)
    assert status == 400 and "cannot be tuned" in payload["error"]
    settings.write("tuning", {"min_samples": 50})
    status, payload = sched.submit(confirm=True)
    assert status == 400 and payload["samples"] == 3 and payload["min_samples"] == 50
    backend.installed = False
    status, payload = sched.submit(confirm=True)
    assert status == 503 and payload["error"] == "training_stack_missing"


def test_only_one_job_at_a_time(env):
    sched, backend, *_ = env
    gate = {"go": False}

    def hold(step):
        while not gate["go"]:
            time.sleep(0.01)
    backend.on_step = hold
    status, first = sched.submit(confirm=True)
    assert status == 202
    assert sched.submit(confirm=True)[0] == 409
    gate["go"] = True
    sched.wait(10)


def test_interrupted_job_is_reported_and_resumable(env):
    sched, backend, *_rest, tmp = env
    job = run(sched, confirm=True)
    path = sched.paths.run_dir(job.id) / "job.json"
    raw = json.loads(path.read_text())
    raw["state"] = "training"
    path.write_text(json.dumps(raw))
    again = Scheduler(sched.paths, settings=sched.settings, feedback=sched.feedback,
                      transcripts=sched.transcripts, backend=backend, catalogue=sched.catalogue,
                      hardware=laptop)
    loaded = again.get(job.id)
    assert loaded.state == "failed" and "interrupted" in loaded.error
    status, payload = again.submit(resume=job.id)
    assert status == 202 and payload["job"]["id"] == job.id
    again.wait(10)
    assert again.get(job.id).state == "done"


def test_automation_policy(env, monkeypatch):
    sched, _backend, _events, state, _cat, settings, _tmp = env
    assert sched.auto_due()[0] is False                              # disabled by default
    settings.write("tuning", {"enabled": True, "auto": "when-idle", "idle_minutes": 15})
    sched.last_activity = lambda: time.time() - 60
    due, why = sched.auto_due()
    assert not due and "idle" in why
    sched.last_activity = lambda: time.time() - 3600
    assert sched.auto_due()[0] is True
    state["busy"] = True
    assert sched.auto_due() == (False, "the user is chatting")
    state["busy"] = False
    settings.write("tuning", {"auto": "nightly", "nightly_hour": (time.localtime().tm_hour + 1) % 24})
    assert sched.auto_due()[0] is False
    settings.write("tuning", {"auto": "every-N-approved-samples", "auto_download": True})
    record = sched.auto_tick()
    assert record["started"] and record["status"] == 202
    sched.wait(10)
    assert sched.jobs()[0]["trigger"] == "every-N-approved-samples"
    assert sched.jobs()[0]["state"] == "done"


def test_auto_run_without_auto_download_fails_honestly(env):
    sched, _backend, _events, _state, _cat, settings, _tmp = env
    settings.write("tuning", {"enabled": True, "auto": "every-N-approved-samples"})
    sched.last_activity = lambda: 0.0
    record = sched.auto_tick()
    assert record["status"] == 409 and record["result"] == "needs_confirm_download"


def test_validate_settings():
    assert validate_settings({"enabled": True, "auto": "nightly", "quant": "q8_0", "min_samples": "50",
                              "unknown": 1}) == {"enabled": True, "auto": "nightly", "quant": "Q8_0",
                                                 "min_samples": 50}
    for bad in ({"auto": "hourly"}, {"quant": "Q2"}, {"epochs": 9}, {"enabled": "yes"},
                {"max_hours": -1}, {"nightly_hour": 24}):
        with pytest.raises(ValueError):
            validate_settings(bad)

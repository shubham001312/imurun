"""/api/tuning/* on a real in-process ApiServer with a fake model and a fake trainer."""
from __future__ import annotations

import time

import pytest

from services import model_manager as mm
from services.persona.store import PersonaStore
from services.server.api import ApiServer
from services.skills.store import SkillsStore
from services.tools import ToolRunner, default_registry
from services.tuning import routes as tuning_routes
from tests.test_agentic_api import FakeModel, req
from tests.tuning_fakes import roomy_disk, LONG, FakeBackend, laptop


@pytest.fixture()
def api(tmp_path, monkeypatch):
    roomy_disk(monkeypatch)  # the disk guard must not read this machine's /tmp
    monkeypatch.setattr(mm, "FISH_COMPATIBLE_IDS", set(mm.FISH_COMPATIBLE_IDS))
    monkeypatch.setattr(mm, "RECOMMENDED_MODELS", dict(mm.RECOMMENDED_MODELS))
    monkeypatch.setattr(tuning_routes.feasibility, "probe_hardware", lambda *a, **k: laptop())
    model = FakeModel()
    server = ApiServer(
        persona_store=PersonaStore(tmp_path / "persona.json"),
        sessions_root=tmp_path / "sessions", data_root=tmp_path, model=model,
        skills=SkillsStore(tmp_path / "skills"), tools=ToolRunner(default_registry()), static_root=None,
    )
    backend = FakeBackend(tmp_path / "models")
    service = tuning_routes.TuningService(server, backend=backend)
    service.scheduler.hardware = laptop
    server._tuning = service
    server.start()
    yield server, model, backend, tmp_path
    server.stop()


def chat(server, model, text, answer):
    model.replies = [answer]
    _, session = req(server, "POST", "/api/sessions", {"first_command": text})
    _, turn = req(server, "POST", "/api/turns", {"session_id": session["id"], "text": text})
    for _ in range(100):
        _, view = req(server, "GET", f"/api/turns/{turn['turn_id']}")
        if view.get("state") != "running":
            break
        time.sleep(0.02)
    return session["id"], turn["turn_id"]


def test_auth_and_status(api):
    server, *_ = api
    assert req(server, "GET", "/api/tuning/status", token=False)[0] == 401
    status, body = req(server, "GET", "/api/tuning/status")
    assert status == 200 and body["enabled"] is False and body["auto"] == "off"
    assert body["stack"]["installed"] is True and body["hardware"]["vram_total_gb"] == 4.0
    assert body["feedback"] == {"up": 0, "down": 0, "teach": 0, "consented": 0}
    assert req(server, "GET", "/api/tuning/nope")[0] == 404


def test_settings_round_trip_and_token_is_write_only(api):
    server, *_ = api
    status, body = req(server, "GET", "/api/tuning/settings")
    assert status == 200 and body["tuning"]["min_samples"] == 200 and body["hf_token"] == ""
    assert req(server, "POST", "/api/tuning/settings", {"auto": "hourly"})[0] == 400
    status, body = req(server, "POST", "/api/tuning/settings",
                       {"enabled": True, "auto": "when-idle", "hf_token": "hf_secretvalue123"})
    assert status == 200 and body["tuning"]["auto"] == "when-idle" and body["hf_token"] == "••••••••"
    assert "hf_secretvalue123" not in str(body)
    # Echoing the mask back never overwrites the token, through either route.
    req(server, "POST", "/api/tuning/settings", {"hf_token": "••••••••"})
    req(server, "PUT", "/api/settings/huggingface", {"hf_token": "••••••••"})
    assert server._tuning.scheduler.hf_token() == "hf_secretvalue123"
    _, generic = req(server, "GET", "/api/settings/huggingface")
    assert generic["values"]["hf_token"] == "••••••••"


def test_feedback_is_the_consent_channel(api):
    server, model, *_ = api
    session_id, turn_id = chat(server, model, "Explain photosynthesis briefly.", LONG)
    assert req(server, "POST", "/api/tuning/feedback", {"rating": "up"})[0] == 400
    assert req(server, "POST", "/api/tuning/feedback", {"turn_id": "nope", "rating": "up"})[0] == 404
    assert req(server, "POST", "/api/tuning/feedback", {"turn_id": turn_id, "rating": "meh"})[0] == 400
    status, body = req(server, "POST", "/api/tuning/feedback", {"turn_id": turn_id, "rating": "up"})
    assert status == 200 and body["feedback"]["session_id"] == session_id and body["feedback"]["consented"]
    _, datasets = req(server, "GET", "/api/tuning/datasets")
    # The selected model is unknown to the fake engine, so pick one explicitly.
    req(server, "POST", "/api/tuning/settings", {"model": "llama-3.2-1b-q4"})
    _, datasets = req(server, "GET", "/api/tuning/datasets")
    assert datasets["usable"] == 1 and datasets["preview"][0]["source"] == "feedback:up"
    status, body = req(server, "POST", "/api/tuning/feedback",
                       {"turn_id": turn_id, "rating": "down", "correction": "Plants make sugar from light."})
    assert status == 200 and body["feedback"]["rating"] == "teach"
    # Stored next to the transcript, not inside it.
    assert (server._tuning.feedback.root / f"{session_id}.jsonl").is_file()
    # Read back per session (frontend request 4): latest mark per turn.
    assert req(server, "GET", "/api/tuning/feedback")[0] == 400
    status, back = req(server, "GET", f"/api/tuning/feedback?session_id={session_id}")
    assert status == 200 and back["turns"][turn_id]["rating"] == "teach"
    assert back["turns"][turn_id]["consented"] and back["turns"][turn_id]["correction"].startswith("Plants")
    assert req(server, "GET", "/api/tuning/feedback?session_id=nope")[1]["turns"] == {}


def test_jobs_lifecycle_through_the_api(api):
    server, model, backend, tmp = api
    req(server, "POST", "/api/tuning/settings", {"model": "llama-3.2-1b-q4", "min_samples": 1})
    _, turn_id = chat(server, model, "Explain photosynthesis briefly.", LONG)
    req(server, "POST", "/api/tuning/feedback", {"turn_id": turn_id, "rating": "up"})
    status, body = req(server, "POST", "/api/tuning/jobs", {})
    assert status == 409 and body["needs"] == ["confirm"]
    status, body = req(server, "POST", "/api/tuning/jobs", {"confirm": True})
    assert status == 202
    job_id = body["job"]["id"]
    for _ in range(200):
        _, view = req(server, "GET", f"/api/tuning/jobs/{job_id}")
        if view["job"]["state"] in ("done", "failed", "rejected", "cancelled"):
            break
        time.sleep(0.02)
    assert view["job"]["state"] == "done", view["job"]["error"]
    assert isinstance(view["metrics"], list)
    _, listing = req(server, "GET", "/api/tuning/jobs")
    assert listing["jobs"][0]["id"] == job_id
    new_id = view["job"]["new_model_id"]
    # The tuned model shows up in the existing models API as a local model.
    status, models = req(server, "GET", "/api/models")
    assert status == 200
    assert new_id in str(models)
    assert req(server, "GET", "/api/tuning/jobs/unknown")[0] == 404
    assert req(server, "POST", f"/api/tuning/jobs/{job_id}/cancel", {})[0] == 409
    _, status_view = req(server, "GET", "/api/tuning/status")
    assert status_view["tuned_models"][0]["id"] == new_id


def test_stack_missing_is_reported_honestly(api):
    server, _model, backend, _ = api
    backend.installed = False
    req(server, "POST", "/api/tuning/settings", {"model": "llama-3.2-1b-q4"})
    status, body = req(server, "POST", "/api/tuning/jobs", {"confirm": True})
    assert status == 503 and body["error"] == "training_stack_missing"


def test_plan_route(api):
    server, *_ = api
    status, body = req(server, "GET", "/api/tuning/plan/deepseek-r1-distill-llama-8b-q4")
    assert status == 200 and body["plan"]["verdict"] == "impossible"
    assert body["plan"]["method"] == "qlora-cpu" and "not offered" in body["plan"]["reason"]
    assert req(server, "GET", "/api/tuning/plan/llama-3.2-8b-q4")[0] == 404


def test_setup_runs_in_the_background(api, monkeypatch):
    server, *_ = api
    calls = []

    def fake_setup(paths, *, progress, torch_index=None, skip_bnb=False):
        progress("torch", 10, "installing")
        calls.append(paths.root)
        return {"ok": True, "venv_bytes": 123}

    monkeypatch.setattr(tuning_routes.setup_mod, "run_setup", fake_setup)
    status, body = req(server, "POST", "/api/tuning/setup", {})
    assert status == 202 and body["setup"]["running"] is True
    for _ in range(100):
        if not server._tuning.setup_state.get("running"):
            break
        time.sleep(0.02)
    assert server._tuning.setup_state["ok"] is True and calls

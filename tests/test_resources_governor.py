"""Governor: sharing between chat/voice/tuning, dynamic lowering, disk
refusals, engine knobs, settings API and the `resources` event."""
from __future__ import annotations

import asyncio
import http.client
import json
from types import SimpleNamespace

import pytest

from services import model_manager as mm
from services.resources import ResourceGovernor, ThreadLedger, set_governor
from services.resources.budget import GB, Machine
from services.server.settings import SettingsStore


def machine(**kw) -> Machine:
    base = dict(physical_cores=8, logical_cores=16, ram_total=int(15.8 * GB), ram_available=int(9 * GB),
                vram_total=4 * GB, vram_free=int(3.5 * GB), gpu_name="test gpu",
                disk_total=400 * GB, disk_free=270 * GB)
    base.update(kw)
    return Machine(**base)


def governor(tmp_path, **kw) -> ResourceGovernor:
    settings = SettingsStore(tmp_path / "settings")
    m = machine(**kw)
    return ResourceGovernor(settings, data_root=tmp_path, measure_fn=lambda: m)


# ------------------------------------------------------------ sharing

def test_chat_first_voice_and_tuning_get_the_leftover():
    ledger = ThreadLedger(6)
    assert ledger.grant("voice", 2) == 2                  # idle machine: voice gets what it asks
    ledger.release("voice")
    assert ledger.grant("tuning", 5) == 5
    assert ledger.grant("voice", 2) == 1                  # 6 - 5 tuning = 1 left
    ledger.release("voice")
    ledger.release("tuning")
    assert ledger.grant("chat", 6) == 6                   # chat always gets its target
    assert ledger.grant("voice", 2) == 1                  # leftover is 0 -> voice keeps 1 (below normal)
    assert ledger.grant("tuning", 5) == 0                 # tuning gets nothing while chat runs
    ledger.release("chat")
    assert ledger.grant("tuning", 5) == 5                 # 6 - 1 voice
    ledger.release("chat")


def test_chat_never_takes_more_than_the_budget_and_voice_never_more_than_leftover():
    ledger = ThreadLedger(4)
    assert ledger.grant("chat", 99) == 4
    ledger.release("chat")
    assert ledger.grant("chat", 2) == 2
    assert ledger.grant("voice", 8) == 2                  # leftover only
    with ledger.hold("tuning", 3) as n:
        assert n == 0                                     # chat 2 + voice 2 = all
    assert ledger.view()["held"] == {"chat": 2, "voice": 2}


def test_governor_grants_follow_the_engine_generation(tmp_path):
    gov = governor(tmp_path)
    assert gov.target_threads == 6
    assert gov.voice_threads(2) == 2
    gov.release_voice()
    assert gov.before_generate() == 6
    assert gov.chat_active()
    assert gov.voice_threads(2) == 1
    assert gov.tuning_threads() == 0
    gov.after_generate()
    gov.release_voice()
    assert not gov.chat_active()
    assert gov.tuning_threads() == 5                      # all but one, so voice still fits
    gov.release_tuning()


def test_voice_worker_uses_the_grant(tmp_path):
    from services.voice.worker import WorkerClient

    worker = WorkerClient(cwd=tmp_path, threads=4, idle_seconds=0)
    try:
        sent = {}
        worker._spawn = lambda: None
        worker._proc = SimpleNamespace(poll=lambda: None, pid=1)

        def exchange(header, payload, timeout):
            sent.update(header)
            return {"ok": True}, b""

        worker._exchange = exchange
        released = []
        worker.thread_grant = lambda want: 1
        worker.thread_release = lambda: released.append(True)
        worker.request({"op": "speak"})
        assert sent["threads"] == 1 and released == [True]
    finally:
        worker._proc = None
        worker.close()


# ------------------------------------------------------------ dynamic

def test_other_load_lowers_threads_for_the_next_generation_and_idle_raises_them(tmp_path):
    gov = governor(tmp_path)
    events = []
    gov._publish = lambda kind, data: events.append((kind, data))
    assert gov.target_threads == 6
    busy = 5 / 16            # other programs use 5 logical CPUs (> 2-core headroom)
    assert gov.observe(busy) is False                     # one spike is not enough
    assert gov.observe(busy) is True
    assert gov.target_threads == 3 and gov.state == "yielding"
    assert "other programs" in gov.reason
    assert gov.before_generate() == 3
    gov.after_generate()
    assert gov.voice_threads(4) <= 3
    gov.release_voice()
    calm = 1 / 16
    assert gov.observe(calm) is False and gov.observe(calm) is False
    assert gov.observe(calm) is True                      # three calm samples
    assert gov.target_threads == 6 and gov.state == "normal"


def test_load_inside_the_headroom_changes_nothing(tmp_path):
    gov = governor(tmp_path)
    for _ in range(10):
        assert gov.observe(2 / 16) is False               # 2 cores = exactly the headroom
    assert gov.target_threads == 6


def test_dynamic_off_keeps_the_static_budget(tmp_path):
    gov = governor(tmp_path)
    gov._settings.write("resources", {"dynamic": False})
    for _ in range(5):
        gov.observe(15 / 16)
    assert gov.target_threads == 6


def test_never_below_one_thread(tmp_path):
    gov = governor(tmp_path)
    for _ in range(4):
        gov.observe(1.0)
    assert gov.target_threads == 1


def test_sample_publishes_a_resources_event(tmp_path, monkeypatch):
    from services.resources import counters, governor as gmod

    gov = governor(tmp_path)
    events = []
    gov._publish = lambda kind, data: events.append((kind, data))
    monkeypatch.setattr(counters, "gpu_memory", lambda timeout=3.0: None)
    monkeypatch.setattr(gmod, "_memory", lambda: (int(15.8 * GB), int(9 * GB)))
    gov.sample()
    gov.sample()
    kinds = [k for k, _ in events]
    assert kinds and set(kinds) == {"resources"}
    view = events[-1][1]
    for key in ("budget", "live", "imurun", "headroomState", "state", "preset", "disk"):
        assert key in view
    assert view["budget"]["threads"] == 6


# ------------------------------------------------------------ engine knobs

def test_load_env_carries_threads_ram_and_vram_headroom(tmp_path, monkeypatch):
    for key in ("FISH_THREADS", "FISH_RAM_BUDGET_MB", "FISH_VRAM_HEADROOM_MB"):
        monkeypatch.delenv(key, raising=False)
    gov = governor(tmp_path)
    env = gov.load_env()
    assert env["FISH_THREADS"] == "6"
    assert env["FISH_VRAM_HEADROOM_MB"] == "537"          # max(256 MB, 10 %) + 25 % of the 0.5 GB others hold
    b = gov.budget()
    assert int(env["FISH_RAM_BUDGET_MB"]) == b.ram_budget_now(int(9 * GB)) // (1024 * 1024)


def test_load_env_respects_a_users_own_engine_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("FISH_THREADS", "3")
    gov = governor(tmp_path)
    assert "FISH_THREADS" not in gov.load_env()


def test_native_client_applies_the_governed_knobs(tmp_path, monkeypatch):
    from model import fish_native

    for key in ("FISH_THREADS", "FISH_RAM_BUDGET_MB", "FISH_VRAM_HEADROOM_MB"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(fish_native.FishNativeClient, "_SHELL_KNOBS", frozenset())
    client = fish_native.FishNativeClient(SimpleNamespace(model_path="", threads=0, ram_budget_mb=0,
                                                          vram_headroom_mb=0))
    client.resource_hook = governor(tmp_path)
    client._apply_load_budget()
    import os

    assert os.environ["FISH_THREADS"] == "6"
    assert os.environ["FISH_VRAM_HEADROOM_MB"] == "537"
    # An explicit FishConfig value is a hard override of the governor.
    client.config.threads = 2
    client._apply_load_budget()
    assert os.environ["FISH_THREADS"] == "2"


# ------------------------------------------------------------ disk

def test_disk_headroom_refuses_with_a_clear_message(tmp_path):
    import shutil

    gov = governor(tmp_path)
    free = shutil.disk_usage(tmp_path).free
    ok = gov.check_disk(tmp_path, 0, "nothing")
    big = gov.check_disk(tmp_path, free, "downloading a 9 TB model")
    assert not big.ok
    assert "Not enough disk" in big.message and "keeps" in big.message
    assert big.min_free >= 10 * GB
    assert ok.view()["minFreeGb"] == round(big.min_free / GB, 2)


def test_model_download_refused_when_it_would_eat_the_headroom(tmp_path, monkeypatch):
    import shutil

    gov = governor(tmp_path)
    set_governor(gov)
    try:
        model_id = next(iter(mm.RECOMMENDED_MODELS))
        info = mm.RECOMMENDED_MODELS[model_id]
        monkeypatch.setitem(mm.RECOMMENDED_MODELS, model_id,
                            mm.ModelInfo(**{**info.__dict__, "size_bytes": shutil.disk_usage(tmp_path).free}))
        manager = mm.ModelManager(tmp_path / "models")

        async def first():
            async for progress in manager.download_model(model_id):
                return progress

        progress = asyncio.run(first())
        assert progress.status == "error"
        assert "Not enough disk" in progress.error
        assert manager.disk_status()["ok"] is True
        assert "usableGb" in manager.disk_status()
    finally:
        set_governor(None)


def test_tuning_export_refused_by_disk_headroom(tmp_path, monkeypatch):
    from services.tuning.paths import TuningPaths
    from services.tuning.scheduler import Scheduler
    from tests.tuning_fakes import FakeBackend, fixture_sessions, laptop

    monkeypatch.setattr(mm, "FISH_COMPATIBLE_IDS", set(mm.FISH_COMPATIBLE_IDS))
    store, fb = fixture_sessions(tmp_path)
    settings = SettingsStore(tmp_path / "settings")
    settings.write("tuning", {"min_samples": 1, "model": "llama-3.2-1b-q4"})

    class Gov:
        def __init__(self):
            self.threads = []

        def tuning_threads(self):
            self.threads.append(3)
            return 3

        def release_tuning(self):
            pass

        def check_disk(self, path, need, what):
            ok = not what.startswith("exporting")
            return SimpleNamespace(ok=ok, message="" if ok else f"Not enough disk for {what}")

    gov = Gov()
    backend = FakeBackend(tmp_path / "models")
    sched = Scheduler(TuningPaths.at(tmp_path / "tuning"), settings=settings, feedback=fb, transcripts=store,
                      backend=backend, catalogue=dict(mm.RECOMMENDED_MODELS), hardware=laptop, resources=gov)
    status, payload = sched.submit(confirm=True)
    assert status == 202, payload
    sched.wait(10)
    job = sched.get(payload["job"]["id"])
    assert job.state == "failed"
    assert "Not enough disk for exporting" in job.error
    assert "export" not in backend.calls
    assert gov.threads                                   # the trainer got a governed thread count


# ------------------------------------------------------------ settings API

def test_settings_section_validates_and_shows_live_numbers(tmp_path):
    from services.persona.store import PersonaStore
    from services.server.api import ApiServer
    from services.skills.store import SkillsStore

    class Model:
        def generate(self, *_a, **_k):
            return "ok"

    server = ApiServer(persona_store=PersonaStore(tmp_path / "persona.json"), sessions_root=tmp_path / "sessions",
                       data_root=tmp_path, model=Model(), skills=SkillsStore(tmp_path / "skills")).start()
    try:
        m = machine()
        server._governor._measure = lambda: m

        def call(method, path, payload=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
            body = json.dumps(payload).encode() if payload is not None else None
            conn.request(method, path, body=body, headers={"Authorization": f"Bearer {server.token}",
                                                           "Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read().decode() or "{}")
            conn.close()
            return resp.status, data

        status, data = call("GET", "/api/settings/resources")
        assert status == 200
        values = data["values"]
        assert values["headroom"] == "balanced" and values["dynamic"] is True
        assert values["computed"]["budget"]["threads"] == 6
        status, data = call("PUT", "/api/settings/resources", {"headroom": "turbo"})
        assert status == 400 and "headroom" in data["error"]
        status, data = call("PUT", "/api/settings/resources",
                            {"headroom": "performance", "computed": {"budget": {"threads": 99}}})
        assert status == 200
        assert data["values"]["computed"]["budget"]["threads"] == 7
        assert "computed" not in server._settings.read("resources")
        status, data = call("GET", "/api/resources")
        assert status == 200 and data["governor"]["preset"] == "performance"
        status, data = call("GET", "/api/resources/budget")
        assert status == 200 and data["budget"]["headroom"]["preset"] == "performance"
    finally:
        server.stop()
        set_governor(None)

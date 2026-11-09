"""Resource governor: device-class headroom, VRAM share, GPU detection
fallbacks (captured tool outputs), engine placement merge, process priority,
sampler state machine, listener-gated events and the API round trip."""
from __future__ import annotations

import http.client
import json
from types import SimpleNamespace

import pytest

from services.resources import ResourceGovernor, counters, set_governor
from services.resources import governor as gmod
from services.resources.budget import GB, Machine, compute_budget, validate_settings, vram_headroom_bytes
from services.resources.priority import (BELOW_NORMAL_PRIORITY_CLASS, NORMAL_PRIORITY_CLASS, PosixBackend,
                                         PriorityGuard, WindowsBackend)
from services.server.events import EventBroker
from services.server.settings import SettingsStore

MiB = 1024**2


def machine(**kw) -> Machine:
    base = dict(physical_cores=8, logical_cores=16, ram_total=int(15.8 * GB), ram_available=int(9 * GB),
                vram_total=4 * GB, vram_free=int(3.7 * GB), gpu_name="NVIDIA GeForce RTX 3050 Laptop GPU",
                disk_total=400 * GB, disk_free=270 * GB, device_class="laptop", gpu_vendor="nvidia",
                gpu_source="nvidia-smi")
    base.update(kw)
    return Machine(**base)


class FakeBackend:
    mode = "below_normal"
    sticky = False

    def __init__(self):
        self.calls = []

    def lower(self):
        self.calls.append("lower")
        return NORMAL_PRIORITY_CLASS

    def restore(self, saved):
        self.calls.append(("restore", saved))


def governor(tmp_path, backend=None, **kw) -> ResourceGovernor:
    m = machine(**kw)
    return ResourceGovernor(SettingsStore(tmp_path / "settings"), data_root=tmp_path, measure_fn=lambda: m,
                            priority=PriorityGuard(backend or FakeBackend()))


# ------------------------------------------------------------ CPU headroom per device class

@pytest.mark.parametrize("cores, klass, threads", [
    (4, "laptop", 3), (4, "desktop", 3),          # 30 %: 1 core free
    (8, "laptop", 6), (8, "desktop", 6),          # 2.4 -> 2 / 2.0 -> 2
    (6, "laptop", 4), (6, "desktop", 4),          # 1.8 -> 2 / 1.5 -> 2
    (12, "laptop", 9), (12, "desktop", 9),        # 25 % = 3 / 20 % = 2.4 -> at least 20 %: 3
    (16, "laptop", 12), (16, "desktop", 12),      # 4 / 3.2 -> 4
    (24, "laptop", 19), (24, "desktop", 19),      # 20 % = 4.8 -> 5
    (12, "unknown", 9), (12, "", 9),              # unknown is treated as a laptop
])
def test_thread_grant_by_device_class(cores, klass, threads):
    b = compute_budget(machine(physical_cores=cores, logical_cores=cores * 2, device_class=klass))
    assert b.threads == threads
    assert 0.2 <= b.headroom_cores / cores <= 0.34
    assert 1 <= b.threads <= cores - 1


def test_thread_grant_bounds_on_tiny_machines():
    assert compute_budget(machine(physical_cores=1, logical_cores=1)).threads == 1
    assert compute_budget(machine(physical_cores=2, logical_cores=2), "performance").threads == 1
    assert compute_budget(machine(physical_cores=0, logical_cores=0)).threads == 1


# ------------------------------------------------------------ VRAM share

def test_vram_headroom_leaves_an_8b_room_on_a_4gb_card():
    m = machine(vram_free=int(3.7 * GB), vram_used=int(0.3 * GB))
    b = compute_budget(m)
    assert b.vram_headroom == int(486.4 * MiB)               # 409.6 MB + 25 % of 307.2 MB; was 1024 MB
    assert b.vram_budget > int(3.1 * GB)                     # the 8B residency the engine needs


def test_vram_headroom_rules():
    assert vram_headroom_bytes(0) == 0
    assert vram_headroom_bytes(1 * GB) == 256 * MiB          # floor
    assert vram_headroom_bytes(24 * GB) == int(2.4 * GB)     # 10 %, others idle
    assert vram_headroom_bytes(4 * GB, 2 * GB) == int(409.6 * MiB) + 512 * MiB   # growth capped at 512 MB
    assert vram_headroom_bytes(4 * GB, 0, 2.0) == int(819.2 * MiB)              # gentle doubles
    assert vram_headroom_bytes(4 * GB, 0, 0.5) == 256 * MiB                     # performance, floored
    assert vram_headroom_bytes(1 * GB, 1 * GB, 10.0) == 512 * MiB               # cap: half the card


def test_identity_only_gpu_has_no_others_and_a_note():
    m = machine(vram_free=4 * GB, vram_live=False, gpu_source="registry")
    b = compute_budget(m)
    assert b.vram_used_by_others == 0 and b.vram_headroom == int(409.6 * MiB)
    assert any("registry" in n for n in b.notes)


def test_presets_eco_alias_and_lower_priority_validate():
    assert validate_settings({"preset": "eco"}) == {"headroom": "gentle"}
    assert validate_settings({"headroom": "ECO"}) == {"headroom": "gentle"}
    assert validate_settings({"lowerPriority": False}) == {"lowerPriority": False}
    for bad in ({"preset": "turbo"}, {"lowerPriority": 1}, {"preset": "eco", "headroom": "balanced"}):
        with pytest.raises(ValueError):
            validate_settings(bad)


# ------------------------------------------------------------ GPU detection parsing

NVIDIA_SMI = "NVIDIA GeForce RTX 3050 Laptop GPU, 4096, 787, 3165, 8\n"
NVIDIA_SMI_TWO = "NVIDIA T400, 2048, 100, 1948, [N/A]\nNVIDIA GeForce RTX 3090, 24576, 500, 24076, 3\n"
ROCM_SMI = json.dumps({"card0": {"Card series": "Radeon RX 7800 XT", "Card model": "0x7480",
                                 "VRAM Total Memory (B)": "17163091968",
                                 "VRAM Total Used Memory (B)": "1073741824"}})
CIM_JSON = json.dumps([
    {"Name": "Intel(R) UHD Graphics", "AdapterRAM": 1073741824, "AdapterCompatibility": "Intel Corporation"},
    {"Name": "NVIDIA GeForce RTX 3050 Laptop GPU", "AdapterRAM": 4293918720, "AdapterCompatibility": "NVIDIA"},
    {"Name": "Microsoft Basic Display Adapter", "AdapterRAM": 0, "AdapterCompatibility": "(Standard)"},
])
REGISTRY = [
    {"DriverDesc": "Intel(R) UHD Graphics", "ProviderName": "Intel Corporation",
     "HardwareInformation.qwMemorySize": 128 * MiB},
    {"DriverDesc": "NVIDIA GeForce RTX 3050 Laptop GPU", "ProviderName": "NVIDIA",
     "HardwareInformation.qwMemorySize": 4 * GB},
    {"DriverDesc": "AMD Radeon(TM) Graphics", "ProviderName": "Advanced Micro Devices, Inc.",
     "HardwareInformation.MemorySize": (512 * MiB).to_bytes(4, "little")},
]
SYSTEM_PROFILER = json.dumps({"SPDisplaysDataType": [
    {"sppci_model": "Apple M2 Pro", "spdisplays_vendor": "sppci_vendor_Apple"}]})


def test_parse_nvidia_smi():
    card = counters.parse_nvidia_smi(NVIDIA_SMI)
    assert card.name == "NVIDIA GeForce RTX 3050 Laptop GPU" and card.vendor == "nvidia"
    assert (card.total, card.used, card.free) == (4096 * MiB, 787 * MiB, 3165 * MiB)
    assert card.utilization == pytest.approx(0.08) and card.live
    two = counters.parse_nvidia_smi(NVIDIA_SMI_TWO)
    assert two.name.endswith("3090")
    assert counters.parse_nvidia_smi("") is None
    assert counters.parse_nvidia_smi("garbage line\n") is None


def test_parse_rocm_smi():
    card = counters.parse_rocm_smi(ROCM_SMI)
    assert card.name == "Radeon RX 7800 XT" and card.vendor == "amd" and card.source == "rocm-smi"
    assert card.total == 17163091968 and card.used == GB and card.live
    assert counters.parse_rocm_smi("not json") is None


def test_parse_cim_prefers_the_discrete_card_and_skips_basic_display():
    card = counters.parse_cim_json(CIM_JSON)
    assert card.name == "NVIDIA GeForce RTX 3050 Laptop GPU" and card.vendor == "nvidia"
    assert card.source == "cim" and not card.live and card.total == 4293918720
    single = counters.parse_cim_json(json.dumps({"Name": "Intel(R) Iris(R) Xe Graphics", "AdapterRAM": 1 << 30}))
    assert single.vendor == "intel"
    assert counters.parse_cim_json(json.dumps([{"Name": "Microsoft Basic Display Adapter"}])) is None


def test_parse_registry_reads_64bit_and_binary_sizes():
    card = counters.parse_registry(REGISTRY)
    assert card.name == "NVIDIA GeForce RTX 3050 Laptop GPU" and card.total == 4 * GB
    assert card.source == "registry" and not card.live
    amd = counters.parse_registry(REGISTRY[2:])
    assert amd.vendor == "amd" and amd.total == 512 * MiB


def test_parse_sysfs_tree(tmp_path):
    for card, vendor, total, used in (("card0", "0x8086", None, None), ("card1", "0x1002", 8 * GB, GB)):
        dev = tmp_path / card / "device"
        dev.mkdir(parents=True)
        (dev / "vendor").write_text(vendor + "\n")
        (dev / "device").write_text("0x73bf\n")
        if total:
            (dev / "mem_info_vram_total").write_text(str(total))
            (dev / "mem_info_vram_used").write_text(str(used))
    (tmp_path / "card1-DP-1").mkdir()                       # a connector, not a card
    card = counters.parse_drm(str(tmp_path))
    assert card.vendor == "amd" and card.total == 8 * GB and card.used == GB and card.live
    assert card.source == "sysfs"
    assert counters.parse_drm(str(tmp_path / "missing")) is None


def test_parse_system_profiler_apple_unified_memory():
    card = counters.parse_system_profiler(SYSTEM_PROFILER)
    assert card.name == "Apple M2 Pro" and card.vendor == "apple" and card.total == 0


def test_detect_gpu_falls_back_to_os_identity(monkeypatch):
    monkeypatch.setattr(counters, "gpu_memory", lambda timeout=3.0: None)
    monkeypatch.setattr(counters, "gpu_identity", lambda timeout=8.0: counters.parse_registry(REGISTRY))
    card = counters.detect_gpu()
    assert card is not None and card.source == "registry"
    monkeypatch.setattr(gmod, "_memory", lambda: (16 * GB, 9 * GB))
    m = gmod.measure()
    assert m.gpu_name == "NVIDIA GeForce RTX 3050 Laptop GPU" and m.vram_total == 4 * GB
    assert m.gpu_source == "registry" and not m.vram_live and m.vram_others == 0


def test_measure_counts_imurun_own_vram_as_free(monkeypatch):
    monkeypatch.setattr(counters, "detect_gpu", lambda timeout=3.0: counters.parse_nvidia_smi(
        "NVIDIA GeForce RTX 3050 Laptop GPU, 4096, 1200, 2896, 50\n"))
    monkeypatch.setattr(gmod, "_memory", lambda: (16 * GB, 9 * GB))
    m = gmod.measure(own_vram=900 * MiB)
    assert m.vram_used == 300 * MiB and m.vram_free == 3796 * MiB


# ------------------------------------------------------------ engine placement merge

STATS = {"threads": 6, "tokens_per_second": 21.37, "prefill_tokens_per_second": 180.0, "peak_rss_mb": 1400,
         "placement": {"gpu_layers": 16, "ram_layers": 0, "disk_layers": 0, "gpu_bytes": int(0.8 * GB),
                       "gpu": "NVIDIA GeForce RTX 3050 Laptop GPU (vulkan)", "mix": "q4_k->int8"}}


def test_view_merges_engine_placement_and_label(tmp_path):
    gov = governor(tmp_path)
    gov.load_env()
    budget_at_load = gov.budget().vram_budget
    state = {"stats": STATS}
    gov.attach_engine(lambda: state["stats"])
    view = gov.view()
    eng = view["imurun"]["engine"]
    assert eng["gpu"].endswith("(vulkan)") and eng["totalLayers"] == 16 and eng["tokensPerSecond"] == 21.37
    assert eng["prefillTokensPerSecond"] == 180.0 and eng["threads"] == 6
    assert view["engine"]["loaded"] is True
    gpu = view["gpu"]
    assert gpu["engine"]["gpuLayers"] == 16 and gpu["vramBudget"] == budget_at_load
    assert gpu["label"] == f"GPU: RTX 3050 Laptop GPU - 16/16 layers, 0.8 GB of {budget_at_load / GB:.1f} GB"
    assert view["machine"]["gpu"] == "NVIDIA GeForce RTX 3050 Laptop GPU"      # still a string
    assert view["machine"]["gpuInfo"]["vendor"] == "nvidia"
    state["stats"] = {}                       # a reply holds the context: keep the last report
    assert gov.view()["engine"]["gpuLayers"] == 16
    state["stats"] = {"error": "model not loaded"}
    view = gov.view()
    assert view["engine"] is None and view["imurun"]["engine"] == {} and view["gpu"]["engine"] is None


def test_engine_gpu_is_used_when_the_os_sees_none(tmp_path):
    gov = governor(tmp_path, gpu_name="", vram_total=0, vram_free=0, gpu_source="", gpu_vendor="")
    gov.attach_engine(lambda: STATS)
    gpu = gov.view()["gpu"]
    assert gpu["source"] == "engine" and gpu["vendor"] == "nvidia" and "16/16" in gpu["label"]
    assert gov.view()["machine"]["gpu"].startswith("NVIDIA")


def test_no_gpu_anywhere_is_null(tmp_path):
    gov = governor(tmp_path, gpu_name="", vram_total=0, vram_free=0)
    assert gov.view()["gpu"] is None


def test_model_load_publishes_placement(tmp_path):
    gov = governor(tmp_path)
    events = []
    gov._publish = lambda kind, data: events.append((kind, data))
    gov.attach_engine(lambda: STATS)
    gov.note_engine(pool_threads=6, can_retarget=False)
    assert events and events[-1][1]["engine"]["gpuLayers"] == 16


# ------------------------------------------------------------ priority

class FakeKernel32:
    def __init__(self, current=NORMAL_PRIORITY_CLASS, ok=True):
        self.current, self.ok, self.calls = current, ok, []

    def GetCurrentProcess(self):  # noqa: N802
        return -1

    def GetPriorityClass(self, handle):  # noqa: N802
        return self.current

    def SetPriorityClass(self, handle, value):  # noqa: N802
        self.calls.append(value)
        if self.ok:
            self.current = value
        return self.ok


def test_windows_backend_lowers_and_restores_the_saved_class():
    k = FakeKernel32(current=0x8000)          # ABOVE_NORMAL before we started
    guard = PriorityGuard(WindowsBackend(k))
    guard.acquire("chat")
    assert k.calls == [BELOW_NORMAL_PRIORITY_CLASS] and guard.lowered
    guard.acquire("voice")                    # nested: no second call
    guard.release("chat")
    assert guard.lowered and k.calls == [BELOW_NORMAL_PRIORITY_CLASS]
    guard.release("voice")
    assert k.calls == [BELOW_NORMAL_PRIORITY_CLASS, 0x8000] and not guard.lowered
    assert guard.view()["mode"] == "below_normal"


def test_priority_failure_is_reported_not_raised():
    guard = PriorityGuard(WindowsBackend(FakeKernel32(ok=False)))
    guard.acquire("chat")
    assert not guard.lowered and "SetPriorityClass" in guard.view()["error"]
    guard.release("chat")


def test_posix_backend_is_sticky(monkeypatch):
    calls = []
    monkeypatch.setattr("os.nice", lambda n: calls.append(n) or n, raising=False)
    guard = PriorityGuard(PosixBackend())
    guard.acquire("chat")
    guard.release("chat")
    guard.acquire("chat")
    assert calls == [10] and guard.view()["sticky"] is True


def test_governor_lowers_priority_for_chat_voice_and_tuning(tmp_path):
    backend = FakeBackend()
    gov = governor(tmp_path, backend)
    gov.before_generate()
    assert backend.calls == ["lower"] and gov.view()["priority"]["holders"] == ["chat"]
    gov.voice_threads(2)
    gov.after_generate()
    assert gov.priority.lowered                       # voice still runs
    gov.release_voice()
    assert backend.calls == ["lower", ("restore", NORMAL_PRIORITY_CLASS)]
    gov.tuning_threads()
    assert gov.priority.lowered
    gov.release_tuning()
    assert not gov.priority.lowered


def test_lower_priority_setting_off_and_switching_off_mid_job(tmp_path):
    backend = FakeBackend()
    gov = governor(tmp_path, backend)
    gov.before_generate()
    gov._settings.write("resources", {"lowerPriority": False})
    gov.refresh()
    assert not gov.priority.lowered and backend.calls[-1][0] == "restore"
    gov.after_generate()
    gov.before_generate()
    assert backend.calls.count("lower") == 1          # off: no further lowering
    assert gov.view()["priority"]["mode"] == "off" and gov.view()["lowerPriority"] is False
    gov.after_generate()


def test_stop_restores_priority(tmp_path):
    backend = FakeBackend()
    gov = governor(tmp_path, backend)
    gov.before_generate()
    gov.stop()
    assert not gov.priority.lowered


# ------------------------------------------------------------ sampler state machine

class FakeMeter:
    def __init__(self, shares):
        self.shares = list(shares)

    def sample(self, pids=()):
        other = self.shares.pop(0)
        return {"system": other, "own": 0.0, "other": other, "interval": 2.0}


def test_sampler_lowers_after_two_hot_samples_and_raises_after_three_calm(tmp_path, monkeypatch):
    monkeypatch.setattr(gmod, "_memory", lambda: (16 * GB, 9 * GB))
    hot, calm = 6 / 16, 1 / 16
    gov = governor(tmp_path)
    gov._gpu_sampler = lambda: None
    gov._meter = FakeMeter([hot, hot, calm, calm, hot, calm, calm, calm])
    targets = []
    for _ in range(8):
        gov.sample()
        targets.append((gov.target_threads, gov.state))
    assert targets[0] == (6, "normal")                      # one hot sample: nothing
    assert targets[1] == (2, "yielding")                    # second: 8 - 6 busy cores
    assert targets[2][0] == 2 and targets[3][0] == 2
    assert targets[4][0] == 2                               # a hot sample resets the calm count
    assert targets[5][0] == 2 and targets[6][0] == 2
    assert targets[7] == (6, "normal")                      # third calm in a row
    assert gov.before_generate() == 6
    gov.after_generate()


def test_events_only_while_a_client_listens(tmp_path, monkeypatch):
    monkeypatch.setattr(gmod, "_memory", lambda: (16 * GB, 9 * GB))
    broker = EventBroker()
    gov = governor(tmp_path)
    polled = []
    gov._gpu_sampler = lambda: polled.append(1)
    gov._publish = broker.publish
    gov._meter = FakeMeter([0.0] * 4)
    built = []
    real_view = gov.view
    gov.view = lambda: built.append(1) or real_view()
    gov.sample()
    gov.refresh()
    assert gov.listeners() == 0 and not built and not polled   # nobody listens: no view, no GPU poll
    sub = broker.subscribe()
    gov.sample()
    event = sub.get(timeout=1)
    assert event is not None and event.type == "resources" and polled
    broker.unsubscribe(sub)


# ------------------------------------------------------------ API round trip

def test_update_handler_validates_and_applies(tmp_path):
    from services.server.resources import ResourceSampler, handle_update

    gov = governor(tmp_path)
    server = SimpleNamespace(_settings=gov._settings, _governor=gov, _resources=ResourceSampler(tmp_path))
    assert handle_update(server, {})[0] == 400
    assert handle_update(server, {"turbo": True})[0] == 400
    status, body = handle_update(server, {"preset": "nonsense"})
    assert status == 400 and "preset" in body["error"]
    status, body = handle_update(server, {"preset": "eco", "dynamic": False, "lowerPriority": False})
    assert status == 200 and body["ok"]
    view = body["governor"]
    assert view["preset"] == "gentle" and view["dynamic"] is False and view["lowerPriority"] is False
    assert gov._settings.read("resources")["headroom"] == "gentle"
    status, body = handle_update(server, {"preset": "custom", "custom": {"cpu": 50}})
    assert status == 200 and body["governor"]["custom"]["cpu"] == 50.0
    assert body["governor"]["budget"]["threads"] == 4


def test_settings_put_applies_to_the_governor_at_runtime(tmp_path):
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
        server._governor._priority = PriorityGuard(FakeBackend())

        def call(method, path, payload=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
            body = json.dumps(payload).encode() if payload is not None else None
            conn.request(method, path, body=body, headers={"Authorization": f"Bearer {server.token}",
                                                           "Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read().decode() or "{}")
            conn.close()
            return resp.status, data

        status, data = call("PUT", "/api/settings/resources", {"headroom": "eco", "lowerPriority": False})
        assert status == 200
        assert data["values"]["computed"]["preset"] == "gentle"
        assert data["values"]["computed"]["lowerPriority"] is False
        assert server._governor.target_threads == 3                 # gentle on 8 laptop cores
        status, data = call("PUT", "/api/settings/resources", {"lowerPriority": "yes"})
        assert status == 400
        status, data = call("GET", "/api/resources")
        gov = data["governor"]
        assert gov["preset"] == "gentle" and gov["presets"] == ["gentle", "balanced", "performance", "custom"]
        assert gov["gpu"]["name"] == "NVIDIA GeForce RTX 3050 Laptop GPU" and gov["gpu"]["label"]
        assert gov["machine"]["deviceClass"] == "laptop" and gov["priority"]["enabled"] is False
    finally:
        server.stop()
        set_governor(None)

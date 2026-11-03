"""ResourceBudget for synthetic machines (docs/resources/README.md formulas)."""
from __future__ import annotations

import pytest

from services.resources.budget import (
    GB,
    Machine,
    compute_budget,
    headroom_for,
    normalize_custom,
    os_reserve,
    validate_settings,
)

GiB = GB
MiB = 1024**2


def laptop_4c_8g() -> Machine:
    return Machine(physical_cores=4, logical_cores=8, ram_total=int(7.8 * GiB), ram_available=int(5 * GiB),
                   disk_total=256 * GiB, disk_free=100 * GiB)


def laptop_8c_16g_4g() -> Machine:   # the owner's class of machine (RTX 3050 Laptop 4 GB)
    return Machine(physical_cores=8, logical_cores=16, ram_total=int(15.8 * GiB), ram_available=int(9 * GiB),
                   vram_total=4 * GiB, vram_free=int(3.5 * GiB), gpu_name="RTX 3050 Laptop",
                   disk_total=400 * GiB, disk_free=270 * GiB)


def desktop_16c_64g_24g() -> Machine:
    return Machine(physical_cores=16, logical_cores=32, ram_total=64 * GiB, ram_available=50 * GiB,
                   vram_total=24 * GiB, vram_free=23 * GiB, gpu_name="RTX 4090",
                   disk_total=2000 * GiB, disk_free=1500 * GiB, device_class="desktop")


def no_gpu_6c_32g() -> Machine:
    return Machine(physical_cores=6, logical_cores=12, ram_total=32 * GiB, ram_available=20 * GiB,
                   disk_total=1000 * GiB, disk_free=500 * GiB)


def test_small_laptop_keeps_30_percent_cpu_and_25_percent_ram():
    b = compute_budget(laptop_4c_8g())
    assert b.headroom.cpu == pytest.approx(0.30)
    assert (b.threads, b.headroom_cores) == (3, 1)          # round(4 * 0.3) = 1 core free
    assert b.headroom.ram == pytest.approx(0.25)
    assert b.ram_os_reserve == int(1.5 * GiB)               # max(1.5 GB, total / 8)
    assert b.ram_budget == int(7.8 * GiB) - int(1.5 * GiB) - int(7.8 * GiB * 0.25)
    assert b.vram_total == 0 and b.vram_budget == 0
    assert b.disk_min_free == int(256 * GiB * 0.10)          # 10 % of the drive
    small = compute_budget(Machine(4, 8, 8 * GiB, 4 * GiB, disk_total=64 * GiB, disk_free=30 * GiB))
    assert small.disk_min_free == 10 * GiB                   # 10 % of 64 GB < the 10 GB floor


def test_8_core_16gb_4gb_gpu_laptop():
    m = laptop_8c_16g_4g()
    b = compute_budget(m)
    assert (b.threads, b.headroom_cores) == (6, 2)          # 30 % of 8 laptop cores = 2.4 -> 2
    assert b.headroom.ram == pytest.approx(0.20)
    assert b.ram_budget == m.ram_total - os_reserve(m.ram_total) - int(m.ram_total * 0.20)
    assert 10 * GiB < b.ram_budget < 11 * GiB
    # 10 % of the card (409.6 MB) + 25 % of the 512 MB other programs hold.
    assert b.vram_headroom == int(537.6 * MiB)
    assert b.vram_used_by_others == 512 * MiB
    assert b.vram_budget == int(3.5 * GiB) - int(537.6 * MiB)   # ~2.97 GB (was 2.5 with a flat 25 %)
    assert b.disk_min_free == 40 * GiB                      # 10 % of 400 GB


def test_big_desktop_keeps_20_percent_cpu_and_15_percent_ram_and_vram():
    m = desktop_16c_64g_24g()
    b = compute_budget(m)
    assert (b.threads, b.headroom_cores) == (12, 4)         # 16 * 0.2 = 3.2 -> never under 20 %: 4
    assert b.ram_os_reserve == 4 * GiB                      # total / 8 = 8 GB, capped at 4
    assert b.ram_budget == 64 * GiB - 4 * GiB - int(64 * GiB * 0.15)
    assert b.vram_headroom == int(24 * GiB * 0.10)          # others hold 1 GB < 5 %: no growth term
    assert b.vram_budget == 23 * GiB - int(24 * GiB * 0.10)
    assert b.disk_min_free == 200 * GiB


def test_no_gpu_machine_has_no_vram_budget_and_says_so():
    b = compute_budget(no_gpu_6c_32g())
    assert (b.threads, b.headroom_cores) == (4, 2)          # 6 * 0.25 = 1.5 -> 2
    assert b.vram_budget == 0 and b.vram_headroom == 0
    assert any("no GPU" in note for note in b.notes)
    assert b.headroom.ram == pytest.approx(0.15)            # 32 GB: 15 %


def test_hyperthreads_never_count_and_one_core_always_stays_free():
    two = Machine(physical_cores=2, logical_cores=4, ram_total=4 * GiB, ram_available=2 * GiB)
    assert compute_budget(two).threads == 1
    assert compute_budget(two, "performance").threads == 1   # still one core free
    single = Machine(physical_cores=1, logical_cores=2, ram_total=2 * GiB, ram_available=1 * GiB)
    b = compute_budget(single)
    assert b.threads == 1 and b.headroom_cores == 0 and b.notes
    for machine in (laptop_4c_8g(), laptop_8c_16g_4g(), desktop_16c_64g_24g()):
        for preset in ("balanced", "performance", "gentle"):
            b = compute_budget(machine, preset)
            assert 1 <= b.threads <= machine.physical_cores - 1


def test_presets_scale_the_headroom():
    m = laptop_8c_16g_4g()
    bal, perf, gentle = (compute_budget(m, p) for p in ("balanced", "performance", "gentle"))
    assert perf.headroom.cpu == pytest.approx(bal.headroom.cpu / 2)
    assert gentle.headroom.ram == pytest.approx(bal.headroom.ram * 2)
    assert perf.threads == 7 and bal.threads == 6 and gentle.threads == 3
    assert perf.ram_budget > bal.ram_budget > gentle.ram_budget
    assert perf.vram_headroom == int(268.8 * MiB)            # half of 537.6 MB, above the 256 MB floor
    assert perf.disk_min_free == 20 * GiB and gentle.disk_min_free == 80 * GiB


def test_custom_percentages_and_fallback_to_balanced():
    m = laptop_8c_16g_4g()
    room = headroom_for(m, "custom", {"cpu": 50, "vram": 10})
    assert room.cpu == pytest.approx(0.5) and room.vram == pytest.approx(0.1)
    assert room.ram == pytest.approx(0.20)                  # missing keys: balanced
    b = compute_budget(m, "custom", {"cpu": 50, "vram": 10})
    assert b.threads == 4
    assert b.vram_headroom == int(4 * GiB * 0.1)
    assert compute_budget(m, "nonsense").headroom.preset == "balanced"


def test_ram_budget_now_keeps_the_users_headroom_when_ram_is_already_full():
    b = compute_budget(laptop_8c_16g_4g())
    assert b.ram_budget_now(int(14 * GiB)) == b.ram_budget             # plenty free: static
    assert b.ram_budget_now(int(6 * GiB)) == int(6 * GiB) - b.ram_headroom
    assert b.ram_budget_now(int(2.9 * GiB)) == 0                      # measured on this laptop


def test_settings_presets_validate():
    assert validate_settings({"headroom": "gentle"}) == {"headroom": "gentle"}
    assert validate_settings({"headroom": "custom", "custom": {"cpu": 30, "disk": 15.5}})["custom"] == {
        "cpu": 30.0, "disk": 15.5}
    assert validate_settings({"dynamic": False}) == {"dynamic": False}
    for bad in ({"headroom": "turbo"}, {"custom": {"cpu": 95}}, {"custom": {"cpu": -1}},
                {"custom": {"gpu": 10}}, {"custom": {"ram": "lots"}}, {"custom": [1]},
                {"custom": {"ram": True}}, {"dynamic": "yes"}):
        with pytest.raises(ValueError):
            validate_settings(bad)
    assert normalize_custom({}, laptop_4c_8g())["cpu"] == 30.0

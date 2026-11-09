"""The feasibility gate: honest per-machine verdicts, enforced before any run."""
from __future__ import annotations

from services.tuning import feasibility
from services.tuning.catalogue import base_for, catalogue_id_for_filename, parent_id
from services.tuning.feasibility import Hardware
from tests.tuning_fakes import laptop

# What setup probed on the owner's laptop (bitsandbytes 0.50 runs NF4 on GPU and CPU).
CAPS = {"cuda": True, "bitsandbytes": True, "bf16": True, "bnb_cpu": True}


def test_1b_and_3b_fit_the_4gb_laptop_gpu():
    for model in ("llama-3.2-1b-q4", "llama-3.2-3b-q4", "qwen2-5-1.5b-q4"):
        plan = feasibility.plan(model, hardware=laptop(), caps=CAPS)
        assert plan.verdict == "ok", (model, plan.reason)
        assert plan.method == "qlora-gpu" and plan.need_gb <= plan.have_gb


def test_1b_without_bitsandbytes_uses_plain_lora_on_the_gpu():
    plan = feasibility.plan("llama-3.2-1b-q4", hardware=laptop(), caps={**CAPS, "bitsandbytes": False})
    assert plan.verdict == "ok" and plan.method == "lora-gpu"


def test_8b_on_the_laptop_is_refused_with_the_measured_cpu_speed():
    # QLoRA needs ~5.7 GB VRAM; the 4-bit CPU path was measured at ~0.2 tok/s
    # for the 1B here, so an 8B run would take far longer than a week.
    plan = feasibility.plan("deepseek-r1-distill-llama-8b-q4", hardware=laptop(), caps=CAPS)
    assert plan.method == "qlora-cpu" and plan.verdict == "impossible"
    assert "not offered" in plan.reason and plan.est_total_hours > 168
    assert any(o["method"] == "qlora-gpu" and not o["fits"] for o in plan.options)


def test_8b_with_allow_slow_territory_on_a_tiny_dataset():
    # A handful of samples keeps the CPU estimate under a week: then it is
    # "slow" (needs allow_slow) and the estimate is shown.
    plan = feasibility.plan("deepseek-r1-distill-llama-8b-q4", hardware=laptop(), caps=CAPS,
                            train_tokens=2_000, epochs=1)
    assert plan.verdict == "slow" and "8B-class" in plan.reason and plan.est_total_hours > 4


def test_8b_without_cpu_4bit_would_page_to_disk_and_is_refused():
    plan = feasibility.plan("deepseek-r1-distill-llama-8b-q4", hardware=laptop(),
                            caps={**CAPS, "bnb_cpu": False})
    assert plan.method == "lora-cpu-swap" and plan.verdict == "impossible"
    assert "not offered" in plan.reason


def test_8b_fits_a_bigger_gpu():
    big = Hardware(vram_total_gb=8.0, vram_free_gb=7.5, ram_total_gb=32, commit_limit_gb=40, disk_free_gb=500)
    plan = feasibility.plan("deepseek-r1-distill-llama-8b-q4", hardware=big, caps=CAPS, train_tokens=20_000)
    assert plan.method == "qlora-gpu" and plan.verdict == "ok", plan.reason


def test_70b_and_unknown_and_moe_are_impossible():
    assert feasibility.plan("llama-3.1-70b-q4", hardware=laptop(), caps=CAPS).verdict == "impossible"
    unknown = feasibility.plan("llama-3.2-8b-q4", hardware=laptop(), caps=CAPS)
    assert unknown.verdict == "impossible" and "GGUF weights cannot be fine-tuned" in unknown.reason
    assert feasibility.plan("mixtral-8x7b-q4", hardware=laptop(), caps=CAPS).verdict == "impossible"


def test_cpu_only_machine_is_slow_not_ok():
    cpu = Hardware(ram_total_gb=16, commit_limit_gb=20, disk_free_gb=300)
    plan = feasibility.plan("llama-3.2-1b-q4", hardware=cpu, caps={"cuda": False})
    assert plan.method == "lora-cpu" and plan.verdict == "slow"


def test_max_hours_turns_a_long_gpu_run_slow():
    plan = feasibility.plan("llama-3.2-3b-q4", hardware=laptop(), caps=CAPS, train_tokens=2_000_000,
                            max_hours=1)
    assert plan.verdict == "slow" and "max_hours" in plan.reason


def test_disk_space_is_checked():
    tiny = Hardware(vram_total_gb=4, ram_total_gb=16, commit_limit_gb=19, disk_free_gb=3)
    plan = feasibility.plan("llama-3.2-1b-q4", hardware=tiny, caps=CAPS)
    assert plan.verdict == "impossible" and "disk" in plan.reason


def test_measured_throughput_replaces_the_guess(tmp_path):
    path = tmp_path / "hardware.json"
    feasibility.record_throughput(path, "qlora-gpu", tok_s=1000.0, params_b=1.24)
    measured = feasibility.measured_throughput(path)
    plan = feasibility.plan("llama-3.2-1b-q4", hardware=laptop(), caps=CAPS, throughput=measured)
    assert plan.tok_s_measured and abs(plan.tok_s - 1000.0) < 0.01


def test_catalogue_mapping():
    assert base_for("llama-3.2-1b-q4").repo == "meta-llama/Llama-3.2-1B-Instruct"
    assert base_for("deepseek-r1-distill-llama-8b-q4").repo == "deepseek-ai/DeepSeek-R1-Distill-Llama-8B"
    assert base_for("llama-3.2-1b-q4-tuned-r1").catalogue_id == "llama-3.2-1b-q4"
    assert parent_id("phi-3-mini-q4-tuned-r9") == "phi-3-mini-q4"
    assert "qkv_proj" in base_for("phi-3-mini-q4").targets
    assert catalogue_id_for_filename("Llama-3.2-1B-Instruct-Q4_K_M.gguf") == "llama-3.2-1b-q4"
    # Every catalogue model is either mapped or explicitly marked untrainable.
    from services.model_manager import RECOMMENDED_MODELS
    from services.tuning.catalogue import BASES

    assert set(RECOMMENDED_MODELS) <= set(BASES)

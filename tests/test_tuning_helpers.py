"""Pure helpers of the venv-side scripts (importable without torch) and the registry."""
from __future__ import annotations

import re

from services import model_manager as mm
from services.tuning import export, llamacpp, train
from services.tuning.registry import Registry, register, register_all


def test_sanity_pass_matching():
    assert train.sanity_pass(" Paris, the city of light", ("paris",))
    assert train.sanity_pass(" 4.", ("4", "four"))
    assert not train.sanity_pass(" 42", ("4",))            # whole token, not a substring
    assert train.sanity_pass(" Au (from Latin aurum)", ("au",))
    assert not train.sanity_pass(" Australia", ("au",))


def _sanity(passes: dict) -> dict:
    return {prompt: {"pass": passes.get(prompt, True)} for prompt, *_ in train.SANITY_PROMPTS}


def test_acceptance_rules():
    paris = "The capital of France is"
    ok, why = train.evaluate_acceptance(2.0, 1.5, _sanity({}), _sanity({}))
    assert ok, why
    ok, why = train.evaluate_acceptance(2.0, 1.5, _sanity({}), _sanity({paris: False}))
    assert not ok and "France" in why
    # The base already failed a non-required prompt: not a regression.
    two = "2 + 2 ="
    assert train.evaluate_acceptance(2.0, 1.5, _sanity({two: False}), _sanity({two: False}))[0]
    assert not train.evaluate_acceptance(2.0, 1.5, _sanity({}), _sanity({two: False}))[0]
    ok, why = train.evaluate_acceptance(2.0, 2.2, _sanity({}), _sanity({}))
    assert not ok and "held-out loss" in why
    assert train.evaluate_acceptance(None, None, _sanity({}), _sanity({}))[0]


def test_cosine_schedule_reference():
    assert train.cosine_lr(0, 100, 1.0, 5) == 0.0
    assert abs(train.cosine_lr(5, 100, 1.0, 5) - 1.0) < 1e-9
    assert train.cosine_lr(100, 100, 1.0, 5) < 1e-9


def test_adapter_key_mapping_and_filenames():
    key = "base_model.model.model.layers.3.self_attn.q_proj.lora_A.weight"
    assert export.adapter_key_to_base(key) == ("model.layers.3.self_attn.q_proj.weight", "A")
    assert export.adapter_key_to_base("base_model.model.model.layers.0.mlp.down_proj.lora_B.weight") == \
        ("model.layers.0.mlp.down_proj.weight", "B")
    assert export.adapter_key_to_base("model.embed_tokens.weight") is None
    assert export._adapter_scale({"r": 16, "lora_alpha": 32}) == 2.0
    assert export.tuned_filename("Llama-3.2-1B-Instruct-Q4_K_M.gguf", "r1", "Q8_0") == \
        "Llama-3.2-1B-Instruct-tuned-r1-Q8_0.gguf"


def test_release_picking_skips_tags_without_binaries():
    pattern = re.compile(r"^llama-b?\d*-?bin-win-cpu-x64\.(zip|tar\.gz)$")
    releases = [
        {"tag_name": "v0.8.0", "assets": []},
        {"tag_name": "b12032", "assets": [{"name": "llama-b12032-bin-win-cpu-x64.zip"}]},
    ]
    assert llamacpp.pick_release(releases, pattern)["tag_name"] == "b12032"
    assert llamacpp.pick_release([{"tag_name": "x", "assets": []}], pattern) is None


def test_registry_adds_local_models_to_the_catalogue(tmp_path):
    gguf = tmp_path / "Llama-3.2-1B-Instruct-tuned-r1-Q4_K_M.gguf"
    gguf.write_bytes(b"GGUF" + b"\0" * 100)
    registry = Registry(tmp_path / "registry.json")
    registry.add({"id": "llama-3.2-1b-q4-tuned-r1", "parent": "llama-3.2-1b-q4", "run_id": "r1",
                  "path": str(gguf), "adapter_dir": str(tmp_path)})
    registry.add({"id": "llama-3.2-1b-q4-tuned-gone", "parent": "llama-3.2-1b-q4",
                  "path": str(tmp_path / "missing.gguf")})
    catalogue, compatible = dict(mm.RECOMMENDED_MODELS), set(mm.FISH_COMPATIBLE_IDS)
    assert register_all(registry, catalogue, compatible) == 1
    info = catalogue["llama-3.2-1b-q4-tuned-r1"]
    assert info.source == "local" and info.filename == gguf.name and "tuned" in info.tags
    assert "llama-3.2-1b-q4-tuned-r1" in compatible
    assert registry.latest_adapter("llama-3.2-1b-q4") == str(tmp_path)
    assert register({"id": "x-tuned-y", "path": str(tmp_path / "nope.gguf")}, catalogue, compatible) is False

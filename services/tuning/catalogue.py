"""Which original (trainable) weights belong to each GGUF catalogue model.

Fine-tuning cannot run on GGUF weights: they are quantised for inference and
have no gradients. Training needs the original fp16/bf16 safetensors the GGUF
was made from. This table maps each catalogue id in
``services/model_manager.py`` to that original Hugging Face repo.

``mirrors`` are repos that publish the *same* weights without a licence gate
(for example ``unsloth/Llama-3.2-1B-Instruct`` re-hosts Meta's files). They
are tried only when the official repo refuses the download (no token, or the
licence was not accepted); the licence of the original model still applies.

Models with no entry (or ``repo=""``) cannot be tuned: we do not know where
their original weights are, and we will not guess.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# LoRA target modules per architecture. Llama / Qwen / Mistral / Gemma use
# separate projections; Phi-3 fuses q/k/v and gate/up.
TARGETS_SPLIT = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")
TARGETS_PHI3 = ("qkv_proj", "o_proj", "gate_up_proj", "down_proj")


@dataclass(frozen=True)
class BaseModel:
    catalogue_id: str
    repo: str
    params_b: float                 # parameters, billions
    arch: str
    gated: bool = False
    mirrors: tuple[str, ...] = ()
    targets: tuple[str, ...] = TARGETS_SPLIT
    approx_gb: float = 0.0          # size of the bf16 safetensors download
    note: str = ""
    extra: dict = field(default_factory=dict)

    def view(self) -> dict:
        return {
            "catalogue_id": self.catalogue_id,
            "repo": self.repo,
            "mirrors": list(self.mirrors),
            "params_b": self.params_b,
            "arch": self.arch,
            "gated": self.gated,
            "download_gb": self.approx_gb or round(self.params_b * 2.0, 1),
            "targets": list(self.targets),
            "note": self.note,
        }


def _b(cid, repo, params, arch, **kw) -> tuple[str, BaseModel]:
    return cid, BaseModel(catalogue_id=cid, repo=repo, params_b=params, arch=arch, **kw)


BASES: dict[str, BaseModel] = dict([
    _b("llama-3.2-1b-q4", "meta-llama/Llama-3.2-1B-Instruct", 1.24, "llama", gated=True,
       mirrors=("unsloth/Llama-3.2-1B-Instruct",), approx_gb=2.5),
    _b("llama-3.2-3b-q4", "meta-llama/Llama-3.2-3B-Instruct", 3.21, "llama", gated=True,
       mirrors=("unsloth/Llama-3.2-3B-Instruct",), approx_gb=6.4),
    _b("gemma-2-2b-q4", "google/gemma-2-2b-it", 2.61, "gemma2", gated=True,
       mirrors=("unsloth/gemma-2-2b-it",), approx_gb=5.2),
    _b("qwen2-5-1.5b-q4", "Qwen/Qwen2.5-1.5B-Instruct", 1.54, "qwen2", approx_gb=3.1),
    _b("qwen2-5-3b-q4", "Qwen/Qwen2.5-3B-Instruct", 3.09, "qwen2", approx_gb=6.2),
    _b("phi-3-mini-q4", "microsoft/Phi-3-mini-4k-instruct", 3.82, "phi3",
       targets=TARGETS_PHI3, approx_gb=7.6),
    _b("phi-3.5-mini-q4", "microsoft/Phi-3.5-mini-instruct", 3.82, "phi3",
       targets=TARGETS_PHI3, approx_gb=7.6),
    _b("llama-3.1-8b-q4", "meta-llama/Llama-3.1-8B-Instruct", 8.03, "llama", gated=True,
       mirrors=("unsloth/Llama-3.1-8B-Instruct",), approx_gb=16.1),
    _b("qwen2-5-7b-q4", "Qwen/Qwen2.5-7B-Instruct", 7.62, "qwen2", approx_gb=15.2),
    _b("deepseek-coder-6.7b-q4", "deepseek-ai/deepseek-coder-6.7b-instruct", 6.74, "llama",
       approx_gb=13.5),
    _b("deepseek-r1-distill-llama-8b-q4", "deepseek-ai/DeepSeek-R1-Distill-Llama-8B", 8.03,
       "llama", approx_gb=16.1),
    _b("mistral-7b-v0.3-q4", "mistralai/Mistral-7B-Instruct-v0.3", 7.25, "llama", gated=True,
       approx_gb=14.5),
    _b("mistral-nemo-12b-q4", "mistralai/Mistral-Nemo-Instruct-2407", 12.2, "llama",
       gated=True, approx_gb=24.5),
    _b("qwen2-5-14b-q4", "Qwen/Qwen2.5-14B-Instruct", 14.8, "qwen2", approx_gb=29.5),
    _b("qwq-32b-q4", "Qwen/QwQ-32B-Preview", 32.8, "qwen2", approx_gb=65.5),
    _b("qwen2-5-32b-q4", "Qwen/Qwen2.5-32B-Instruct", 32.8, "qwen2", approx_gb=65.5),
    _b("deepseek-coder-33b-q4", "deepseek-ai/deepseek-coder-33b-instruct", 33.3, "llama",
       approx_gb=66.6),
    _b("mixtral-8x7b-q4", "mistralai/Mixtral-8x7B-Instruct-v0.1", 46.7, "llama", gated=True,
       approx_gb=93.4, note="MoE: LoRA on experts is not supported by this channel"),
    _b("llama-3.1-70b-q4", "meta-llama/Llama-3.1-70B-Instruct", 70.6, "llama", gated=True,
       approx_gb=141.0),
    _b("qwen2-5-72b-q4", "Qwen/Qwen2.5-72B-Instruct", 72.7, "qwen2", approx_gb=145.0),
    # The catalogue's "Llama 3.2 8B" is a community merge with no official
    # original repo; we do not know which weights it was built from.
    _b("llama-3.2-8b-q4", "", 8.0, "llama",
       note="community GGUF with no known original weights; cannot be tuned"),
])


def base_for(catalogue_id: str) -> BaseModel | None:
    """The trainable original for a catalogue id (tuned ids resolve to their parent)."""
    parent = parent_id(catalogue_id)
    base = BASES.get(parent)
    if base is None or not base.repo:
        return None
    return base


def parent_id(catalogue_id: str) -> str:
    """``llama-3.2-1b-q4-tuned-r123`` -> ``llama-3.2-1b-q4``."""
    marker = "-tuned-"
    if marker in catalogue_id:
        return catalogue_id.split(marker, 1)[0]
    return catalogue_id


def catalogue_id_for_filename(filename: str, catalogue: dict | None = None) -> str:
    """Map a GGUF file name (what the engine reports) back to a catalogue id."""
    if not filename:
        return ""
    if catalogue is None:
        try:
            from services.model_manager import RECOMMENDED_MODELS as catalogue
        except Exception:  # noqa: BLE001 - the catalogue is optional here
            catalogue = {}
    for model_id, info in catalogue.items():
        if getattr(info, "filename", None) == filename:
            return model_id
    return ""

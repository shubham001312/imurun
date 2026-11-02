"""Shared fixtures for the tuning tests: fixture transcripts and a fake backend."""
from __future__ import annotations

import json
from pathlib import Path

from services.sessions.transcript import TranscriptStore
from services.tuning.feasibility import Hardware
from services.tuning.feedback import FeedbackStore

LONG = ("Photosynthesis is the process plants use to turn light, water and carbon dioxide "
        "into glucose and oxygen. It happens in the chloroplasts, mostly in the leaves.")
LONG2 = ("A binary search halves the search interval each step: compare the middle element, "
         "then continue in the left or right half until the target is found or the range is empty.")
LONG3 = ("To reverse a list in Python use slicing with a negative step, items[::-1], which returns "
         "a new list, or call items.reverse() to reverse the list in place without copying it.")


def laptop() -> Hardware:
    """The owner's machine: RTX 3050 Laptop 4 GB, 16 GB RAM."""
    return Hardware(gpu_name="RTX 3050 Laptop", vram_total_gb=4.0, vram_free_gb=3.5,
                    ram_total_gb=16.0, ram_free_gb=8.0, commit_limit_gb=21.4, cpu_threads=16,
                    disk_free_gb=300.0)


def turn(store: TranscriptStore, session: str, turn_id: str, prompt: str, *answers: str) -> None:
    store.append(session, "user", prompt, turn_id)
    for answer in answers:
        store.append(session, "assistant", answer, turn_id)


def fixture_sessions(root: Path) -> tuple[TranscriptStore, FeedbackStore]:
    """Two sessions covering every filter. Returns (transcripts, feedback)."""
    sessions = root / "sessions"
    store = TranscriptStore(sessions)
    fb = FeedbackStore(sessions)
    turn(store, "s1", "t-good", "Explain photosynthesis briefly.", LONG)
    turn(store, "s1", "t-tool", "Read my todo file",
         '```tool\n{"name": "file_read", "args": {"path": "todo.md"}}\n```', LONG2)
    turn(store, "s1", "t-refuse", "Write something mean about my boss.",
         "I'm sorry, but I can't help with writing insults about real people, even in jest; "
         "maybe we could draft a constructive message instead?")
    turn(store, "s1", "t-short", "Capital of France?", "Paris.")
    turn(store, "s1", "t-teach", "Who founded ImuTechz?",
         "As of my knowledge cutoff I do not have specific information about this person or company.")
    turn(store, "s1", "t-pii", "Draft a reply to bob@example.com about binary search.", LONG2)
    turn(store, "s1", "t-live", "What is the weather today in Pune?", LONG3)
    turn(store, "s1", "t-disclaimer", "Who is the CEO of a small startup called Foo?",
         "As of my last update in 2023, I don't have information about that; my training data "
         "does not include every small company, so please check their website for details.")
    turn(store, "s1", "t-unrated", "Explain lists.", LONG3)
    turn(store, "s2", "t-dup", "Explain photosynthesis briefly.", LONG)
    turn(store, "s2", "t-good2", "/terse How does binary search work?", LONG2)
    for session, turn_id in (("s1", "t-good"), ("s1", "t-tool"), ("s1", "t-refuse"), ("s1", "t-short"),
                             ("s1", "t-pii"), ("s1", "t-live"), ("s1", "t-disclaimer"),
                             ("s2", "t-dup"), ("s2", "t-good2")):
        fb.record(session, turn_id, "up")
    fb.record("s1", "t-teach", "down", "ImuTechz was founded by its owner, the user of this app.")
    return store, fb


class FakeBackend:
    """Stands in for the tuning venv. Records calls; never trains."""

    def __init__(self, models_dir: Path, *, installed: bool = True, accepted: bool = True,
                 steps: int = 4, on_step=None) -> None:
        self.models_dir = Path(models_dir)
        self.installed = installed
        self.accepted = accepted
        self.steps = steps
        self.on_step = on_step
        self.calls: list[str] = []
        self.configs: list[dict] = []
        self.ticks: list[str] = []

    def stack(self) -> dict:
        return {"installed": self.installed, "message": "" if self.installed else "training stack not installed",
                "caps": {"cuda": True, "bf16": True, "bitsandbytes": True, "bnb_cpu": True}}

    def ensure_base(self, base, directory, token, progress, cancelled) -> dict:
        self.calls.append("ensure_base")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "model.safetensors").write_bytes(b"x")
        (directory / ".imurun-base.json").write_text(json.dumps({"repo": base.repo}))
        progress("downloaded", 100.0, None)
        return {"repo": base.repo}

    def train(self, config, run_dir, on_event, tick) -> dict:
        self.calls.append("train")
        self.configs.append(config)
        on_event({"event": "eval", "step": 0, "eval_loss": 2.0, "base": True})
        for step in range(1, self.steps + 1):
            if self.on_step:
                self.on_step(step)
            verdict = tick()
            self.ticks.append(verdict)
            if verdict == "cancel":
                return {"status": "cancelled"}
            on_event({"event": "progress", "step": step, "max_steps": self.steps,
                      "loss": 2.0 - step * 0.1, "eta_s": 10.0})
        on_event({"event": "phase", "phase": "evaluating", "message": "sanity"})
        (run_dir / "adapter").mkdir(exist_ok=True)
        sanity = {"The capital of France is": {"answer": "Paris" if self.accepted else "Lyon",
                                                "pass": self.accepted, "required": True}}
        return {"status": "ok" if self.accepted else "rejected", "accepted": self.accepted,
                "reason": "passed" if self.accepted else "sanity check failed",
                "base_eval_loss": 2.0, "eval_loss": 1.5, "tok_s": 500.0, "samples": 3, "steps": self.steps,
                "sanity_base": sanity, "sanity_tuned": sanity, "adapter_dir": str(run_dir / "adapter")}

    def export(self, run_dir, base_dir, adapter_dir, parent_filename, run_id, quant, progress) -> dict:
        self.calls.append("export")
        from services.tuning.export import tuned_filename

        self.models_dir.mkdir(parents=True, exist_ok=True)
        out = self.models_dir / tuned_filename(parent_filename, run_id, quant)
        out.write_bytes(b"GGUF")
        adapter = run_dir / "export" / f"adapter-{run_id}-f16.gguf"
        adapter.parent.mkdir(parents=True, exist_ok=True)
        adapter.write_bytes(b"GGUF")
        progress("done", 100.0)
        return {"gguf": str(out), "adapter_gguf": str(adapter), "quant": quant}

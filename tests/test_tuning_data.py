"""Dataset building: consent, quality filters, files, manifest dedupe, chat-template rendering."""
from __future__ import annotations

import json

import pytest

from services.tuning import data
from services.tuning.feedback import FeedbackStore
from tests.tuning_fakes import LONG, LONG2, fixture_sessions


def collect(tmp_path, manifest=None, include_pii=False):
    store, fb = fixture_sessions(tmp_path)
    return data.collect(feedback=fb, transcripts=store, datasets_dir=tmp_path / "datasets",
                        manifest=manifest, model_key="llama-3.2-1b-q4", include_pii=include_pii)


def test_only_consented_turns_survive_the_filters(tmp_path):
    kept, dropped, sources = collect(tmp_path)
    turns = sorted(s.turn_id for s in kept)
    assert turns == ["t-good", "t-good2", "t-teach"]
    assert dropped == {"tool_call": 1, "refusal": 1, "too_short": 1, "pii": 1, "live_data": 1,
                       "disclaimer": 1, "duplicate": 1}
    assert sources["feedback:up"] == 2 and sources["feedback:teach"] == 1
    teach = next(s for s in kept if s.turn_id == "t-teach")
    # The user's correction replaces the hallucinated, disclaimer-laden answer.
    assert teach.completion.startswith("ImuTechz was founded") and teach.source == "feedback:teach"
    good2 = next(s for s in kept if s.turn_id == "t-good2")
    assert good2.messages == [{"role": "user", "content": "How does binary search work?"}]  # /skill stripped


def test_pii_opt_in_keeps_the_email_turn(tmp_path):
    kept, dropped, _ = collect(tmp_path, include_pii=True)
    assert "t-pii" in {s.turn_id for s in kept} and "pii" not in dropped


def test_feedback_latest_wins_and_clear_withdraws_consent(tmp_path):
    fb = FeedbackStore(tmp_path)
    fb.record("s", "t1", "up")
    fb.record("s", "t2", "down")
    assert {k: v.consented for k, v in fb.for_session("s").items()} == {"t1": True, "t2": False}
    fb.record("s", "t1", "clear")
    assert "t1" not in fb.for_session("s")
    with pytest.raises(ValueError):
        fb.record("s", "t3", "teach")          # teach without a correction
    with pytest.raises(ValueError):
        fb.record("s", "t3", "meh")
    assert fb.record("s", "t4", "", "the right answer").rating == "teach"
    assert fb.counts() == {"up": 0, "down": 1, "teach": 1, "consented": 1}


def test_pii_patterns():
    assert data.find_pii("mail me at a.b@example.org") == ["email"]
    assert data.find_pii("call +1 415-555-0132 now") == ["phone"]
    assert "card" in data.find_pii("card 4111 1111 1111 1111")
    assert data.find_pii("token ghp_abcdefghijklmnopqrstuvwxyz0123") == ["key"]
    assert data.find_pii("api_key = abcd1234efgh5678") == ["secret"]
    assert data.find_pii("The year 2024 had 366 days and 12 months.") == []
    assert data.find_pii("version 1.2.3 of the tool") == []


def test_user_files_jsonl_csv_markdown(tmp_path):
    folder = tmp_path / "datasets"
    folder.mkdir()
    (folder / "a.jsonl").write_text("\n".join([
        json.dumps({"messages": [{"role": "user", "content": "What is a list?"},
                                 {"role": "assistant", "content": LONG2}]}),
        json.dumps({"prompt": "Explain plants", "completion": LONG}),
        json.dumps({"instruction": "Summarise", "input": "a long text", "output": LONG}),
        "not json",
    ]), encoding="utf-8")
    (folder / "b.csv").write_text("Question,Answer\nWhat is binary search?,\"" + LONG2 + "\"\n", encoding="utf-8")
    (folder / "c.md").write_text("Q: What is photosynthesis?\nA: " + LONG + "\n\n### User\nHi there\n"
                                 "### Assistant\n" + LONG2 + "\n", encoding="utf-8")
    samples, counts = data.from_files(folder)
    assert counts == {"a.jsonl": 3, "b.csv": 1, "c.md": 2}
    assert samples[2].messages[-1]["content"] == "Summarise\n\na long text"
    assert all(s.source.startswith("file:") for s in samples)
    listing = {f["name"]: f["supported"] for f in data.list_files(folder)}
    assert listing == {"a.jsonl": True, "b.csv": True, "c.md": True}


def test_manifest_dedupes_across_runs(tmp_path):
    manifest = data.Manifest(tmp_path / "manifest.json")
    kept, _, _ = collect(tmp_path, manifest=manifest)
    assert len(kept) == 3
    assert manifest.mark("llama-3.2-1b-q4", [s.hash for s in kept], "r1") == 3
    assert manifest.mark("llama-3.2-1b-q4", [kept[0].hash], "r2") == 0     # never twice
    again, dropped, _ = collect(tmp_path / "again", manifest=manifest)
    # 3 trained samples + the cross-session duplicate of one of them.
    assert again == [] and dropped["already_trained"] == 4 and "duplicate" not in dropped
    # A different base model has its own manifest entry.
    assert manifest.trained("qwen2-5-1.5b-q4") == set()
    assert manifest.view()["models"] == {"llama-3.2-1b-q4": 3}


def test_hash_is_whitespace_and_case_insensitive():
    a = data.sample_hash([{"role": "user", "content": "Hello  World"}], "Answer")
    b = data.sample_hash([{"role": "user", "content": "hello world "}], "answer")
    assert a == b


class FakeEngine:
    """Llama-3 style template, like fish_chat_prompt renders it."""

    def __init__(self):
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        out = "<|begin_of_text|>"
        for m in messages:
            out += f"<|start_header_id|>{m['role']}<|end_header_id|>\n\n{m['content']}<|eot_id|>"
        return out + "<|start_header_id|>assistant<|end_header_id|>\n\n"


def test_build_renders_with_the_engine_template_and_splits(tmp_path):
    samples = [data.Sample(messages=[{"role": "user", "content": f"question {i}"}],
                           completion=LONG + f" variant {i}", source="feedback:up") for i in range(30)]
    engine = FakeEngine()
    build = data.build(samples, out_root=tmp_path, model_key="m", renderer=engine)
    assert build.kept == 30 and build.eval >= 1 and build.train + build.eval == 30
    assert build.rendered_by == "fish" and engine.calls == 30
    rows = [json.loads(line) for line in (tmp_path / build.build_id / "train.jsonl").read_text().splitlines()]
    assert rows[0]["prompt_text"].endswith("<|start_header_id|>assistant<|end_header_id|>\n\n")
    assert rows[0]["rendered_by"] == "fish" and "completion" in rows[0]
    summary = json.loads((tmp_path / build.build_id / "summary.json").read_text())
    assert summary["kept"] == 30 and "hashes" not in summary
    # The split is a pure function of the hash: rebuilding gives the same eval set.
    again = data.build(samples, out_root=tmp_path / "2", model_key="m")
    assert again.eval == build.eval and again.rendered_by == "hf"


def test_build_falls_back_per_row_when_the_engine_fails(tmp_path):
    def broken(messages):
        raise RuntimeError("engine busy")

    samples = [data.Sample(messages=[{"role": "user", "content": "q"}], completion=LONG, source="x")]
    build = data.build(samples, out_root=tmp_path, model_key="m", renderer=broken)
    row = json.loads((tmp_path / build.build_id / "train.jsonl").read_text().splitlines()[0])
    assert row["rendered_by"] == "hf" and "prompt_text" not in row and build.train == 1

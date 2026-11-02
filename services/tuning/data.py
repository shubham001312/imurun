"""The data channel: turn this app's own usage into a training set.

Sources (and nothing else):

1. Chat turns the user explicitly consented to in the feedback channel
   (``feedback.py``): a thumbs-up keeps the model's answer, a "teach"
   correction replaces it with the user's text. Unrated turns are never used.
2. Files the user dropped into ``~/.imurun/tuning/datasets/``:
   ``.jsonl`` (``messages`` / ``prompt``+``completion`` / ``instruction``+
   ``input``+``output`` / ``question``+``answer``), ``.csv`` (the same column
   names) and ``.md`` (``Q:`` / ``A:`` pairs, or ``### User`` / ``### Assistant``
   blocks).

Automatic quality filters, each with a reason code that is counted in the
build summary:

``tool_call``     the turn called a tool (its answer depends on tool output
                  the model will not have at training time)
``live_data``     the prompt asks for live / time-sensitive facts (news,
                  weather, prices, "today", "latest" ...). Baking those into
                  weights makes the model confidently stale.
``refusal``       the answer is a refusal
``disclaimer``    the answer leans on a knowledge-cutoff / "I don't have
                  real-time access" disclaimer (the exact failure that started
                  this feature: do not reinforce it)
``too_short``     completion under 20 tokens (4 for user-written corrections)
``pii``           e-mail addresses, phone numbers, card numbers, API keys /
                  tokens (unless ``tuning.include_pii`` is on)
``duplicate``     the same prompt+completion twice in this build
``already_trained`` the manifest says this sample already trained this model
``empty``         nothing usable (missing prompt or answer)

User corrections are exempt from ``refusal`` and ``disclaimer`` (the user
wrote them) but not from the others.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .feedback import FeedbackStore

MIN_TOKENS = 20
MIN_TOKENS_CORRECTION = 4
MAX_SAMPLE_CHARS = 16_000
EVAL_FRACTION = 0.10

Renderer = Callable[[list[dict]], str]

# --------------------------------------------------------------- filters

_TOOL_RE = re.compile(r"```tool\b|\{\s*\"name\"\s*:\s*\"[a-z_]+\"\s*,\s*\"args\"\s*:", re.I)
_LIVE_RE = re.compile(
    r"\b(today|tonight|right now|at the moment|currently|latest|this (week|month|year)|"
    r"current (president|prime minister|ceo|leader|price|weather|news|events|time|date|champion|status|version)|"
    r"yesterday|tomorrow|breaking|news|headlines?|weather|forecast|temperature outside|"
    r"stock price|share price|price of|exchange rate|bitcoin|crypto|live scores?|standings|"
    r"election results?|trending|who won)\b",
    re.I,
)
_REFUSAL_RE = re.compile(
    r"^\s*(i'?m sorry|i am sorry|sorry, (but )?i|i cannot|i can'?t|i can not|i won'?t|"
    r"as an ai|i'?m unable|i am unable|i'?m not able|i am not able|i must decline)",
    re.I,
)
_DISCLAIMER_RE = re.compile(
    r"knowledge cut-?off|as of my (last|latest) (update|training|knowledge)|my training data|"
    r"i (do not|don'?t) have (real-time|access to real-time|current|up-to-date)|"
    r"i (do not|don'?t) have (any )?(specific )?information (about|on) (this|that|a) person|"
    r"as of my knowledge",
    re.I,
)
_PII_PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<![\w.])(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3,4}[\s.-]\d{3,4}(?:[\s.-]\d{2,4})?(?![\w.])"),
    "card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "key": re.compile(
        r"\b(sk-[A-Za-z0-9_-]{16,}|sk_live_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|hf_[A-Za-z0-9]{20,}|"
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"
    ),
    "secret": re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|bearer)\b\s*[:=]\s*\S{8,}"),
}
_SKILL_PREFIX = re.compile(r"^/[a-z0-9][a-z0-9-]*\s+", re.I)


def estimate_tokens(text: str) -> int:
    """Rough token count (no tokenizer in the app venv): max(chars/4, words)."""
    text = text or ""
    return max(len(text) // 4, len(text.split()))


def find_pii(text: str) -> list[str]:
    found = []
    for name, pattern in _PII_PATTERNS.items():
        for match in pattern.finditer(text or ""):
            value = match.group(0)
            if name in ("phone", "card"):
                digits = re.sub(r"\D", "", value)
                if name == "phone" and not (9 <= len(digits) <= 15):
                    continue
                if name == "card" and not _luhn(digits):
                    continue
            found.append(name)
            break
    return found


def _luhn(digits: str) -> bool:
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass
class Sample:
    messages: list[dict]            # the prompt side: [{"role","content"}, ...] ending with user
    completion: str
    source: str                     # feedback:up | feedback:teach | file:<name>
    session_id: str = ""
    turn_id: str = ""
    tool_used: bool = False
    hash: str = ""

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = sample_hash(self.messages, self.completion)

    @property
    def correction(self) -> bool:
        return self.source == "feedback:teach"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


def sample_hash(messages: list[dict], completion: str) -> str:
    h = hashlib.sha256()
    for message in messages:
        h.update(str(message.get("role", "")).encode())
        h.update(b"\x00")
        h.update(_norm(str(message.get("content", ""))).encode("utf-8"))
        h.update(b"\x01")
    h.update(_norm(completion).encode("utf-8"))
    return h.hexdigest()


def check(sample: Sample, *, include_pii: bool = False) -> str:
    """'' if the sample is usable, else the reason code."""
    prompt = " ".join(str(m.get("content", "")) for m in sample.messages if m.get("role") == "user")
    answer = sample.completion or ""
    if not prompt.strip() or not answer.strip():
        return "empty"
    if sample.tool_used or _TOOL_RE.search(answer) or _TOOL_RE.search(prompt):
        return "tool_call"
    if _LIVE_RE.search(prompt):
        return "live_data"
    if not sample.correction:
        if _REFUSAL_RE.search(answer):
            return "refusal"
        if _DISCLAIMER_RE.search(answer):
            return "disclaimer"
    minimum = MIN_TOKENS_CORRECTION if sample.correction else MIN_TOKENS
    if estimate_tokens(answer) < minimum:
        return "too_short"
    if len(prompt) + len(answer) > MAX_SAMPLE_CHARS:
        return "too_long"
    if not include_pii and (find_pii(prompt) or find_pii(answer)):
        return "pii"
    return ""


# --------------------------------------------------------------- manifest

class Manifest:
    """Hashes of samples that already trained a model (per base model id).

    A sample is recorded only when its run is *accepted* (state ``done``), so
    a failed or rejected run never burns data.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {"models": {}, "runs": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"models": {}, "runs": []}
        data.setdefault("models", {})
        data.setdefault("runs", [])
        return data

    def trained(self, model_key: str) -> set[str]:
        return set(self._load()["models"].get(model_key, {}))

    def has(self, model_key: str, sample: str) -> bool:
        return sample in self.trained(model_key)

    def mark(self, model_key: str, hashes: Iterable[str], run_id: str) -> int:
        with self._lock:
            data = self._load()
            table = data["models"].setdefault(model_key, {})
            added = 0
            for value in hashes:
                if value not in table:
                    table[value] = run_id
                    added += 1
            data["runs"].append({"run_id": run_id, "model": model_key, "samples": added,
                                 "at": time.time()})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            return added

    def view(self) -> dict:
        data = self._load()
        return {"models": {k: len(v) for k, v in data["models"].items()}, "runs": data["runs"][-20:]}


# --------------------------------------------------------------- collection

def _strip_skill(text: str) -> str:
    return _SKILL_PREFIX.sub("", text, count=1).strip()


def turns_from_transcript(messages: Iterable) -> dict[str, dict]:
    """Group transcript messages by turn id: first user prompt, final answer."""
    turns: dict[str, dict] = {}
    for message in messages:
        turn_id = getattr(message, "turn_id", "") or ""
        if not turn_id:
            continue
        role = getattr(message, "role", "")
        content = getattr(message, "content", "")
        entry = turns.setdefault(turn_id, {"user": "", "answers": [], "users": 0})
        if role == "user":
            entry["users"] += 1
            if not entry["user"]:
                entry["user"] = content
        elif role == "assistant":
            entry["answers"].append(content)
    return turns


def from_feedback(feedback: FeedbackStore, transcripts) -> tuple[list[Sample], dict]:
    """Samples for every consented turn. ``transcripts`` is a TranscriptStore."""
    samples: list[Sample] = []
    dropped: dict[str, int] = {}
    for session_id in feedback.sessions():
        marks = feedback.for_session(session_id)
        consented = {t: f for t, f in marks.items() if f.consented}
        if not consented:
            continue
        try:
            history = transcripts.read(session_id, limit=1000)
        except (ValueError, OSError):
            history = []
        turns = turns_from_transcript(history)
        for turn_id, mark in consented.items():
            turn = turns.get(turn_id)
            if not turn or not turn["user"] or not turn["answers"]:
                dropped["missing_turn"] = dropped.get("missing_turn", 0) + 1
                continue
            # More than one assistant message (or extra user messages carrying
            # tool results) means the loop called tools in this turn.
            tool_used = len(turn["answers"]) > 1 or turn["users"] > 1 or any(
                _TOOL_RE.search(a) for a in turn["answers"])
            correction = bool(mark.correction)
            samples.append(Sample(
                messages=[{"role": "user", "content": _strip_skill(turn["user"])}],
                completion=mark.correction if correction else turn["answers"][-1],
                source="feedback:teach" if correction else "feedback:up",
                session_id=session_id, turn_id=turn_id,
                # A correction is the user's own answer: it does not depend on
                # the tool output, so it is not dropped for the tool call.
                tool_used=tool_used and not correction,
            ))
    return samples, dropped


def _row_to_sample(row: dict, source: str) -> Sample | None:
    if not isinstance(row, dict):
        return None
    if isinstance(row.get("messages"), list):
        msgs = [{"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
                for m in row["messages"] if isinstance(m, dict)]
        if len(msgs) >= 2 and msgs[-1]["role"] == "assistant":
            return Sample(messages=msgs[:-1], completion=msgs[-1]["content"], source=source)
        return None
    pairs = (("prompt", "completion"), ("question", "answer"), ("instruction", "output"),
             ("input", "output"), ("user", "assistant"))
    for p_key, c_key in pairs:
        if row.get(p_key) and row.get(c_key):
            prompt = str(row[p_key])
            if p_key == "instruction" and row.get("input"):
                prompt = f"{prompt}\n\n{row['input']}"
            messages = []
            if row.get("system"):
                messages.append({"role": "system", "content": str(row["system"])})
            messages.append({"role": "user", "content": prompt})
            return Sample(messages=messages, completion=str(row[c_key]), source=source)
    return None


_MD_QA = re.compile(r"^\s*(?:\*\*)?(Q|A|User|Assistant)(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*", re.I)
_MD_HEAD = re.compile(r"^\s*#{2,4}\s*(User|Assistant|Question|Answer)\s*$", re.I)


def parse_markdown(text: str) -> list[tuple[str, str]]:
    """``Q:`` / ``A:`` pairs or ``### User`` / ``### Assistant`` blocks."""
    pairs: list[tuple[str, str]] = []
    role, buffer, question = None, [], None

    def flush():
        nonlocal question
        content = "\n".join(buffer).strip()
        if role == "q":
            question = content
        elif role == "a" and question:
            pairs.append((question, content))
            question = None

    for line in text.splitlines():
        head = _MD_HEAD.match(line)
        qa = _MD_QA.match(line)
        if head or qa:
            flush()
            label = (head or qa).group(1).lower()
            role = "q" if label in ("q", "user", "question") else "a"
            buffer = [] if head else [line[qa.end():]]
            continue
        if role is not None:
            buffer.append(line)
    flush()
    return [(q, a) for q, a in pairs if q and a]


def from_files(directory: Path) -> tuple[list[Sample], dict]:
    samples: list[Sample] = []
    files: dict[str, int] = {}
    if not directory.is_dir():
        return samples, files
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
            continue
        source = f"file:{path.name}"
        found: list[Sample] = []
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        suffix = path.suffix.lower()
        if suffix in (".jsonl", ".json"):
            rows = []
            if suffix == ".json":
                try:
                    loaded = json.loads(text)
                    rows = loaded if isinstance(loaded, list) else []
                except ValueError:
                    rows = []
            else:
                for line in text.splitlines():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
            found = [s for s in (_row_to_sample(r, source) for r in rows) if s]
        elif suffix == ".csv":
            reader = csv.DictReader(io.StringIO(text))
            found = [s for s in (_row_to_sample({k.strip().lower(): v for k, v in r.items() if k}, source)
                                 for r in reader) if s]
        elif suffix in (".md", ".markdown", ".txt"):
            found = [Sample(messages=[{"role": "user", "content": q}], completion=a, source=source)
                     for q, a in parse_markdown(text)]
        files[path.name] = len(found)
        samples.extend(found)
    return samples, files


def list_files(directory: Path) -> list[dict]:
    out = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.iterdir()):
        if path.is_file():
            out.append({"name": path.name, "bytes": path.stat().st_size,
                        "modified": path.stat().st_mtime,
                        "supported": path.suffix.lower() in (".jsonl", ".json", ".csv", ".md",
                                                             ".markdown", ".txt")})
    return out


# --------------------------------------------------------------- build

@dataclass
class Build:
    build_id: str
    model_key: str
    directory: str = ""
    train: int = 0
    eval: int = 0
    tokens: int = 0
    rendered_by: str = ""
    dropped: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    hashes: list = field(default_factory=list)
    created_at: float = 0.0

    @property
    def kept(self) -> int:
        return self.train + self.eval

    def view(self) -> dict:
        data = asdict(self)
        data.pop("hashes", None)
        data["kept"] = self.kept
        return data


def is_eval(sample_hash_value: str, fraction: float = EVAL_FRACTION) -> bool:
    bucket = int(sample_hash_value[:8], 16) / 0xFFFFFFFF
    return bucket < fraction


def collect(
    *,
    feedback: FeedbackStore,
    transcripts,
    datasets_dir: Path,
    manifest: Manifest | None,
    model_key: str,
    include_pii: bool = False,
) -> tuple[list[Sample], dict, dict]:
    """All usable samples (filtered + deduped), the drop counts, and source counts."""
    chat, dropped = from_feedback(feedback, transcripts)
    files, file_counts = from_files(datasets_dir)
    trained = manifest.trained(model_key) if manifest is not None else set()
    seen: set[str] = set()
    kept: list[Sample] = []
    for sample in chat + files:
        reason = check(sample, include_pii=include_pii)
        if not reason and sample.hash in seen:
            reason = "duplicate"
        if not reason and sample.hash in trained:
            reason = "already_trained"
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        seen.add(sample.hash)
        kept.append(sample)
    sources = {"feedback:up": 0, "feedback:teach": 0}
    for sample in kept:
        key = sample.source if sample.source.startswith("feedback") else "files"
        sources[key] = sources.get(key, 0) + 1
    sources["files_found"] = file_counts
    return kept, dropped, sources


def build(
    samples: list[Sample],
    *,
    out_root: Path,
    model_key: str,
    renderer: Renderer | None = None,
    dropped: dict | None = None,
    sources: dict | None = None,
    eval_fraction: float = EVAL_FRACTION,
) -> Build:
    """Write ``train.jsonl`` / ``eval.jsonl`` for the trainer.

    Each row carries ``messages`` + ``completion``; when ``renderer`` (the
    engine's own chat template, ``fish_chat_prompt``) is available the row also
    carries ``prompt_text`` and ``rendered_by: "fish"`` so the model trains on
    exactly the text it sees at inference. Otherwise the trainer renders with
    the Hugging Face tokenizer's template (``rendered_by: "hf"``).
    """
    build_id = time.strftime("b%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    directory = Path(out_root) / build_id
    directory.mkdir(parents=True, exist_ok=True)
    result = Build(build_id=build_id, model_key=model_key, directory=str(directory),
                   dropped=dict(dropped or {}), sources=dict(sources or {}), created_at=time.time())
    rendered_by = "hf"
    rows_train, rows_eval = [], []
    ordered = sorted(samples, key=lambda s: s.hash)
    force_eval = len(ordered) >= 5 and not any(is_eval(s.hash, eval_fraction) for s in ordered)
    for index, sample in enumerate(ordered):
        row = {"hash": sample.hash, "source": sample.source, "messages": sample.messages,
               "completion": sample.completion}
        if renderer is not None:
            try:
                row["prompt_text"] = renderer(sample.messages)
                row["rendered_by"] = "fish"
                rendered_by = "fish"
            except Exception:  # noqa: BLE001 - fall back to the HF template per row
                row["rendered_by"] = "hf"
        else:
            row["rendered_by"] = "hf"
        result.tokens += estimate_tokens(row.get("prompt_text") or sample.messages[-1]["content"])
        result.tokens += estimate_tokens(sample.completion)
        target = rows_eval if (is_eval(sample.hash, eval_fraction) or (force_eval and index == 0)) else rows_train
        target.append(row)
        result.hashes.append(sample.hash)
    if not rows_train and rows_eval:
        rows_train.append(rows_eval.pop())
    _write_jsonl(directory / "train.jsonl", rows_train)
    _write_jsonl(directory / "eval.jsonl", rows_eval)
    result.train, result.eval, result.rendered_by = len(rows_train), len(rows_eval), rendered_by
    (directory / "summary.json").write_text(json.dumps(result.view(), indent=2), encoding="utf-8")
    return result


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

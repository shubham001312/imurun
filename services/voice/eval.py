"""Speech-to-text evaluation harness (WER/CER per language, latency, deltas).

    python -m services.voice.eval run --data DIR [--sets librispeech-en,fleurs-hi]
            [--model small] [--device cpu|cuda|auto] [--beam 5] [--threads 2]
            [--prompt none|vocab|"literal text"] [--cleanup] [--force-lang]
            [--label "step 2: turbo on GPU"] [--out results.jsonl] [--details rows.jsonl]
    python -m services.voice.eval report results.jsonl [--markdown]
    python -m services.voice.eval synth --data DIR --set terms|disfluent|hinglish|noise
    python -m services.voice.eval synth --data DIR --set NAME --from texts.jsonl

A set is a directory with ``manifest.jsonl``: one object per clip,
``{"audio": "file.flac", "text": "reference", "language": "en", "spoken": "..."}``
(``spoken`` only for synthesised sets, where what is said differs from what
should be typed - fillers, corrections, spoken names). Audio is any format
``audio.decode_16k`` reads.

The run uses the product's own ``engines.WhisperSTT`` (same decoding
options, VAD, guards, Bengali re-scripting) in this process, so a number here
is a number the server produces. Scores ignore case, punctuation and number
spelling (``cleanup.normalize_for_score``). ``garbage`` = clips with CER >=
0.9; ``empty`` = no text. Latency is the wall time of one transcription
(decode + VAD + Whisper) with the model already loaded.

Downloaded sets are untrusted data: this module only reads them as audio and
JSON. Results go where ``--out`` says, never into the repository.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

from .cleanup import clean, normalize_for_score

CANDIDATES = ["en", "hi", "bn", "de"]


# ------------------------------------------------------------------ scoring

def _distance(a: list, b: list) -> int:
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def score(reference: str, hypothesis: str, language: str) -> dict:
    ref, hyp = normalize_for_score(reference, language), normalize_for_score(hypothesis, language)
    ref_c, hyp_c = list("".join(ref)), list("".join(hyp))
    return {"wer_err": _distance(ref, hyp), "words": len(ref),
            "cer_err": _distance(ref_c, hyp_c), "chars": len(ref_c),
            "cer": _distance(ref_c, hyp_c) / max(1, len(ref_c))}


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] if lo == hi else ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def load_set(directory: Path) -> list[dict]:
    rows = []
    manifest = directory / "manifest.jsonl"
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if not isinstance(item, dict) or not isinstance(item.get("audio"), str):
            continue
        path = (directory / Path(item["audio"]).name)   # never leave the set's directory
        if path.is_file():
            rows.append({**item, "path": path})
    return rows


# ------------------------------------------------------------------ run

def run(args) -> int:
    from . import engines
    from .audio import decode_16k
    from .paths import VoicePaths
    from .vocabulary import build_prompt

    data = Path(args.data)
    names = [s for s in (args.sets.split(",") if args.sets else sorted(p.name for p in data.iterdir()
                                                                         if (p / "manifest.jsonl").is_file()))]
    paths = VoicePaths.at(args.home)
    model_dir = str(paths.whisper_dir(args.model))
    if args.legacy:
        # The engine as it was before 2026-11-02 (the baseline row): faster-
        # whisper's default VAD parameters, no silence/hallucination guards.
        engines.VAD.clear()
        engines.SILENT_RMS = -1.0
        engines.WhisperSTT.drop_hallucinations = staticmethod(lambda items, speech: (items, 0))
    stt = engines.WhisperSTT()
    from . import gpu

    vram_before = gpu.memory() if args.device != "cpu" else {}
    load_s = stt.load(model_dir, args.threads, args.device)
    vram_loaded = gpu.memory() if stt.device == "cuda" else {}
    summary = {"label": args.label, "at": time.strftime("%Y-%m-%d %H:%M"), "model": args.model,
               "device": stt.device, "compute_type": stt.compute_type, "device_reason": stt.device_reason,
               "beam": args.beam, "threads": args.threads, "prompt": args.prompt, "cleanup": bool(args.cleanup),
               "force_lang": bool(args.force_lang), "legacy": bool(args.legacy), "load_s": round(load_s, 2),
               "vram_free_before_mb": vram_before.get("free_mb"),
               "vram_model_mb": (vram_before["free_mb"] - vram_loaded["free_mb"]) if vram_loaded and vram_before else None,
               "sets": {}}
    details = open(args.details, "a", encoding="utf-8") if args.details else None
    vocab = [w for w in (args.vocab or "").split(",") if w.strip()]
    try:
        for name in names:
            rows = load_set(data / name)[: args.limit or None]
            if not rows:
                continue
            totals = {"wer_err": 0, "words": 0, "cer_err": 0, "chars": 0}
            totals_clean = dict(totals)
            lat, audio_s, empty, garbage, lang_ok, hallucinated = [], [], 0, 0, 0, 0
            devices = set()
            for index, row in enumerate(rows):
                language = row.get("language") or ""
                samples = decode_16k(row["path"].read_bytes(), "")
                if args.prompt == "none":
                    prompt = ""
                elif args.prompt == "vocab":
                    prompt = build_prompt(language=language if args.force_lang else "", vocabulary=vocab,
                                          context=row.get("context") or [])
                else:
                    prompt = args.prompt
                kwargs = dict(model_dir=model_dir, threads=args.threads,
                              language=language if args.force_lang else "", beam_size=args.beam, vad=True,
                              candidates=[] if args.force_lang else CANDIDATES, device=args.device,
                              prompt=prompt, samples=samples)
                start = time.perf_counter()
                result = stt.transcribe(b"", "", **kwargs)
                elapsed = time.perf_counter() - start
                devices.add(result.get("device"))
                if index == 0 and args.warmup:
                    # The first call on a fresh CUDA context includes kernel
                    # set-up; time it separately, decode the clip again.
                    summary.setdefault("first_call_s", {})[name] = round(elapsed, 2)
                    start = time.perf_counter()
                    result = stt.transcribe(b"", "", **kwargs)
                    elapsed = time.perf_counter() - start
                text = result.get("text", "")
                reference = row.get("text", "")
                if not reference.strip():
                    # Noise / silence sets: anything written is a hallucination.
                    hallucinated += 1 if text.strip() else 0
                    lat.append(elapsed)
                    audio_s.append(float(result.get("duration") or 0.0))
                    if details:
                        details.write(json.dumps({"label": args.label, "set": name, "audio": row["audio"],
                                                  "hyp": text, "s": round(elapsed, 3)}, ensure_ascii=False) + "\n")
                    continue
                s = score(reference, text, language)
                for key in totals:
                    totals[key] += s[key]
                cleaned_text = None
                if args.cleanup:
                    cleaned_text = clean(text, result.get("language") or language).text
                    c = score(reference, cleaned_text, language)
                    for key in totals_clean:
                        totals_clean[key] += c[key]
                empty += 0 if text.strip() else 1
                # Garbage = unrelated to what was SAID (for synthesised sets the
                # spoken form; a short clean target makes CER > 1 by design).
                said = score(row.get("spoken") or reference, text, language)["cer"]
                garbage += 1 if said >= 0.9 else 0
                lang_ok += 1 if (result.get("language") or "") == language else 0
                lat.append(elapsed)
                audio_s.append(float(result.get("duration") or 0.0))
                if details:
                    details.write(json.dumps({"label": args.label, "set": name, "audio": row["audio"],
                                              "lang": result.get("language"), "hyp": text, "clean": cleaned_text,
                                              "cer": round(s["cer"], 3), "s": round(elapsed, 3),
                                              "script_fixed": result.get("script_fixed")},
                                             ensure_ascii=False) + "\n")
                print(f"  {name} {index + 1}/{len(rows)} cer={s['cer']:.2f} {elapsed:.2f}s", file=sys.stderr)
            entry = {"n": len(rows), "lat_p50": round(percentile(lat, 0.5), 3),
                     "lat_p95": round(percentile(lat, 0.95), 3),
                     "audio_mean_s": round(statistics.mean(audio_s), 2) if audio_s else 0.0,
                     "devices": sorted(d for d in devices if d)}
            if totals["words"]:
                entry.update({"wer": round(totals["wer_err"] / totals["words"], 4),
                              "cer": round(totals["cer_err"] / max(1, totals["chars"]), 4),
                              "empty": empty, "garbage": garbage, "lang_ok": lang_ok})
                if args.cleanup:
                    entry.update({"wer_clean": round(totals_clean["wer_err"] / totals_clean["words"], 4),
                                  "cer_clean": round(totals_clean["cer_err"] / max(1, totals_clean["chars"]), 4)})
            else:
                entry["hallucinated"] = hallucinated
            summary["sets"][name] = entry
            print(json.dumps({name: entry}), file=sys.stderr)
    finally:
        if details:
            details.close()
    line = json.dumps(summary, ensure_ascii=False)
    print(line)
    if args.out:
        with open(args.out, "a", encoding="utf-8") as out:
            out.write(line + "\n")
    return 0


# ------------------------------------------------------------------ report

def report(args) -> int:
    runs = [json.loads(line) for line in Path(args.file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.markdown:
        print("| Run | Model / device | Set | n | WER | CER | clean WER | empty/garbage | p50 s | p95 s | delta WER |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
    first: dict[str, float] = {}
    for item in runs:
        for name, entry in item["sets"].items():
            metric = entry.get("wer")
            delta = ""
            if metric is not None:
                if name in first:
                    delta = f"{metric - first[name]:+.3f}"
                else:
                    first[name] = metric
            if args.markdown:
                if metric is None:
                    quality = f"hallucinated {entry.get('hallucinated')}/{entry['n']}"
                    cols = ["-", "-", "-", quality]
                else:
                    cols = [f"{entry['wer']:.3f}", f"{entry['cer']:.3f}",
                            f"{entry['wer_clean']:.3f}" if "wer_clean" in entry else "-",
                            f"{entry['empty']}/{entry['garbage']}"]
                print(f"| {item.get('label') or ''} | {item['model']} {item['device']} {item.get('compute_type', '')}"
                      f" beam {item['beam']} | {name} | {entry['n']} | " + " | ".join(cols)
                      + f" | {entry['lat_p50']:.2f} | {entry['lat_p95']:.2f} | {delta} |")
            else:
                print(json.dumps({"label": item.get("label"), "set": name, **entry, "delta_wer": delta}))
    return 0


# ------------------------------------------------------------------ synthesis

# Built-in synthesised sets. ``spoken`` is what the voice says (how a person
# pronounces the name), ``text`` is what should be typed. Written for this
# harness; no user data.
TERMS = [
    ("Load the Phi-3 model in FISH and run it on Vulkan.", "Load the Phi three model in fish and run it on Vulcan."),
    ("Can IMURUN fine-tune a LoRA adapter on my laptop?", "Can Immu run fine tune a Lora adapter on my laptop?"),
    ("Convert the Qwen checkpoint to GGUF before you test it.", "Convert the Kwen checkpoint to G G U F before you test it."),
    ("Compare DeepSeek and Llama on the same prompt.", "Compare deep seek and llama on the same prompt."),
    ("Send the ImuTechz invoice through Gmail tomorrow morning.", "Send the Immu techs invoice through G mail tomorrow morning."),
    ("Push the fix to GitHub and deploy it on Vercel.", "Push the fix to git hub and deploy it on ver sell."),
    ("Post the build status in Slack after the Render deploy finishes.", "Post the build status in Slack after the render deploy finishes."),
    ("Store the user table in Supabase and put Cloudflare in front of it.", "Store the user table in Supa base and put cloud flare in front of it."),
    ("Make a Canva poster for the IMURUN launch.", "Make a canva poster for the Immu run launch."),
    ("Why is Phi-3 slower than Qwen on the GPU?", "Why is Phi three slower than Kwen on the G P U?"),
    ("Quantise Llama to a smaller GGUF and keep the LoRA weights.", "Quantise llama to a smaller G G U F and keep the Lora weights."),
    ("Open FISH settings and turn on Vulkan offload.", "Open fish settings and turn on Vulcan offload."),
    ("DeepSeek writes better code than Phi-3 for me.", "Deep seek writes better code than Phi three for me."),
    ("Ask IMURUN to summarise my Gmail inbox.", "Ask Immu run to summarise my G mail inbox."),
    ("Download the Qwen GGUF from Hugging Face.", "Download the Kwen G G U F from Hugging Face."),
]
DISFLUENT = [
    ("So I want to send the report on Wednesday.", "Um, so I want to, uh, send the report on Tuesday, no wait, Wednesday."),
    ("Can you check the logs for me?", "Can you, uh, can you check the the logs for me?"),
    ("Set the timer for fifteen minutes.", "Set the timer for ten minutes, I mean, fifteen minutes."),
    ("I think the model is pretty fast.", "Um, you know, I think the model is, uh, pretty fast."),
    ("Book a table for six people.", "Book a table for four people, actually make that six people."),
    ("The meeting is at four pm.", "The meeting is at three, no sorry, at four pm."),
    ("Please write a short email to Rahul, thanking him for the help.",
     "Please write a short email to Rahul comma thanking him for the help full stop"),
    ("What is the capital of France?", "What is the, uh, what is the capital of France question mark"),
    ("I need three copies of this file.", "I need, um, three copies of this file."),
    ("Open the folder.", "Open the file, uh, sorry, open the folder."),
    ("Move the call to Friday.", "Move the call to Thursday, no, Friday."),
    ("Add milk, eggs and bread to the list.", "Add milk, eggs and, uh, bread to the list."),
]
# Code-switched Hindi-English ("Hinglish"), written the way a Hindi speaker
# says the English words (spoken by the Hindi Piper voice). ``text`` is the
# Devanagari reference, ``latin`` the romanised form many users type.
HINGLISH = [
    ("मुझे कल की मीटिंग का एजेंडा भेज दो", "Mujhe kal ki meeting ka agenda bhej do"),
    ("यह फाइल डाउनलोड क्यों नहीं हो रही है", "Yeh file download kyon nahin ho rahi hai"),
    ("प्लीज़ इस ईमेल का रिप्लाई लिख दो", "Please is email ka reply likh do"),
    ("मेरा लैपटॉप बहुत स्लो चल रहा है", "Mera laptop bahut slow chal raha hai"),
    ("आज शाम को प्रोजेक्ट का स्टेटस अपडेट करना है", "Aaj shaam ko project ka status update karna hai"),
    ("इस कोड में एक बग है उसे फिक्स करो", "Is code mein ek bug hai use fix karo"),
    ("मॉडल को जीपीयू पर लोड करो", "Model ko GPU par load karo"),
    ("कल सुबह दस बजे कॉल शेड्यूल कर दो", "Kal subah das baje call schedule kar do"),
]


def synth(args) -> int:
    import numpy as np

    from .audio import wav_bytes
    from .catalogue import DEFAULT_VOICE
    from .engines import PiperTTS
    from .paths import VoicePaths

    out_dir = Path(args.data) / (args.name or args.set)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if args.set == "noise":
        rng = np.random.default_rng(7)
        rate = 16000
        n = rate * 3
        t = np.arange(n) / rate

        def pink(size):
            white = rng.standard_normal(size)
            spectrum = np.fft.rfft(white)
            spectrum /= np.sqrt(np.arange(1, spectrum.size + 1))
            out = np.fft.irfft(spectrum, size)
            return out / np.max(np.abs(out))

        clips = {
            "silence": np.zeros(n),
            "white-quiet": rng.standard_normal(n) * 0.003,
            "white-loud": rng.standard_normal(n) * 0.03,
            "pink-room": pink(n) * 0.02,
            "brown-hvac": np.cumsum(rng.standard_normal(n)) / 4000.0,
            "hum-50hz": 0.02 * np.sin(2 * np.pi * 50 * t) + 0.01 * np.sin(2 * np.pi * 150 * t) + pink(n) * 0.003,
            "keyboard": pink(n) * 0.002 + np.where(rng.random(n) < 0.0015, rng.standard_normal(n) * 0.3, 0.0),
            "fan-pink-loud": pink(n) * 0.08,
        }
        for key, signal in clips.items():
            signal = np.clip(signal - np.mean(signal), -1, 1)
            pcm = (signal * 32767).astype("<i2").tobytes()
            (out_dir / f"{key}.wav").write_bytes(wav_bytes(pcm, rate))
            rows.append({"audio": f"{key}.wav", "text": "", "language": ""})
    else:
        paths = VoicePaths.at(args.home)
        tts = PiperTTS(str(paths.tmp))
        if args.from_jsonl:
            items = [json.loads(line) for line in Path(args.from_jsonl).read_text(encoding="utf-8").splitlines()
                     if line.strip()]
            items = [(i["text"], i.get("spoken") or i["text"], i.get("language") or "en") for i in items]
        elif args.set == "terms":
            items = [(text, spoken, "en") for text, spoken in TERMS]
        elif args.set == "disfluent":
            items = [(text, spoken, "en") for text, spoken in DISFLUENT]
        elif args.set == "hinglish":
            items = [(deva, deva, "hi") for deva, _ in HINGLISH]
        else:
            raise SystemExit(f"unknown set {args.set!r}")
        latin = dict(HINGLISH)
        for index, (text, spoken, lang) in enumerate(items):
            key = args.voice or DEFAULT_VOICE[lang]
            audio, meta = tts.speak(spoken, model=str(paths.piper_model(key)), config=str(paths.piper_config(key)),
                                    threads=2)
            name = f"{args.set}-{index:03d}.wav"
            (out_dir / name).write_bytes(audio)
            row = {"audio": name, "text": text, "language": lang, "spoken": spoken, "voice": key}
            if text in latin:
                row["latin"] = latin[text]
            if args.context:
                row["context"] = [args.context]
            rows.append(row)
    with open(out_dir / "manifest.jsonl", "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{out_dir}: {len(rows)} clips")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m services.voice.eval", description=__doc__.split("\n")[0])
    parser.add_argument("--home", default=None, help="data root with voice/ models (default ~/.imurun)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--data", required=True)
    p_run.add_argument("--sets", default="")
    p_run.add_argument("--model", default="small")
    p_run.add_argument("--device", default="cpu")
    p_run.add_argument("--beam", type=int, default=5)
    p_run.add_argument("--threads", type=int, default=2)
    p_run.add_argument("--prompt", default="none")
    p_run.add_argument("--vocab", default="", help="extra comma-separated vocabulary for --prompt vocab")
    p_run.add_argument("--cleanup", action="store_true")
    p_run.add_argument("--force-lang", action="store_true")
    p_run.add_argument("--legacy", action="store_true", help="the pre-2026-11-02 decoding (baseline)")
    p_run.add_argument("--warmup", action="store_true", help="time the first call separately")
    p_run.add_argument("--limit", type=int, default=0)
    p_run.add_argument("--label", default="")
    p_run.add_argument("--out", default="")
    p_run.add_argument("--details", default="")
    p_rep = sub.add_parser("report")
    p_rep.add_argument("file")
    p_rep.add_argument("--markdown", action="store_true")
    p_syn = sub.add_parser("synth")
    p_syn.add_argument("--data", required=True)
    p_syn.add_argument("--set", required=True)
    p_syn.add_argument("--name", default="")
    p_syn.add_argument("--from-jsonl", default="")
    p_syn.add_argument("--voice", default="")
    p_syn.add_argument("--context", default="")
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args)
    if args.command == "report":
        return report(args)
    return synth(args)


if __name__ == "__main__":
    sys.exit(main())

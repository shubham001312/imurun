"""Voice command line (works without the server running).

    python -m services.voice doctor                  what is available on this machine
    python -m services.voice install whisper small   download a speech model
    python -m services.voice install piper hi_IN-priyamvada-medium
    python -m services.voice speak "text" --lang hi --out C:/path/reply.wav
    python -m services.voice transcribe C:/path/audio.webm [--lang hi]
    python -m services.voice roundtrip [--langs en,hi,bn,de] [--engine piper|system]
                                       speak test sentences, transcribe them, print WER/CER

``--home`` sets the data root (default ~/.imurun). Nothing is written to the
working directory: models and voices go to <home>/voice.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

# Sentences for the round trip: ordinary assistant replies, one per language.
ROUNDTRIP = {
    "en": "The weather in Berlin is cloudy today, with a high of twelve degrees.",
    "de": "Das Wetter in Berlin ist heute bewölkt, mit höchstens zwölf Grad.",
    "hi": "आज दिल्ली में मौसम साफ है और तापमान पच्चीस डिग्री है।",
    "bn": "আজ কলকাতায় আকাশ মেঘলা এবং তাপমাত্রা ত্রিশ ডিগ্রি।",
}


def _service(home: str | None):
    from services.server.settings import SettingsStore

    from .service import VoiceService

    root = Path(home) if home else Path.home() / ".imurun"
    settings = SettingsStore(root / "settings")

    def publish(event: str, data: dict) -> None:
        if event == "voice.model.download":
            pct = data.get("percent")
            sys.stderr.write(f"\r  {data['name']}: {data['state']} {pct if pct is not None else ''}%   ")
            if data.get("state") in ("done", "error"):
                sys.stderr.write("\n")
            sys.stderr.flush()

    return VoiceService(data_root=root, settings=settings, publish=publish)


def _normalize_words(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text).casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    return text.split()


def _distance(a: list, b: list) -> int:
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def error_rates(reference: str, hypothesis: str) -> tuple[float, float]:
    ref, hyp = _normalize_words(reference), _normalize_words(hypothesis)
    wer = _distance(ref, hyp) / max(1, len(ref))
    ref_c, hyp_c = list("".join(ref)), list("".join(hyp))
    cer = _distance(ref_c, hyp_c) / max(1, len(ref_c))
    return wer, cer


def doctor(service) -> int:
    import platform

    from .audio import ffmpeg_path
    from .languages import CORE, name

    status = service.status()
    print(f"IMURUN voice doctor  ({platform.system()} {platform.release()}, Python {platform.python_version()})")
    print(f"data root: {service.paths.root}")
    print(f"summary:   {status['summary']}")
    print("\npackages:")
    for module, present in service.packages().items():
        print(f"  {module:15s} {'yes' if present else 'no'}")
    print(f"  ffmpeg on PATH  {ffmpeg_path() or 'no'}")
    stt = status["stt"]
    print(f"\nSTT: {'available' if stt['available'] else 'UNAVAILABLE - ' + stt['reason']}")
    print(f"  model {stt['model']}: {'installed' if stt['model_installed'] else 'not downloaded'}")
    print(f"  formats: {', '.join(k for k, v in stt['formats'].items() if v is True)} (decoder {stt['formats']['decoder']})")
    tts = status["tts"]["engines"]
    print("\nTTS engines:")
    print(f"  piper   {'available' if tts['piper']['available'] else 'no - ' + tts['piper']['reason']}"
          f"  ({tts['piper']['installed_voices']} voices installed)")
    print(f"  system  {tts['system']['backend'] or 'no - ' + tts['system']['reason']}  ({tts['system']['voices']} voices)")
    print(f"  online  {'ENABLED' if tts['online']['enabled'] else 'off'} (installed: {tts['online']['installed']};"
          f" {tts['online']['privacy']})")
    matrix = service.voices()["languages"]
    print("\nlanguage matrix (core languages; full list: GET /api/voice/voices):")
    for code in CORE:
        row = matrix.get(code, {"voices": []})
        installed = [v["voice"] for v in row["voices"] if v.get("installed") and v.get("engine_available", True)]
        available = [v["voice"] for v in row["voices"] if not v.get("installed")]
        print(f"  {code} {name(code):8s} STT: {'yes' if row.get('stt') else 'no'}  "
              f"TTS ready: {', '.join(installed) or '-'}")
        if available:
            print(f"     on demand: {', '.join(available[:4])}{' ...' if len(available) > 4 else ''}")
    others = sorted(set(matrix) - set(CORE))
    print(f"\nother languages with a voice in the catalogue / system: {', '.join(others)}")
    return 0


def roundtrip(service, langs: list[str], engine: str, forced: bool = False) -> int:
    results = []
    for code in langs:
        sentence = ROUNDTRIP.get(code)
        if not sentence:
            print(f"{code}: no test sentence")
            continue
        voice = ""
        if engine == "system":
            matches = service.system.for_language(code)
            if not matches:
                print(f"{code}: no system voice - not tested")
                results.append({"lang": code, "engine": engine, "tested": False})
                continue
            voice = f"system:{matches[0].id}"
        try:
            start = time.perf_counter()
            speech = service.speak(sentence, code, voice)
            tts_s = time.perf_counter() - start
            start = time.perf_counter()
            heard = service.transcribe(speech.audio, speech.content_type, code if forced else "")
            stt_s = time.perf_counter() - start
        except Exception as exc:  # noqa: BLE001
            print(f"{code}: FAILED {type(exc).__name__}: {getattr(exc, 'message', exc)}")
            results.append({"lang": code, "engine": engine, "tested": False, "error": str(exc)})
            continue
        wer, cer = error_rates(sentence, heard["text"])
        row = {"lang": code, "model": service.config()["sttModel"], "forced": forced, "voice": speech.voice, "audio_s": round(speech.duration, 2),
               "tts_s": round(tts_s, 2), "stt_s": round(stt_s, 2), "detected": heard["language"],
               "detected_p": heard.get("language_probability"), "wer": round(wer, 3), "cer": round(cer, 3),
               "heard": heard["text"], "raw": heard.get("detected_raw"), "timings": heard.get("timings"), "tts_timings": speech.timings}
        results.append(row)
        print(json.dumps(row, ensure_ascii=False))
    return 0 if all(r.get("tested", True) for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m services.voice", description=__doc__.split("\n")[0])
    parser.add_argument("--home", default=None, help="data root (default ~/.imurun)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    p_install = sub.add_parser("install")
    p_install.add_argument("engine", choices=("whisper", "piper"))
    p_install.add_argument("name")
    p_speak = sub.add_parser("speak")
    p_speak.add_argument("text")
    p_speak.add_argument("--lang", default="")
    p_speak.add_argument("--voice", default="")
    p_speak.add_argument("--out", required=True)
    p_tr = sub.add_parser("transcribe")
    p_tr.add_argument("file")
    p_tr.add_argument("--lang", default="")
    p_rt = sub.add_parser("roundtrip")
    p_rt.add_argument("--langs", default="en,hi,bn,de")
    p_rt.add_argument("--engine", default="piper", choices=("piper", "system"))
    p_rt.add_argument("--model", default="", help="whisper size for this run (default: settings)")
    p_rt.add_argument("--forced", action="store_true", help="tell Whisper the language instead of detecting it")
    p_rt.add_argument("--threads", type=int, default=0)
    args = parser.parse_args(argv)
    service = _service(args.home)
    try:
        if args.command == "doctor":
            return doctor(service)
        if args.command == "install":
            job = service.install(args.engine, args.name, wait=True)
            print(json.dumps(job.view()))
            return 0 if job.state == "done" else 1
        if args.command == "speak":
            speech = service.speak(args.text, args.lang, args.voice)
            Path(args.out).write_bytes(speech.audio)
            print(json.dumps({**speech.headers(), "bytes": len(speech.audio), "timings": speech.timings}))
            return 0
        if args.command == "transcribe":
            data = Path(args.file).read_bytes()
            print(json.dumps(service.transcribe(data, "", args.lang), ensure_ascii=False, indent=2))
            return 0
        if args.command == "roundtrip":
            service.overrides = {k: v for k, v in (("sttModel", args.model), ("threads", args.threads)) if v}
            if args.forced:
                service.overrides["autoDetectLanguage"] = False
            return roundtrip(service, [x.strip() for x in args.langs.split(",") if x.strip()], args.engine,
                             args.forced)
    except Exception as exc:  # noqa: BLE001 - print the reason, not a traceback
        print(f"error: {getattr(exc, 'message', None) or exc}", file=sys.stderr)
        return 1
    finally:
        service.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())

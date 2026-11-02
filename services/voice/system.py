"""System-voice TTS fallback: zero installs, whatever the OS already has.

* Windows: SAPI 5 through PowerShell ``System.Speech`` (voices installed with
  Windows language packs / "Speech" voices; a stock en-US install has only
  David and Zira - measured on this machine, 2026-11-01).
* macOS: ``say`` (``say -v '?'`` lists voices with their locale).
* Linux: ``espeak-ng`` (or ``espeak``), which covers 100+ languages
  including hi, bn and de with a robotic voice. ``spd-say`` can only play to
  the speakers, not return audio, so it is reported but not used.

Each call is a short child process at lowered priority; text and voice name
are passed through the environment / stdin, never interpolated into a
command line. Output is WAV bytes. Scratch files go to ``<voice root>/tmp``
and are deleted after reading.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .languages import LANGUAGES, normalize

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000


@dataclass
class SystemVoice:
    id: str
    name: str
    language: str   # ISO 639-1
    locale: str     # e.g. "en-US"
    engine: str     # "sapi" | "say" | "espeak-ng"

    def view(self) -> dict:
        return {"engine": "system", "backend": self.engine, "voice": self.id, "name": self.name,
                "locale": self.locale, "installed": True, "offline": True, "quality": "system"}


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW}
    return {"preexec_fn": lambda: os.nice(10)}  # noqa: PLW1509 - only lowers our own child


def _run(args: list[str], *, env: dict | None = None, data: bytes | None = None, timeout: float = 120.0):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(args, input=data, capture_output=True, timeout=timeout, env=full_env,
                          check=False, **_popen_kwargs())


_PS_LIST = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "foreach ($v in $s.GetInstalledVoices()) { if ($v.Enabled) {"
    "[Console]::Out.WriteLine($v.VoiceInfo.Name + '|' + $v.VoiceInfo.Culture.Name) } }"
)
_PS_SPEAK = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "if ($env:IMURUN_TTS_VOICE) { $s.SelectVoice($env:IMURUN_TTS_VOICE) };"
    "$s.Rate = [int]$env:IMURUN_TTS_RATE;"
    "$s.SetOutputToWaveFile($env:IMURUN_TTS_OUT);"
    "$s.Speak($env:IMURUN_TTS_TEXT); $s.Dispose()"
)


class SystemTTS:
    def __init__(self, tmp_dir: Path) -> None:
        self._tmp = tmp_dir
        self._lock = threading.Lock()
        self._voices: list[SystemVoice] | None = None
        self.backend = self._detect_backend()

    @staticmethod
    def _detect_backend() -> str:
        if os.name == "nt":
            return "sapi" if shutil.which("powershell") or shutil.which("powershell.exe") else ""
        if sys.platform == "darwin":
            return "say" if shutil.which("say") else ""
        for name in ("espeak-ng", "espeak"):
            if shutil.which(name):
                return name
        return ""

    @property
    def available(self) -> bool:
        return bool(self.backend)

    def reason(self) -> str:
        if self.backend:
            return ""
        if os.name == "nt":
            return "PowerShell not found"
        if sys.platform == "darwin":
            return "`say` not found"
        extra = " (spd-say is present but cannot return audio)" if shutil.which("spd-say") else ""
        return "no espeak-ng: install it (apt install espeak-ng / dnf install espeak-ng)" + extra

    def voices(self, refresh: bool = False) -> list[SystemVoice]:
        with self._lock:
            if self._voices is not None and not refresh:
                return self._voices
            try:
                self._voices = self._list()
            except Exception:  # noqa: BLE001 - a broken system voice list is "none"
                self._voices = []
            return self._voices

    def _list(self) -> list[SystemVoice]:
        if self.backend == "sapi":
            proc = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_LIST], timeout=30)
            out = []
            for line in proc.stdout.decode("utf-8", "replace").splitlines():
                name, _, culture = line.strip().partition("|")
                if name:
                    out.append(SystemVoice(name, name, normalize(culture), culture, "sapi"))
            return out
        if self.backend == "say":
            proc = _run(["say", "-v", "?"], timeout=30)
            out = []
            for line in proc.stdout.decode("utf-8", "replace").splitlines():
                match = re.match(r"^(.+?)\s{2,}([a-z]{2,3}[_-][A-Za-z0-9]+)\s", line)
                if match:
                    name, locale = match.group(1).strip(), match.group(2).replace("_", "-")
                    out.append(SystemVoice(name, name, normalize(locale), locale, "say"))
            return out
        if self.backend in ("espeak-ng", "espeak"):
            proc = _run([self.backend, "--voices"], timeout=30)
            out = []
            for line in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
                cols = line.split()
                if len(cols) >= 4:
                    locale, voice_name = cols[1], cols[3]
                    out.append(SystemVoice(locale, voice_name, normalize(locale), locale, self.backend))
            return out
        return []

    def for_language(self, lang: str) -> list[SystemVoice]:
        code = normalize(lang)
        return [voice for voice in self.voices() if voice.language == code]

    def speak(self, text: str, lang: str = "", voice: str = "", rate: float = 1.0) -> tuple[bytes, str]:
        """(wav bytes, voice id used). Raises RuntimeError with a reason."""
        if not self.backend:
            raise RuntimeError(self.reason())
        code = normalize(lang)
        if not voice:
            matches = self.for_language(code) if code else self.voices()
            if not matches:
                label = LANGUAGES[code].english if code in LANGUAGES else (code or "this language")
                raise RuntimeError(f"no system voice for {label} on this machine")
            voice = matches[0].id
        self._tmp.mkdir(parents=True, exist_ok=True)
        target = self._tmp / f"sys-{uuid.uuid4().hex}.wav"
        try:
            if self.backend == "sapi":
                # SAPI rate is -10..10; 1.0x -> 0, 1.5x -> ~4, 0.6x -> ~-4.
                sapi_rate = max(-10, min(10, int(round((rate - 1.0) * 8))))
                proc = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_SPEAK],
                            env={"IMURUN_TTS_TEXT": text, "IMURUN_TTS_VOICE": voice,
                                 "IMURUN_TTS_OUT": str(target), "IMURUN_TTS_RATE": str(sapi_rate)})
                if proc.returncode != 0 or not target.is_file():
                    raise RuntimeError("SAPI failed: " + proc.stderr.decode("utf-8", "replace")[:200])
                return target.read_bytes(), voice
            if self.backend == "say":
                words = str(int(175 * rate))
                proc = _run(["say", "-v", voice, "-r", words, "-o", str(target), "--file-format=WAVE",
                             "--data-format=LEI16@22050", "-f", "-"], data=text.encode("utf-8"))
                if proc.returncode != 0 or not target.is_file():
                    raise RuntimeError("say failed: " + proc.stderr.decode("utf-8", "replace")[:200])
                return target.read_bytes(), voice
            proc = _run([self.backend, "-v", voice, "-s", str(int(175 * rate)), "--stdout"],
                        data=text.encode("utf-8"))
            if proc.returncode != 0 or not proc.stdout:
                raise RuntimeError(f"{self.backend} failed: " + proc.stderr.decode("utf-8", "replace")[:200])
            return proc.stdout, voice
        finally:
            try:
                target.unlink()
            except OSError:
                pass

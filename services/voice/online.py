"""Online TTS fallback: Microsoft Edge "Read aloud" voices via ``edge-tts``.

OFF BY DEFAULT. Turning it on (``voice.onlineFallback = true``) SENDS THE
TEXT OF THE REPLY TO MICROSOFT. It is only used when no offline engine has
a voice for the language. Keyless; needs ``pip install edge-tts`` and the
internet. Output is MP3 (``audio/mpeg``), not WAV.
"""
from __future__ import annotations

import asyncio
import importlib.util

from .languages import normalize

PRIVACY_NOTE = "sends the text to Microsoft (edge-tts, online)"

# Neural voices per language (names from edge-tts's voice list).
DEFAULT_VOICES = {
    "en": "en-US-AriaNeural", "hi": "hi-IN-SwaraNeural", "bn": "bn-IN-TanishaaNeural",
    "de": "de-DE-KatjaNeural", "fr": "fr-FR-DeniseNeural", "es": "es-ES-ElviraNeural",
    "it": "it-IT-ElsaNeural", "pt": "pt-BR-FranciscaNeural", "ru": "ru-RU-SvetlanaNeural",
    "ja": "ja-JP-NanamiNeural", "zh": "zh-CN-XiaoxiaoNeural", "ar": "ar-SA-ZariyahNeural",
    "ur": "ur-PK-UzmaNeural", "ta": "ta-IN-PallaviNeural", "te": "te-IN-ShrutiNeural",
    "mr": "mr-IN-AarohiNeural", "gu": "gu-IN-DhwaniNeural", "kn": "kn-IN-SapnaNeural",
    "ml": "ml-IN-SobhanaNeural", "ne": "ne-NP-HemkalaNeural", "tr": "tr-TR-EmelNeural",
    "nl": "nl-NL-ColetteNeural", "pl": "pl-PL-ZofiaNeural", "ko": "ko-KR-SunHiNeural",
}


def installed() -> bool:
    try:
        return importlib.util.find_spec("edge_tts") is not None
    except (ImportError, ValueError):
        return False


def speak(text: str, lang: str = "", voice: str = "", rate: float = 1.0, timeout: float = 60.0) -> tuple[bytes, str]:
    """(mp3 bytes, voice). Raises RuntimeError with a reason."""
    if not installed():
        raise RuntimeError("edge-tts is not installed (pip install edge-tts)")
    import edge_tts

    code = normalize(lang) or "en"
    voice = voice or DEFAULT_VOICES.get(code, "")
    if not voice:
        raise RuntimeError(f"no online voice configured for {code!r}")
    percent = int(round((rate - 1.0) * 100))
    rate_text = f"{percent:+d}%"

    async def run() -> bytes:
        out = bytearray()
        async for chunk in edge_tts.Communicate(text, voice, rate=rate_text).stream():
            if chunk.get("type") == "audio":
                out += chunk.get("data") or b""
        return bytes(out)

    try:
        audio = asyncio.run(asyncio.wait_for(run(), timeout))
    except Exception as exc:  # noqa: BLE001 - network / service errors become a reason
        raise RuntimeError(f"online TTS failed: {type(exc).__name__}: {exc}") from exc
    if not audio:
        raise RuntimeError("online TTS returned no audio")
    return audio, voice

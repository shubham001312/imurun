"""Language codes, names and the per-language engine mapping.

One table answers every language question the voice layer has:

* normalising what a client sends (``hi-IN``, ``hi_IN``, ``Hindi``) to the
  ISO 639-1 code Whisper uses (``hi``);
* the English and native name (for the picker and for the one-line
  "Answer in Hindi" directive the turn can carry);
* which system-voice culture / espeak voice covers it.

Piper coverage is NOT hard-coded here: it comes from the Piper voice
catalogue (``catalogue.py``), which was checked against the live
``voices.json`` on 2026-11-01. Whisper's language list is the one the model
itself carries (``WHISPER_LANGUAGES`` below mirrors faster-whisper's
``tokenizer._LANGUAGE_CODES`` so it is available without the package).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str            # ISO 639-1 (Whisper's code)
    english: str
    native: str
    sapi: str = ""       # Windows culture prefix, e.g. "hi-IN"
    espeak: str = ""     # espeak-ng voice name


# The languages the product promises first, then common ones. Anything
# Whisper knows but is not listed here still works for STT; it just has no
# native name in the picker.
_TABLE = (
    Language("en", "English", "English", "en-", "en"),
    Language("hi", "Hindi", "हिन्दी", "hi-IN", "hi"),
    Language("bn", "Bengali", "বাংলা", "bn-", "bn"),
    Language("de", "German", "Deutsch", "de-", "de"),
    Language("fr", "French", "Français", "fr-", "fr"),
    Language("es", "Spanish", "Español", "es-", "es"),
    Language("it", "Italian", "Italiano", "it-", "it"),
    Language("pt", "Portuguese", "Português", "pt-", "pt"),
    Language("nl", "Dutch", "Nederlands", "nl-", "nl"),
    Language("ru", "Russian", "Русский", "ru-", "ru"),
    Language("uk", "Ukrainian", "Українська", "uk-", "uk"),
    Language("pl", "Polish", "Polski", "pl-", "pl"),
    Language("tr", "Turkish", "Türkçe", "tr-", "tr"),
    Language("ar", "Arabic", "العربية", "ar-", "ar"),
    Language("fa", "Persian", "فارسی", "fa-", "fa"),
    Language("ur", "Urdu", "اردو", "ur-", "ur"),
    Language("ne", "Nepali", "नेपाली", "ne-", "ne"),
    Language("mr", "Marathi", "मराठी", "mr-", "mr"),
    Language("te", "Telugu", "తెలుగు", "te-", "te"),
    Language("ta", "Tamil", "தமிழ்", "ta-", "ta"),
    Language("ml", "Malayalam", "മലയാളം", "ml-", "ml"),
    Language("gu", "Gujarati", "ગુજરાતી", "gu-", "gu"),
    Language("kn", "Kannada", "ಕನ್ನಡ", "kn-", "kn"),
    Language("pa", "Punjabi", "ਪੰਜਾਬੀ", "pa-", "pa"),
    Language("zh", "Chinese", "中文", "zh-", "cmn"),
    Language("ja", "Japanese", "日本語", "ja-", "ja"),
    Language("ko", "Korean", "한국어", "ko-", "ko"),
    Language("vi", "Vietnamese", "Tiếng Việt", "vi-", "vi"),
    Language("id", "Indonesian", "Bahasa Indonesia", "id-", "id"),
    Language("sv", "Swedish", "Svenska", "sv-", "sv"),
    Language("cs", "Czech", "Čeština", "cs-", "cs"),
    Language("el", "Greek", "Ελληνικά", "el-", "el"),
    Language("hu", "Hungarian", "Magyar", "hu-", "hu"),
    Language("ro", "Romanian", "Română", "ro-", "ro"),
    Language("fi", "Finnish", "Suomi", "fi-", "fi"),
    Language("da", "Danish", "Dansk", "da-", "da"),
    Language("no", "Norwegian", "Norsk", "nb-", "nb"),
    Language("sw", "Swahili", "Kiswahili", "sw-", "sw"),
)
LANGUAGES: dict[str, Language] = {item.code: item for item in _TABLE}

# The four languages the product owner named; the matrix always lists them.
CORE = ("en", "hi", "bn", "de")

# faster-whisper tokenizer._LANGUAGE_CODES (99 codes; "yue" is large-v3 only).
WHISPER_LANGUAGES = frozenset(
    "af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr gl "
    "gu ha haw he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln lo lt lv mg mi "
    "mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk sl sn so sq sr "
    "su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh yue".split()
)

_BY_NAME = {item.english.casefold(): item.code for item in _TABLE}
_BY_NAME.update({item.native.casefold(): item.code for item in _TABLE})
_ALIASES = {"iw": "he", "nb": "no", "nn": "no", "jv": "jw", "cmn": "zh", "in": "id"}


def normalize(value: str | None) -> str:
    """``"hi-IN"`` / ``"hi_IN"`` / ``"Hindi"`` / ``"HI"`` -> ``"hi"``; "" if unknown/empty.

    Unknown two/three-letter codes pass through lower-cased so a language the
    table does not name (but Whisper or Piper knows) still works.
    """
    text = str(value or "").strip()
    if not text or text.lower() in ("auto", "detect"):
        return ""
    folded = text.casefold()
    if folded in _BY_NAME:
        return _BY_NAME[folded]
    base = folded.replace("_", "-").split("-")[0]
    base = _ALIASES.get(base, base)
    if base.isalpha() and 2 <= len(base) <= 3:
        return base
    return ""


def name(code: str) -> str:
    item = LANGUAGES.get(normalize(code))
    return item.english if item else (code or "")


def native_name(code: str) -> str:
    item = LANGUAGES.get(normalize(code))
    return item.native if item else (code or "")


def answer_directive(code: str | None) -> str:
    """The one house-style line a turn adds when the user spoke a language.

    Empty for English or an unknown code (English is the model's default, so
    the line would only cost tokens). The native name is included because a
    small model follows "Answer in हिन्दी (Hindi)" more reliably than a bare
    language code.
    """
    lang = normalize(code)
    if not lang or lang == "en":
        return ""
    item = LANGUAGES.get(lang)
    label = f"{item.english} ({item.native})" if item and item.native != item.english else name(lang)
    return f"- The user is speaking {label}. Answer in {label}, in its own script."


# Unicode blocks whose script names one language well enough to pick a voice.
_SCRIPTS = (
    (0x0980, 0x09FF, "bn"), (0x0900, 0x097F, "hi"), (0x0A00, 0x0A7F, "pa"), (0x0A80, 0x0AFF, "gu"),
    (0x0B80, 0x0BFF, "ta"), (0x0C00, 0x0C7F, "te"), (0x0C80, 0x0CFF, "kn"), (0x0D00, 0x0D7F, "ml"),
    (0x0600, 0x06FF, "ar"), (0x0400, 0x04FF, "ru"), (0x0370, 0x03FF, "el"), (0x3040, 0x30FF, "ja"),
    (0xAC00, 0xD7AF, "ko"), (0x4E00, 0x9FFF, "zh"),
)


def script_language(text: str) -> str:
    """The language a non-Latin script implies (Devanagari -> hi), or "".

    Used only to pick a TTS voice when the reply's script contradicts the
    requested language (a Hindi reply with ``lang=en``). Latin text returns
    "" because the script cannot tell English from German.
    """
    counts: dict[str, int] = {}
    for char in str(text or "")[:400]:
        point = ord(char)
        if point < 0x0370:
            continue
        for low, high, code in _SCRIPTS:
            if low <= point <= high:
                counts[code] = counts.get(code, 0) + 1
                break
    if not counts:
        return ""
    code, count = max(counts.items(), key=lambda item: item[1])
    letters = sum(1 for char in str(text)[:400] if char.isalpha())
    return code if letters and count / letters >= 0.3 else ""


_SAME_SCRIPT = {
    "hi": {"hi", "mr", "ne", "sa"}, "bn": {"bn", "as"}, "ar": {"ar", "ur", "fa", "ps", "sd"},
    "ru": {"ru", "uk", "bg", "sr", "mk", "be", "kk"}, "zh": {"zh", "yue", "ja"},
}


def voice_language(requested: str, text: str) -> str:
    """The language to speak ``text`` in: the request, unless the script says otherwise."""
    lang = normalize(requested)
    script = script_language(text)
    if script and lang not in _SAME_SCRIPT.get(script, {script}):
        return script
    return lang or script or "en"


def whisper_supports(code: str) -> bool:
    return normalize(code) in WHISPER_LANGUAGES

"""Context biasing: the ``initial_prompt`` Whisper decodes after.

Whisper conditions on its prompt as if it were the text just before the
audio, so names that appear there are spelled that way ("Phi-3", not "five
3"). ``build_prompt`` assembles, in priority order and within a budget:

1. the user's corrections (``voice.corrections``: what they fixed before);
2. the user's own words (``voice.vocabulary``);
3. names from the current conversation (the caller sends the last messages
   as ``context``; capitalised words, CamelCase, words with digits);
4. the product's built-in names (``PRODUCT_TERMS``).

A glossary-style prompt is used; a prompt in another script than the speech is skipped so
an English list cannot pull Hindi/Bengali speech into Latin letters.
Stdlib only.
"""
from __future__ import annotations

import re

PRODUCT_TERMS = (
    "IMURUN", "FISH", "ImuTechz", "Phi-3", "Llama", "Qwen", "DeepSeek", "Gemma", "Mistral", "GGUF",
    "Vulkan", "CUDA", "LoRA", "QLoRA", "Whisper", "Piper", "Gmail", "GitHub", "Slack", "Vercel",
    "Render", "Cloudflare", "Supabase", "Canva", "MCP",
)
# Whisper keeps the last 223 prompt tokens; ~4 characters per token for Latin
# text. 600 characters leaves the decoder room and keeps the prompt cheap.
MAX_PROMPT_CHARS = 600
MAX_CONTEXT_TERMS = 24
MAX_TERM_CHARS = 40

_STOP = {
    "the", "a", "an", "i", "you", "we", "it", "this", "that", "and", "or", "but", "if", "so", "to", "of", "in",
    "on", "for", "with", "what", "why", "how", "when", "where", "who", "can", "could", "would", "should", "is",
    "are", "was", "were", "be", "do", "does", "did", "my", "your", "our", "please", "hi", "hello", "thanks",
    "ok", "okay", "yes", "no", "not", "now", "then", "here", "there", "also", "just", "let", "make", "give",
    "tell", "show", "write", "use", "run", "get", "set", "add", "find", "need", "want", "like", "one", "two",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "today", "tomorrow",
}

_TERM_RE = re.compile(
    r"(?<![\w-])("
    r"[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+"            # CamelCase: DeepSeek, ImuTechz
    r"|[A-Za-z]+[-.]?\d+[A-Za-z0-9.-]*"           # with digits: Phi-3, Qwen2.5, GPT-4o
    r"|[A-Z]{2,}[a-z]?"                            # acronyms: GGUF, MCP, LoRA-like
    r"|[A-Z][a-z]{2,}"                             # Capitalised: Rahul, Berlin
    r")(?![\w-])")


def _script(text: str) -> str:
    latin = sum(1 for ch in text if ch.isalpha() and ord(ch) < 0x0250)
    other = sum(1 for ch in text if ch.isalpha() and ord(ch) >= 0x0250)
    if not latin and not other:
        return ""
    return "latin" if latin >= other else "other"


LATIN_LANGUAGES = {"en", "de", "fr", "es", "it", "pt", "nl", "sv", "da", "no", "pl", "cs", "ro", "tr", "id",
                   "vi", "fi", "hu", "sk", "sl", "hr", "ca", "et", "lv", "lt", "ms", "sw", "cy", "is"}


def context_terms(messages, limit: int = MAX_CONTEXT_TERMS) -> list[str]:
    """Names worth spelling right, newest message first."""
    if isinstance(messages, str):
        messages = [messages]
    found: list[str] = []
    seen: set[str] = set()
    for message in reversed(list(messages or [])[-8:]):
        text = message.get("content", "") if isinstance(message, dict) else str(message or "")
        text = re.sub(r"https?://\S+|`[^`]*`", " ", text[:4000])
        for sentence in re.split(r"(?<=[.!?\n])\s+", text):
            words = sentence.split()
            for match in _TERM_RE.finditer(sentence):
                term = match.group(1).strip(".-")
                key = term.casefold()
                if len(term) < 2 or len(term) > MAX_TERM_CHARS or key in _STOP or key in seen:
                    continue
                # A capitalised first word of a sentence is usually just grammar.
                plain_cap = re.fullmatch(r"[A-Z][a-z]+", term) is not None
                if plain_cap and words and words[0].strip("\"'(") == term:
                    continue
                seen.add(key)
                found.append(term)
                if len(found) >= limit:
                    return found
    return found


def _clean_terms(items) -> list[str]:
    out, seen = [], set()
    for item in items or ():
        term = " ".join(str(item or "").split())[:MAX_TERM_CHARS]
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            out.append(term)
    return out


def build_prompt(*, language: str = "", vocabulary=None, context=None, corrections=None,
                 product_terms: bool = True, max_chars: int = MAX_PROMPT_CHARS) -> str:
    """A glossary prompt, or "" when there is nothing useful to say.

    ``language`` is the forced/expected language ("" = unknown). For a
    non-Latin-script language only terms in that script are kept: a Latin
    glossary measurably pulled Hindi speech towards English words.
    """
    fixed = [c.get("fixed") if isinstance(c, dict) else (c[1] if isinstance(c, (list, tuple)) and len(c) > 1 else "")
             for c in (corrections or ())]
    terms = _clean_terms([*fixed, *(vocabulary or ()), *context_terms(context or []),
                          *(PRODUCT_TERMS if product_terms else ())])
    code = (language or "").lower().split("-")[0]
    if code and code not in LATIN_LANGUAGES:
        terms = [t for t in terms if _script(t) == "other"]
    if not terms:
        return ""
    picked, size = [], len("Glossary: .")
    for term in terms:
        if size + len(term) + 2 > max_chars:
            break
        picked.append(term)
        size += len(term) + 2
    return "Glossary: " + ", ".join(picked) + "."

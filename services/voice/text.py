"""Text preparation for speech: Markdown -> speakable text, sentence chunks.

The same rules as the shell's browser read-aloud (``docs/frontend/VOICE.md``)
so server and browser speech sound alike: code blocks become "(code block)",
links read their label, bare URLs read "a link", list/table/heading markup is
dropped. Sentences end at ``. ! ?``, the Devanagari/Bengali danda ``।`` and
double danda ``॥``, and CJK full stops; long sentences are cut at a comma or
space near ``max_chars`` so speech can start early.
"""
from __future__ import annotations

import re

MAX_CHARS = 220
MAX_TEXT = 5000  # one /api/voice/speak call; longer text must be chunked by the caller

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_CITATION = re.compile(r"\[\d+\]")
_SENTENCE_END = re.compile(r"(?<=[.!?।॥。！？])[\"'”’)\]]*\s+")


def speakable(markdown: str) -> str:
    text = str(markdown or "")
    text = _FENCE.sub(" (code block) ", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("a link", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _TABLE_RULE.sub("", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _CITATION.sub("", text)
    for _ in range(2):
        text = _EMPHASIS.sub(r"\2", text)
    text = text.replace("|", ", ")
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.!?।])", r"\1", text)
    text = re.sub(r"([.!?।])(?:\s*\.)+", r"\1", text)
    return text.strip(" ,")


def sentences(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    out: list[str] = []
    for piece in _SENTENCE_END.split(text.strip()):
        piece = piece.strip()
        while len(piece) > max_chars:
            cut = max(piece.rfind(", ", 0, max_chars), piece.rfind("، ", 0, max_chars))
            if cut < max_chars // 3:
                cut = piece.rfind(" ", 0, max_chars)
            if cut < max_chars // 3:
                cut = max_chars
            out.append(piece[:cut + 1].strip())
            piece = piece[cut + 1:].strip()
        if piece:
            out.append(piece)
    return out

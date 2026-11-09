"""Dictation clean-up ("Flow formatting"): deterministic, fast, per language.

``clean(text, language)`` turns a Whisper transcript into what a person would
have typed:

1. user corrections (``voice.corrections``: "five 3" -> "Phi-3"), whole words;
2. filler words (per-language list: "um", "uh", "äh", "उम्म" ...);
3. stutters: an adjacent repeat of one to three words ("the the", "can you
   can you"), English and German only - Hindi and Bengali reduplicate on
   purpose ("धीरे धीरे");
4. self-corrections ("send it Tuesday, no wait, Wednesday" -> "send it
   Wednesday"; "no wait / scratch that / make that / I mean / sorry /
   actually"), anchored on a repeated word or, for strong markers, replacing
   as many words as the correction has;
5. spoken punctuation ("comma", "question mark", "new line" in en/hi/de;
   "period" / "Punkt" only where they end a sentence);
6. English numbers: "twenty five" -> "25", "three point five" -> "3.5",
   "twenty first" -> "21st", "50 percent" -> "50%" (below ten stays a word);
7. spacing, sentence capitals and a final full stop where Whisper left none.

Whisper already writes punctuation and capitals, so this pass mostly removes
things. Rules are conservative (no rewriting of
ordinary speech; see tests/test_voice_dictation.py); WER on real sets is not
yet recorded (docs/voice/README.md "Not measured"). Stdlib only; safe to import in the server.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ------------------------------------------------------------------ word lists

FILLERS = {
    "en": ("um", "umm", "ummm", "uh", "uhh", "uhm", "erm", "er", "ah", "ahh", "hmm", "hm", "mm", "mmm", "eh", "huh"),
    "de": ("äh", "ähm", "äähm", "öhm", "öh", "hm", "hmm", "ähem", "em"),
    "hi": ("उम्म", "उम", "अं", "अम्म", "हम्म", "ऊं", "आं", "अ", "um", "uh", "umm", "hmm"),
    "bn": ("উম", "উমম", "হুম", "um", "uh"),
}
# Discourse fillers that only count when set off by commas or at the start.
SOFT_FILLERS = {"en": ("you know", "like", "i mean", "so yeah", "kind of", "sort of", "basically", "oh"),
                "de": ("also", "halt", "sozusagen", "quasi"),
                "hi": ("मतलब", "वो", "यानी"), "bn": ()}
SOFT_FILLERS_START_ONLY = {"so yeah", "basically", "also", "मतलब", "वो", "oh"}

# Self-correction markers, longest first. Strong markers may replace words
# without an anchor; weak ones need an anchor or two same-kind words.
STRONG_MARKERS = {
    "en": ("no wait", "wait no", "no sorry", "sorry no", "scratch that", "actually make that", "make that",
           "actually no", "no no", "or rather", "i meant"),
    "de": ("nein warte", "warte nein", "nein sorry", "nein quatsch", "quatsch", "ich meine", "besser gesagt"),
    "hi": ("नहीं नहीं", "नहीं रुको", "सॉरी", "मेरा मतलब", "no wait", "sorry"),
    "bn": ("না না", "না মানে", "sorry"),
}
WEAK_MARKERS = {"en": ("i mean", "sorry", "actually", "rather", "no"), "de": ("nein", "sorry", "äh nein"),
                "hi": ("नहीं", "मतलब"), "bn": ("না", "মানে")}

SPOKEN_PUNCT = {
    "en": (("new paragraph", "\n\n"), ("new line", "\n"), ("next line", "\n"), ("question mark", "?"),
           ("exclamation mark", "!"), ("exclamation point", "!"), ("full stop", "."), ("semicolon", ";"),
           ("comma", ",")),
    "de": (("neuer absatz", "\n\n"), ("neue zeile", "\n"), ("fragezeichen", "?"), ("ausrufezeichen", "!"),
           ("semikolon", ";"), ("komma", ",")),
    "hi": (("नया पैराग्राफ", "\n\n"), ("नई लाइन", "\n"), ("न्यू लाइन", "\n"), ("प्रश्नवाचक चिह्न", "?"),
           ("क्वेश्चन मार्क", "?"), ("पूर्ण विराम", "।"), ("फुल स्टॉप", "।"), ("कॉमा", ",")),
    "bn": (("নতুন লাইন", "\n"), ("প্রশ্নবোধক চিহ্ন", "?"), ("দাঁড়ি", "।"), ("কমা", ",")),
}
# Words that are ordinary words mid-sentence; punctuation only at the end.
END_PUNCT = {"en": (), "de": (), "hi": (), "bn": ()}
# ("period" / "Punkt" were tried and removed: "a long period" and "Das ist
# der Punkt" end sentences too; Whisper writes a spoken final "period" as "."
# itself in the measured clips.)

# One-word repeats that are real language, not stutters.
KEEP_REPEATS = {"very", "really", "no", "bye", "so", "ha", "had", "that", "is", "do", "yeah", "yes", "okay",
                "ok", "well", "go", "hey", "now", "more", "far", "many", "much", "again", "over", "on",
                "die", "der", "das", "sehr", "ja", "nein", "so", "nun", "gut"}

_UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
             "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
             "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
             "nineteenth": 19, "twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50,
             "sixtieth": 60, "seventieth": 70, "eightieth": 80, "ninetieth": 90, "hundredth": 100}
_KINDS = {
    "day": {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "today", "tomorrow",
            "tonight", "montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag",
            "heute", "morgen", "übermorgen", "gestern", "yesterday", "आज", "कल", "परसों", "সোমবার", "আজ",
            "কাল", "সোমवार", "सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"},
    "month": {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
              "november", "december"},
}

_WORD = r"[\w'ऀ-৿̀-ͯ-]+"


@dataclass
class Cleaned:
    text: str
    changes: list[str] = field(default_factory=list)

    def view(self) -> dict:
        return {"text": self.text, "changes": list(self.changes)}


def _lang(language: str) -> str:
    code = (language or "en").lower().split("-")[0].split("_")[0]
    return code if code in FILLERS else "en"


def _alt(words) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


# ------------------------------------------------------------------ numbers

def _number_value(tokens: list[str]) -> int | None:
    total, current, seen = 0, 0, False
    for tok in tokens:
        if tok in _UNITS:
            if current % 10 or (current and current < 20 and current % 100 != 0 and current >= 10):
                return None  # "five six" is two numbers, not 56
            current += _UNITS[tok]
        elif tok in _TENS:
            if current % 100:
                return None
            current += _TENS[tok]
        elif tok == "hundred":
            current = max(1, current) * 100
        elif tok in ("thousand", "million", "billion"):
            total += max(1, current) * _SCALES[tok]
            current = 0
        elif tok == "and" and seen:
            continue
        else:
            return None
        seen = True
    return total + current if seen else None


def numbers_to_digits(text: str, *, minimum: int = 10, changes: list[str] | None = None) -> str:
    """English number words -> digits. ``minimum`` keeps "three apples" as words."""
    pieces = re.split(r"(\s+)", text)
    words = [(i, p) for i, p in enumerate(pieces) if p and not p.isspace()]
    out = list(pieces)
    k = 0
    number_words = set(_UNITS) | set(_TENS) | set(_SCALES) | {"and"}
    while k < len(words):
        j = k
        run = []
        while j < len(words):
            bare = words[j][1].lower().strip(",.;:!?")
            core = bare.split("-") if "-" in bare else [bare]
            if all(c in number_words for c in core) and not (bare == "and" and not run):
                run.extend(core)
                j += 1
                if words[j - 1][1][-1:] in ",.;:!?":
                    break
                continue
            break
        ordinal = None
        if j < len(words) and run:
            bare = words[j][1].lower().strip(",.;:!?")
            if bare in _ORDINALS and _ORDINALS[bare] < 100 and run[-1] in _TENS:
                ordinal = bare
        while run and run[-1] == "and":
            run.pop()
            j -= 1
        if run:
            point_digits = []
            if j + 1 < len(words) and words[j][1].lower() == "point":
                m = j + 1
                while m < len(words) and words[m][1].lower().strip(",.;:!?") in _UNITS:
                    point_digits.append(str(_UNITS[words[m][1].lower().strip(",.;:!?")] % 10))
                    m += 1
            value = _number_value(run)
            if value is not None and run != ["oh"]:
                tail = words[j - 1][1][-1:] if words[j - 1][1][-1:] in ",.;:!?" else ""
                if ordinal:
                    suffix = {1: "st", 2: "nd", 3: "rd"}.get(_ORDINALS[ordinal] % 10, "th")
                    value += _ORDINALS[ordinal]
                    rendered = f"{value}{suffix}"
                    otail = words[j][1][-1:] if words[j][1][-1:] in ",.;:!?" else ""
                    span_end = j + 1
                    tail = otail
                elif point_digits:
                    rendered = f"{value}.{''.join(point_digits)}"
                    span_end = j + 1 + len(point_digits)
                    tail = words[span_end - 1][1][-1:] if words[span_end - 1][1][-1:] in ",.;:!?" else ""
                else:
                    rendered = str(value)
                    span_end = j
                if value >= minimum or ordinal or point_digits or len(run) > 1:
                    first, last = words[k][0], words[span_end - 1][0]
                    original = "".join(pieces[first:last + 1])
                    out[first] = rendered + tail
                    for idx in range(first + 1, last + 1):
                        out[idx] = ""
                    if changes is not None:
                        changes.append(f"number:{original.strip()}")
                    k = span_end
                    continue
        k = max(k + 1, j if run else k + 1)
    result = "".join(out)
    return re.sub(r"(\d)\s+(?:percent|per cent)\b", r"\1%", result, flags=re.IGNORECASE)


# ------------------------------------------------------------------ passes

def apply_corrections(text: str, corrections, changes: list[str]) -> str:
    for item in corrections or ():
        if isinstance(item, dict):
            heard, fixed = str(item.get("heard") or ""), str(item.get("fixed") or "")
        else:
            heard, fixed = (str(x) for x in item)
        heard = heard.strip()
        if not heard or not fixed.strip() or heard.casefold() == fixed.casefold():
            continue
        pattern = re.compile(r"(?<![\w-])" + r"[\s-]+".join(re.escape(w) for w in heard.split()) + r"(?![\w-])",
                             re.IGNORECASE)
        text, n = pattern.subn(fixed.strip(), text)
        if n:
            changes.append(f"correction:{heard}->{fixed.strip()}")
    return text


def remove_fillers(text: str, lang: str, changes: list[str]) -> str:
    hard = FILLERS.get(lang, ()) + (FILLERS["en"] if lang != "en" else ())
    # The commas Whisper sets around a filler go with it ("want to, uh, send").
    pattern = re.compile(r"(,\s*)?(?<![\wऀ-৿])(" + _alt(hard) + r")(?![\wऀ-৿])[,…]*(\s*)",
                         re.IGNORECASE)

    def drop(match):
        changes.append(f"filler:{match.group(2).lower()}")
        return " " if match.group(1) or match.group(3) else ""

    text = pattern.sub(drop, text)
    soft = SOFT_FILLERS.get(lang, ())
    for phrase in soft:
        # ", you know," mid-sentence, or "You know, ..." at a sentence start.
        n = 0
        if phrase not in SOFT_FILLERS_START_ONLY:
            mid = re.compile(r",\s*" + re.escape(phrase) + r"\s*,", re.IGNORECASE)
            text, n = mid.subn(" ", text)
        start = re.compile(r"(^|[.!?।]\s+)" + re.escape(phrase) + r"\s*,\s*", re.IGNORECASE)
        text, m = start.subn(r"\1", text)
        if n or m:
            changes.append(f"filler:{phrase}")
    return text


def collapse_repeats(text: str, lang: str, changes: list[str]) -> str:
    if lang not in ("en", "de"):
        return text
    pattern = re.compile(r"(?<![\w'])((?:" + _WORD + r")(?:\s+" + _WORD + r"){0,2})(?:\s*[,?]?\s+\1)+(?![\w'])",
                         re.IGNORECASE)

    def fix(match):
        phrase = match.group(1)
        if " " not in phrase and phrase.lower() in KEEP_REPEATS:
            return match.group(0)
        if phrase.isdigit():
            return match.group(0)
        changes.append(f"repeat:{phrase.lower()}")
        return phrase

    return pattern.sub(fix, text)


def _kind(word: str) -> str:
    w = word.lower().strip(",.;:!?")
    if w.isdigit() or w in _UNITS or w in _TENS or w in _ORDINALS or re.fullmatch(r"\d+(st|nd|rd|th|%)?", w):
        return "number"
    for name, members in _KINDS.items():
        if w in members:
            return name
    if re.fullmatch(r"\d{1,2}(:\d\d)?(am|pm)?", w) or w in ("am", "pm", "noon", "midnight"):
        return "time"
    return ""


_SENT_SPLIT = re.compile(r"(?<=[.!?।])\s+|\n+")


def resolve_self_corrections(text: str, lang: str, changes: list[str]) -> str:
    strong = STRONG_MARKERS.get(lang, ()) + (STRONG_MARKERS["en"] if lang != "en" else ())
    weak = WEAK_MARKERS.get(lang, ())
    markers = [(m, True) for m in strong] + [(m, False) for m in weak]
    markers.sort(key=lambda item: len(item[0]), reverse=True)
    out_sentences = []
    pieces = _split_keep(text)
    merged: list[str] = []
    for piece in pieces:
        # Whisper often ends the sentence before the correction:
        # "...on Tuesday. No wait, Wednesday." - join it back first.
        head = piece.strip().lower()
        if merged and any(head.startswith(m + ",") or head.startswith(m + " ") for m, strong_ in markers if strong_):
            merged[-1] = merged[-1].rstrip().rstrip(".!?।") + ", " + piece.strip()
            if piece.endswith((" ", "\n")):
                merged[-1] += piece[len(piece.rstrip()):]
        else:
            merged.append(piece)
    for sentence in merged:
        out_sentences.append(_resolve_sentence(sentence, markers, changes))
    return "".join(out_sentences)


def _split_keep(text: str) -> list[str]:
    parts, last = [], 0
    for match in _SENT_SPLIT.finditer(text):
        parts.append(text[last:match.end()])
        last = match.end()
    parts.append(text[last:])
    return [p for p in parts if p]


def _resolve_sentence(sentence: str, markers, changes: list[str], depth: int = 0) -> str:
    if depth > 3:
        return sentence
    trail = re.search(r"[.!?।]*\s*$", sentence)
    end = sentence[trail.start():] if trail else ""
    body = sentence[: trail.start()] if trail else sentence
    lowered = body.lower()
    for marker, strong in markers:
        # The marker as whole words, usually set off by commas in Whisper's text.
        pattern = re.compile(r"(?:^|[\s,;—-])" + r"[\s,]+".join(re.escape(w) for w in marker.split())
                             + r"(?:[\s,;—-]+|$)")
        found = list(pattern.finditer(lowered))
        if not found:
            continue
        match = found[-1]
        left_text = body[: match.start()].rstrip(" ,;—-")
        right_text = body[match.end():].strip(" ,;—-")
        left, right = left_text.split(), right_text.split()
        if not left or not right or len(right) > 6:
            continue
        bare_no = marker in ("no", "nein", "नहीं", "না")
        if bare_no and "," not in body[max(0, match.start() - 1):match.end() + 1] and \
                not (_kind(left[-1]) and _kind(left[-1]) == _kind(right[0]) and len(right) == 1):
            continue  # a bare "no" is a word; ", no," or "Thursday no Friday" is a correction
        first = right[0].lower().strip(",.;:!?")
        anchor = None
        for i in range(len(left) - 1, max(-1, len(left) - 9), -1):
            if left[i].lower().strip(",.;:!?") == first:
                anchor = i
                break
        new_left = None
        kind = _kind(right[0])
        if anchor is None and kind:
            # "Tuesday, no, Wednesday" / "morgen kommen, nein warte, übermorgen":
            # swap the nearest word of the same kind; a one-word fix keeps what
            # followed it, a longer one replaces it.
            for i in range(len(left) - 1, max(-1, len(left) - 5), -1):
                if _kind(left[i]) == kind:
                    changes.append(f"self-correction:{marker}")
                    rest = left[i + 1:] if len(right) == 1 else []
                    rebuilt = " ".join(left[:i] + right + rest)
                    if body[:1].isupper() and rebuilt[:1].islower():
                        rebuilt = rebuilt[0].upper() + rebuilt[1:]
                    return _resolve_sentence(rebuilt + end, markers, changes, depth + 1)
        if anchor is not None and not (anchor == 0 and len(left) > 1 and not strong and len(right) < 2):
            new_left = left[:anchor]
        elif len(right) <= 3 and len(left) >= len(right):
            replaced = left[-len(right):]
            same_kind = _kind(replaced[0]) and _kind(replaced[0]) == _kind(right[0])
            if strong or same_kind:
                new_left = left[: len(left) - len(right)]
        if new_left is None:
            continue
        changes.append(f"self-correction:{marker}")
        rebuilt = " ".join(new_left + right)
        if body[:1].isupper() and rebuilt[:1].islower():
            rebuilt = rebuilt[0].upper() + rebuilt[1:]
        return _resolve_sentence(rebuilt + end, markers, changes, depth + 1)
    return sentence


def spoken_punctuation(text: str, lang: str, changes: list[str]) -> str:
    table = list(SPOKEN_PUNCT.get(lang, ()))
    if lang != "en":
        table += [item for item in SPOKEN_PUNCT["en"] if item[0] not in ("full stop",)]
    table.sort(key=lambda item: len(item[0]), reverse=True)
    for phrase, mark in table:
        pattern = re.compile(r"[\s,]*(?<![\wऀ-৿])" + r"\s+".join(re.escape(w) for w in phrase.split())
                             + r"(?![\wऀ-৿])[\s,.]*", re.IGNORECASE)

        def put(match, mark=mark, phrase=phrase):
            changes.append(f"punct:{phrase}")
            if mark.startswith("\n"):
                return mark
            return mark + " "

        text = pattern.sub(put, text)
    for phrase, mark in END_PUNCT.get(lang, ()):
        pattern = re.compile(r"[\s,]+" + re.escape(phrase) + r"[\s.]*(?=$|\n)", re.IGNORECASE)
        text, n = pattern.subn(mark, text)
        if n:
            changes.append(f"punct:{phrase}")
    return text


def tidy(text: str, lang: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\s+([,.;:!?।])", r"\1", text)
    text = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", text)            # ", ," -> ","
    text = re.sub(r",\s*([.!?।])", r"\1", text)                     # ",." -> "."
    text = re.sub(r"([.!?।])(?:\s*[.,])+", r"\1", text)             # "?." -> "?"
    text = re.sub(r"([!?])(?:\s*\1)+", r"\1", text)                    # "??" -> "?"
    text = re.sub(r"([,;:!?।])(?=[^\s\d\n\"'”’)\]])", r"\1 ", text)  # a space after a mark
    text = re.sub(r"(?m)^[\s,;]+", "", text)
    text = text.strip(" ,;\t")
    # Sentence capitals (Latin and other cased scripts).
    def cap(match):
        return match.group(1) + match.group(2).upper()

    text = re.sub(r"(^|[.!?]\s+|\n)([a-zäöüß])", cap, text)
    if lang == "en":
        text = re.sub(r"(?<![\w'])i(?=(?:'(?:m|d|ll|ve|s))?(?![\w']))", "I", text)
    return text


def ensure_final_mark(text: str, lang: str, changes: list[str]) -> str:
    if lang not in ("en", "de") or not text or text.endswith("\n"):
        return text
    if text[-1] in ".!?:;…\"'”’)" or len(text.split()) < 3:
        return text
    changes.append("punct:final")
    return text + "."


def clean(text: str, language: str = "en", *, corrections=None, fillers: bool = True,
          spoken_marks: bool = True, numbers: bool = True, self_corrections: bool = True) -> Cleaned:
    lang = _lang(language)
    changes: list[str] = []
    original = text or ""
    text = unicodedata.normalize("NFC", original).strip()
    if not text:
        return Cleaned("", [])
    text = apply_corrections(text, corrections, changes)
    if fillers:
        text = remove_fillers(text, lang, changes)
    text = collapse_repeats(text, lang, changes)
    if self_corrections:
        text = resolve_self_corrections(text, lang, changes)
    if spoken_marks:
        text = spoken_punctuation(text, lang, changes)
    if numbers and lang == "en":
        text = numbers_to_digits(text, changes=changes)
    text = tidy(text, lang)
    text = ensure_final_mark(text, lang, changes)
    return Cleaned(text, changes)


# ------------------------------------------------------------------ metrics

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९০১২৩৪৫৬৭৮৯", "01234567890123456789")


def normalize_for_score(text: str, language: str = "en") -> list[str]:
    """Words for WER/CER: case, punctuation and number spelling do not count."""
    lang = _lang(language)
    text = unicodedata.normalize("NFC", text or "").casefold().translate(_DEVANAGARI_DIGITS)
    text = text.replace("%", " percent ").replace("-", " ")
    if lang == "en" or re.search(r"[a-z]", text):
        text = numbers_to_digits(text, minimum=0).replace("%", " percent ")
    cleaned = []
    for ch in text:
        cat = unicodedata.category(ch)
        cleaned.append(" " if cat[0] in "PSZ" or ch in "।॥" else ch)
    return "".join(cleaned).split()

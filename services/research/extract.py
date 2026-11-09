"""Main-content extraction: from a web page to clean, Markdown-ish text.

Readability-style, standard library only (``html.parser``):

1. **Parse** into a small tree, closing ``<p>`` / ``<li>`` / table cells the
   way browsers do. Metadata is collected on the way: ``<title>``,
   ``og:title``, ``og:site_name``, description, canonical link, ``<html lang>``,
   publication dates (``article:published_time`` and friends,
   ``itemprop=datePublished``, JSON-LD ``datePublished``, the first
   ``<time datetime>``).
2. **Drop** what is never content: scripts, styles, forms, buttons, iframes,
   media, ``nav``, ``aside``, ``header`` / ``footer`` outside the article,
   hidden elements, ARIA landmarks for navigation / banners / sidebars, and
   elements whose class or id says cookie banner, share bar, newsletter,
   related links, ads, sidebar, breadcrumbs ... unless it also says content
   (``article``, ``post``, ``entry``, ``answer`` ...).
3. **Score** paragraph-like blocks (text length + commas, Readability's
   formula), credit their parent fully and grandparent by half, weight by
   tag and class, discount by link density. The best container wins; it is
   widened to its parent while the parent holds substantially more good text
   (a forum thread is many posts, not one).
4. **Render** headings (``#``), paragraphs, lists (``-`` / ``1.``), tables
   (``| a | b |``), quotes (``>``) and code (fenced) as text.
5. **Clean** lines: entities decoded, whitespace normalised, zero-width
   characters removed, boilerplate lines ("Accept cookies", "Subscribe to
   our newsletter", "Share on Facebook", "Advertisement", ...) and exact
   repeats dropped.

The language comes from ``<html lang>`` or, failing that, script and
stop-word counts. ``fetched_at`` is set by the caller.
"""
from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser

from .types import Page

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr"}
DROP = {"script", "style", "noscript", "template", "svg", "math", "iframe", "canvas", "form", "button",
        "input", "select", "textarea", "nav", "aside", "menu", "dialog", "object", "embed", "video",
        "audio", "picture", "map", "head", "label", "option"}
BLOCK = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
         "table", "thead", "tbody", "tfoot", "tr", "td", "th", "pre", "blockquote", "dl", "dt", "dd", "figure",
         "figcaption", "header", "footer", "hr", "br", "address", "details", "summary", "caption"}
PARAGRAPH = {"p", "pre", "td", "blockquote", "dd", "li"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# Opening one of these closes an open <p> (HTML's implied end tags).
CLOSES_P = BLOCK - {"br", "hr", "td", "th", "tr", "li", "dt", "dd", "thead", "tbody", "tfoot", "caption"}

NEGATIVE = re.compile(
    r"(?:^|[\s_-])(?:cookies?|consent|gdpr|banner|share|sharing|social|subscribe|subscription|newsletter|"
    r"promo|advert|advertisement|ads?|adsense|sponsor(?:ed)?|related|recommend(?:ed|ations)?|sidebar|"
    r"breadcrumbs?|masthead|navbar|nav|menu|popup|modal|signup|login|paywall|outbrain|taboola|footer|"
    r"header|toolbar|skip|print-only|author-bio|widget|disqus|comments?-(?:section|list|area|form)|"
    r"respond|pagination|pager)(?:[\s_-]|$)",
    re.IGNORECASE,
)
POSITIVE = re.compile(
    r"article|body|content|entry|main|page|post|text|blog|story|answer|question|message|thread|"
    r"markdown|prose|cooked|postbody|documentation|docs?-?content|section",
    re.IGNORECASE,
)
HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
DROP_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog", "alert",
              "menu", "menubar", "toolbar", "alertdialog"}

BOILERPLATE = re.compile(
    r"\b(?:accept (?:all )?cookies|cookie (?:policy|settings|preferences)|we use cookies|this (?:site|website) uses cookies|"
    r"manage (?:cookies|consent|preferences)|subscribe(?: now| to (?:our|the))?|sign up (?:for|to)|newsletter|"
    r"share (?:this|on|via)|follow us|all rights reserved|copyright ©|advertisement|sponsored content|"
    r"read more|related (?:articles|stories|posts)|you may also like|recommended for you|click here|"
    r"log ?in to|sign in to|skip to (?:main )?content|enable javascript|javascript is (?:disabled|required)|"
    r"your browser (?:does not|doesn't) support|download (?:our|the) app|privacy policy|terms of (?:use|service)|"
    r"back to top|print this|email this|loading\.\.\.|trending now|most read|most popular)\b",
    re.IGNORECASE,
)
BOILER_EXACT = {"share", "tweet", "email", "print", "facebook", "twitter", "linkedin", "whatsapp", "reddit",
                "pinterest", "menu", "search", "home", "close", "advertisement", "ad", "comments", "reply",
                "next", "previous", "more", "top", "x", "quote", "like", "report", "permalink", "flag",
                "edit", "follow", "copy", "link", "on"}
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
DATE_META = ("article:published_time", "og:published_time", "og:article:published_time",
             "datepublished", "date", "pubdate", "publishdate", "publish_date", "publication_date",
             "dc.date", "dc.date.issued", "dcterms.created", "dcterms.date", "sailthru.date",
             "parsely-pub-date", "article.published", "cxenseparse:recs:publishtime")
MODIFIED_META = ("article:modified_time", "og:updated_time", "datemodified", "last-modified", "dcterms.modified")


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: Node | None = None

    def classes(self) -> str:
        return f"{self.attrs.get('class', '')} {self.attrs.get('id', '')}".strip()


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack: list[Node] = [self.root]
        self.meta: dict[str, str] = {}
        self.title = ""
        self.lang = ""
        self.canonical = ""
        self.jsonld: list[str] = []
        self.time_datetime = ""
        self._in_title = False
        self._in_jsonld = False
        self._buffer: list[str] = []

    # ------------------------------------------------------------ helpers
    def _current(self) -> Node:
        return self.stack[-1]

    def _close(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def _open_in_scope(self, tag: str, stop: set[str]) -> bool:
        for node in reversed(self.stack[1:]):
            if node.tag == tag:
                return True
            if node.tag in stop:
                return False
        return False

    # ------------------------------------------------------------- events
    def handle_starttag(self, tag: str, attrs_list) -> None:
        tag = tag.lower()
        attrs = {k.lower(): (v or "") for k, v in attrs_list}
        if tag == "html" and attrs.get("lang"):
            self.lang = attrs["lang"]
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or
                   attrs.get("http-equiv") or "").lower()
            if key and attrs.get("content") and key not in self.meta:
                self.meta[key] = attrs["content"]
        if tag == "link" and "canonical" in attrs.get("rel", "").lower() and attrs.get("href"):
            self.canonical = self.canonical or attrs["href"]
        if tag == "time" and attrs.get("datetime") and not self.time_datetime:
            self.time_datetime = attrs["datetime"]
        if tag == "title" and not self.title:
            self._in_title = True
            self._buffer = []
        if tag == "script" and "ld+json" in attrs.get("type", "").lower():
            self._in_jsonld = True
            self._buffer = []
        # Implied end tags.
        if tag in CLOSES_P and self._open_in_scope("p", {"div", "td", "th", "li", "blockquote", "article",
                                                          "section", "main", "body"}):
            self._close("p")
        if tag == "li" and self._open_in_scope("li", {"ul", "ol"}):
            self._close("li")
        if tag in ("dt", "dd"):
            for other in ("dt", "dd"):
                if self._open_in_scope(other, {"dl"}):
                    self._close(other)
        if tag in ("td", "th"):
            for other in ("td", "th"):
                if self._open_in_scope(other, {"tr", "table"}):
                    self._close(other)
        if tag == "tr" and self._open_in_scope("tr", {"table"}):
            self._close("tr")
        if tag == "option" and self._open_in_scope("option", {"select"}):
            self._close("option")
        node = Node(tag, attrs, parent=self._current())
        self._current().children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs_list) -> None:
        self.handle_starttag(tag, attrs_list)
        if tag.lower() not in VOID:
            self._close(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = " ".join("".join(self._buffer).split())
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld.append("".join(self._buffer))
        if tag == "p" and not self._open_in_scope("p", set()):
            return
        self._close(tag)

    def handle_data(self, data: str) -> None:
        if self._in_title or self._in_jsonld:
            self._buffer.append(data)
        self._current().children.append(data)


# ----------------------------------------------------------------- pruning
def _droppable(node: Node, inside_article: bool) -> bool:
    tag = node.tag
    if tag in DROP:
        return True
    if tag in ("header", "footer") and not inside_article:
        return True
    attrs = node.attrs
    if "hidden" in attrs or attrs.get("aria-hidden", "").lower() == "true":
        return True
    if HIDDEN_STYLE.search(attrs.get("style", "")):
        return True
    if attrs.get("role", "").lower() in DROP_ROLES:
        return True
    if tag in ("html", "body", "main", "article", "#root"):
        return False
    marks = node.classes()
    if marks and NEGATIVE.search(marks) and not POSITIVE.search(marks):
        return True
    return False


def _prune(node: Node, inside_article: bool = False) -> None:
    kept = []
    inside = inside_article or node.tag in ("article", "main") or node.attrs.get("role") == "main" \
        or node.attrs.get("itemprop", "").lower() == "articlebody"
    for child in node.children:
        if isinstance(child, Node):
            if _droppable(child, inside):
                continue
            _prune(child, inside)
        kept.append(child)
    node.children = kept


# ----------------------------------------------------------------- measuring
def _text(node: Node | str) -> str:
    # Memoised on the node: scoring asks for the same subtree's text many
    # times. Safe because the tree is not changed after pruning.
    if isinstance(node, str):
        return node
    cached = node.__dict__.get("_t")
    if cached is not None:
        return cached
    value = "\n" if node.tag == "br" else "".join(_text(child) for child in node.children)
    node.__dict__["_t"] = value
    return value


def _clean_inline(text: str) -> str:
    return " ".join(text.translate(ZERO_WIDTH).replace("\xa0", " ").split())


def _link_text_len(node: Node) -> int:
    cached = node.__dict__.get("_l")
    if cached is not None:
        return cached
    total = 0
    for child in node.children:
        if isinstance(child, Node):
            total += len(_clean_inline(_text(child))) if child.tag == "a" else _link_text_len(child)
    node.__dict__["_l"] = total
    return total


def link_density(node: Node) -> float:
    length = len(_clean_inline(_text(node)))
    return _link_text_len(node) / length if length else 0.0


def _has_block_child(node: Node) -> bool:
    return any(isinstance(c, Node) and c.tag in BLOCK for c in node.children)


def _iter(node: Node):
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from _iter(child)


def _paragraphs(root: Node) -> list[Node]:
    """Paragraph-like blocks: <p>/<pre>/<td>/... and leaf <div>s holding text."""
    out = []
    for node in _iter(root):
        if node.tag in PARAGRAPH or (node.tag in ("div", "section") and not _has_block_child(node)):
            if node.tag == "li" and _has_block_child(node):
                continue
            out.append(node)
    return out


def _class_weight(node: Node) -> float:
    marks = node.classes()
    weight = 0.0
    if marks:
        if NEGATIVE.search(marks):
            weight -= 25
        if POSITIVE.search(marks):
            weight += 25
    return weight


TAG_SCORE = {"div": 5, "article": 10, "main": 10, "section": 3, "pre": 3, "td": 3, "blockquote": 3,
             "address": -3, "ol": -3, "ul": -3, "dl": -3, "dd": -3, "dt": -3, "li": -3,
             "h1": -5, "h2": -5, "h3": -5, "h4": -5, "h5": -5, "h6": -5, "th": -5}


def _good_text(node: Node) -> int:
    """Characters of real prose under ``node`` (paragraphs >= 25 chars, few links)."""
    total = 0
    for para in _paragraphs(node):
        text = _clean_inline(_text(para))
        if len(text) >= 25 and link_density(para) < 0.35:
            total += len(text)
    return total


def best_container(body: Node) -> Node:
    scores: dict[int, float] = {}
    nodes: dict[int, Node] = {}
    for para in _paragraphs(body):
        text = _clean_inline(_text(para))
        if len(text) < 25:
            continue
        points = 1 + text.count(",") + text.count("，") + min(len(text) / 100, 3)
        parent = para.parent
        grand = parent.parent if parent is not None else None
        for ancestor, share in ((parent, 1.0), (grand, 0.5)):
            if ancestor is None or ancestor.tag == "#root":
                continue
            key = id(ancestor)
            if key not in scores:
                nodes[key] = ancestor
                scores[key] = TAG_SCORE.get(ancestor.tag, 0) + _class_weight(ancestor)
            scores[key] += points * share
    if not scores:
        return body
    ranked = sorted(((score * (1 - link_density(nodes[key])), key) for key, score in scores.items()), reverse=True)
    top = nodes[ranked[0][1]]
    # Widen to an ancestor that holds substantially more good prose (a forum
    # thread is many posts, an article may be split into sections). Every
    # ancestor is considered, not just the parent: post-content -> post ->
    # thread adds nothing at the first step and everything at the second.
    best, best_text = top, _good_text(top)
    ancestor = top.parent
    while ancestor is not None and ancestor.tag not in ("#root", "html", "body"):
        text = _good_text(ancestor)
        if text and best_text < 0.7 * text and link_density(ancestor) < 0.33:
            best, best_text = ancestor, text
        ancestor = ancestor.parent
    return best


# ----------------------------------------------------------------- rendering
def _render(node: Node, out: list[str], depth: int = 0, ordered: bool = False) -> None:
    tag = node.tag
    if tag in HEADINGS:
        text = _clean_inline(_text(node))
        if text:
            out.append("")
            out.append("#" * int(tag[1]) + " " + text)
        return
    if tag in ("ul", "ol"):
        out.append("")
        number = 0
        for child in node.children:
            if isinstance(child, Node) and child.tag == "li":
                number += 1
                inline_parts: list[str] = []
                nested: list[Node] = []
                for part in child.children:
                    if isinstance(part, Node) and part.tag in ("ul", "ol"):
                        nested.append(part)
                    else:
                        inline_parts.append(_text(part))
                text = _clean_inline("".join(inline_parts))
                marker = f"{number}." if tag == "ol" else "-"
                if text:
                    out.append("  " * depth + f"{marker} {text}")
                for sub in nested:
                    _render(sub, out, depth + 1)
            elif isinstance(child, Node):
                _render(child, out, depth)
        out.append("")
        return
    if tag == "table":
        _render_table(node, out)
        return
    if tag == "pre":
        code = _text(node).strip("\n")
        if code.strip():
            out.extend(["", "```", code.translate(ZERO_WIDTH), "```", ""])
        return
    if tag == "blockquote":
        inner: list[str] = []
        for child in node.children:
            if isinstance(child, Node):
                _render(child, inner, depth)
            else:
                inner.append(_clean_inline(child))
        lines = [line for line in "\n".join(inner).splitlines() if line.strip()]
        if lines:
            out.append("")
            out.extend("> " + line for line in lines)
            out.append("")
        return
    if tag == "dl":
        for child in node.children:
            if isinstance(child, Node) and child.tag in ("dt", "dd"):
                text = _clean_inline(_text(child))
                if text:
                    out.append(f"**{text}**" if child.tag == "dt" else text)
        return
    if tag in BLOCK and not _has_block_child(node) or tag == "#leaf":
        text = "\n".join(_clean_inline(part) for part in _text(node).split("\n"))
        text = "\n".join(line for line in text.splitlines() if line)
        if text:
            out.append("")
            out.append(text)
        return
    # A container: inline runs between blocks become their own paragraphs.
    run: list[str] = []

    def flush() -> None:
        text = _clean_inline("".join(run))
        run.clear()
        if text:
            out.append("")
            out.append(text)

    for child in node.children:
        if isinstance(child, Node) and (child.tag in BLOCK or _has_block_child(child)):
            flush()
            if child.tag == "br":
                continue
            _render(child, out, depth)
        else:
            run.append(_text(child))
    flush()


def _render_table(table: Node, out: list[str]) -> None:
    rows: list[tuple[list[str], bool]] = []
    for node in _iter(table):
        if node.tag == "tr":
            cells = [c for c in node.children if isinstance(c, Node) and c.tag in ("td", "th")]
            values = [_clean_inline(_text(c)).replace("|", "/") for c in cells]
            if any(values):
                rows.append((values, all(c.tag == "th" for c in cells)))
    if not rows:
        return
    width = max(len(r[0]) for r in rows)
    long_cells = sum(1 for r in rows for v in r[0] if len(v) > 200)
    out.append("")
    if width < 2 or long_cells > len(rows) // 2 or len(rows) > 60:
        for values, _ in rows[:60]:
            for value in values:
                if value:
                    out.append(value)
        out.append("")
        return
    for index, (values, header) in enumerate(rows):
        values = values + [""] * (width - len(values))
        out.append("| " + " | ".join(values) + " |")
        if index == 0:
            out.append("|" + " --- |" * width)
    out.append("")


# ----------------------------------------------------------------- cleaning
def clean_lines(text: str) -> str:
    out: list[str] = []
    seen: set[str] = set()
    in_code = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.strip() == "```":
            in_code = not in_code
            out.append("```")
            continue
        if in_code:
            out.append(line)
            continue
        line = html.unescape(line).translate(ZERO_WIDTH).replace("\xa0", " ")
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        bare = line.lstrip("#-*>|0123456789. ").strip().lower()
        if not re.search(r"\w", bare):
            if not line.startswith("|"):
                continue
        words = re.findall(r"\w+", bare)
        if bare in BOILER_EXACT or (words and len(words) <= 6 and all(w in BOILER_EXACT for w in words)):
            continue  # "Reply Quote Share", "Facebook Twitter Email"
        if len(line) < 160 and BOILERPLATE.search(line):
            continue
        key = line.lower()
        if key in seen and not line.startswith("|") and len(line) > 3:
            continue
        seen.add(key)
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    while out and out[0] == "":
        out.pop(0)
    return "\n".join(out)


# ----------------------------------------------------------------- metadata
def normalize_date(value: str) -> str:
    """ISO 8601 (date or datetime) from the common page formats; '' if unknown."""
    value = (value or "").strip()
    if not value:
        return ""
    candidate = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).isoformat()
    except ValueError:
        pass
    match = re.search(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?", value)
    if match:
        return match.group(0).replace(" ", "T")
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%B %d, %Y", "%d %B %Y", "%b %d, %Y", "%d %b %Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value[:40].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _jsonld_dates(blobs: list[str]) -> tuple[str, str]:
    published = modified = ""

    def walk(value) -> None:
        nonlocal published, modified
        if isinstance(value, dict):
            if not published and value.get("datePublished"):
                published = str(value["datePublished"])
            if not modified and value.get("dateModified"):
                modified = str(value["dateModified"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for blob in blobs:
        try:
            walk(json.loads(blob.strip()))
        except ValueError:
            continue
    return published, modified


_STOPWORDS = {
    "en": {"the", "and", "of", "to", "is", "in", "that", "it", "for", "was", "with", "as", "on"},
    "es": {"el", "la", "de", "que", "y", "en", "los", "se", "del", "las", "por", "una", "con"},
    "fr": {"le", "la", "de", "et", "les", "des", "est", "en", "une", "du", "que", "pour", "dans"},
    "de": {"der", "die", "und", "das", "ist", "nicht", "mit", "den", "von", "ein", "zu", "auf", "sich"},
    "pt": {"o", "a", "de", "que", "e", "do", "da", "em", "um", "para", "os", "uma", "não"},
    "it": {"il", "di", "che", "e", "la", "per", "un", "non", "una", "sono", "del", "della", "gli"},
    "nl": {"de", "het", "een", "en", "van", "is", "dat", "op", "niet", "zijn", "met", "voor", "ook"},
}


def detect_language(text: str, declared: str = "") -> str:
    declared = (declared or "").strip().lower().replace("_", "-")
    if declared:
        return declared.split("-")[0]
    sample = text[:4000]
    counts = {"hi": 0, "ru": 0, "ar": 0, "ja": 0, "zh": 0, "ko": 0, "el": 0, "latin": 0}
    for char in sample:
        code = ord(char)
        if 0x0900 <= code <= 0x097F:
            counts["hi"] += 1
        elif 0x0400 <= code <= 0x04FF:
            counts["ru"] += 1
        elif 0x0600 <= code <= 0x06FF:
            counts["ar"] += 1
        elif 0x3040 <= code <= 0x30FF:
            counts["ja"] += 1
        elif 0x4E00 <= code <= 0x9FFF:
            counts["zh"] += 1
        elif 0xAC00 <= code <= 0xD7AF:
            counts["ko"] += 1
        elif 0x0370 <= code <= 0x03FF:
            counts["el"] += 1
        elif char.isalpha():
            counts["latin"] += 1
    script, amount = max(counts.items(), key=lambda kv: kv[1])
    if amount == 0:
        return "und"
    if script == "zh" and counts["ja"] > amount * 0.1:
        return "ja"
    if script != "latin":
        return script
    words = re.findall(r"[a-zàâçéèêëîïôûùüÿñæœäöüßãõ]+", sample.lower())
    best, best_count = "und", 0
    for lang, stops in _STOPWORDS.items():
        count = sum(1 for w in words if w in stops)
        if count > best_count:
            best, best_count = lang, count
    return best if best_count >= 3 else "und"


# ----------------------------------------------------------------- entry point
def extract(markup: str, url: str = "") -> Page:
    """Clean main content plus metadata. Never raises."""
    builder = _TreeBuilder()
    try:
        builder.feed(markup or "")
        builder.close()
    except Exception:  # noqa: BLE001 - html.parser is forgiving; be sure anyway
        pass
    meta = builder.meta
    title = " ".join((meta.get("og:title") or meta.get("twitter:title") or builder.title or "").split())
    ld_published, ld_modified = _jsonld_dates(builder.jsonld)
    published = ""
    for key in DATE_META:
        if meta.get(key):
            published = meta[key]
            break
    published = published or ld_published or builder.time_datetime
    if not published:
        for key in MODIFIED_META:
            if meta.get(key):
                published = meta[key]
                break
        published = published or ld_modified

    root = builder.root
    _prune(root)
    body = next((n for n in _iter(root) if n.tag == "body"), root)
    container = best_container(body)
    lines: list[str] = []
    _render(container, lines)
    text = clean_lines("\n".join(lines))
    if len(text) < 200 and container is not body:
        # The scorer picked something tiny (a page of short blocks): use the body.
        lines = []
        _render(body, lines)
        whole = clean_lines("\n".join(lines))
        if len(whole) > len(text):
            text = whole
    text = unicodedata.normalize("NFC", text)
    return Page(
        url=url,
        title=title,
        text=text,
        published=normalize_date(published),
        lang=detect_language(text, builder.lang),
        description=" ".join((meta.get("og:description") or meta.get("description") or "").split()),
        site_name=meta.get("og:site_name", ""),
        canonical=builder.canonical,
        fetched_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )

"""Gmail message decoding and composition, standard library only.

Reading: the Gmail API returns a message as a tree of MIME parts whose bodies
are base64url. ``read_message`` walks that tree, prefers ``text/plain``,
falls back to ``text/html`` turned into readable text, lists attachments by
name only, and caps the body so a small model never receives a whole
newsletter.

Writing: ``build_raw`` builds an RFC 2822 message with ``email.message`` and
returns the base64url ``raw`` string the Gmail API expects. Header values are
checked for CR/LF so a model-supplied subject or address can never add a
header (header injection). Replies carry ``In-Reply-To`` and ``References``
so Gmail threads them.

Nothing here logs or stores a message body.
"""
from __future__ import annotations

import base64
import binascii
import email.policy
import html
import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from ..base import ConnectorError

DEFAULT_BODY_CHARS = 4000
MAX_BODY_CHARS = 20000
MAX_SEND_CHARS = 100_000
MAX_RECIPIENTS = 50
_WALK_LIMIT = 200           # MIME parts visited per message (a bomb cannot spin)
_SPACE = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")
_ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


# --------------------------------------------------------------------- read
def b64url_decode(data: str | None) -> bytes:
    """Decode Gmail's base64url (padding optional). Bad input -> b''."""
    if not data:
        return b""
    text = str(data).strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text)
    except (binascii.Error, ValueError):
        return b""


def header_map(payload: dict | None) -> dict[str, str]:
    """Lower-cased header name -> first value."""
    out: dict[str, str] = {}
    for item in (payload or {}).get("headers") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").lower()
        if name and name not in out:
            out[name] = str(item.get("value") or "")
    return out


def _charset(part: dict) -> str:
    content_type = header_map(part).get("content-type", "")
    match = re.search(r"charset\s*=\s*\"?([A-Za-z0-9._-]+)", content_type)
    return match.group(1) if match else "utf-8"


def _decode_text(part: dict) -> str:
    raw = b64url_decode((part.get("body") or {}).get("data"))
    if not raw:
        return ""
    charset = _charset(part)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class _TextExtractor(HTMLParser):
    """HTML -> readable text: drops script/style/head, keeps line structure."""

    BLOCK = {"p", "div", "tr", "table", "section", "article", "header", "footer",
             "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol"}
    SKIP = {"script", "style", "head", "title", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._href: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._href.append(href if href.startswith(("http://", "https://")) else "")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "a" and self._href:
            href = self._href.pop()
            if href and len(href) <= 200:
                self.parts.append(f" ({href})")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:  # noqa: BLE001 - broken HTML still gives what was read
        pass
    return tidy("".join(parser.parts))


def tidy(text: str) -> str:
    lines = [_SPACE.sub(" ", line).strip() for line in (text or "").replace("\r\n", "\n").split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def _walk(payload: dict):
    stack = [payload or {}]
    seen = 0
    while stack and seen < _WALK_LIMIT:
        part = stack.pop(0)
        if not isinstance(part, dict):
            continue
        seen += 1
        yield part
        stack.extend(part.get("parts") or [])


def _is_attachment(part: dict) -> bool:
    if part.get("filename"):
        return True
    disposition = header_map(part).get("content-disposition", "").lower()
    return disposition.startswith("attachment")


def extract_body(payload: dict) -> tuple[str, str, list[dict]]:
    """``(text, kind, attachments)``; kind is ``plain``, ``html`` or ``none``."""
    plain: list[str] = []
    markup: list[str] = []
    attachments: list[dict] = []
    for part in _walk(payload):
        mime = str(part.get("mimeType") or "").lower()
        if _is_attachment(part):
            attachments.append({
                "filename": str(part.get("filename") or "(unnamed)")[:200],
                "mimeType": mime,
                "size": int((part.get("body") or {}).get("size") or 0),
            })
            continue
        if mime == "text/plain":
            text = _decode_text(part)
            if text.strip():
                plain.append(text)
        elif mime == "text/html":
            text = _decode_text(part)
            if text.strip():
                markup.append(text)
    if plain:
        return tidy("\n\n".join(plain)), "plain", attachments
    if markup:
        return html_to_text("\n".join(markup)), "html", attachments
    return "", "none", attachments


def cap(text: str, limit: int) -> tuple[str, bool]:
    limit = max(200, min(int(limit or DEFAULT_BODY_CHARS), MAX_BODY_CHARS))
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ", limit - 200)
    return (cut[:space] if space > 0 else cut), True


def short_date(message: dict, headers: dict[str, str] | None = None) -> str:
    """``YYYY-MM-DD HH:MM`` in local time, from internalDate or the Date header."""
    stamp = message.get("internalDate")
    try:
        if stamp:
            return datetime.fromtimestamp(int(stamp) / 1000).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    raw = (headers or header_map(message.get("payload"))).get("date", "")
    try:
        return parsedate_to_datetime(raw).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, IndexError):
        return raw[:40]


def summarize(message: dict) -> dict:
    """The compact view a small model can work with."""
    headers = header_map(message.get("payload"))
    labels = [str(label) for label in message.get("labelIds") or []]
    return {
        "id": str(message.get("id") or ""),
        "threadId": str(message.get("threadId") or ""),
        "from": headers.get("from", "")[:200],
        "to": headers.get("to", "")[:300],
        "date": short_date(message, headers),
        "subject": headers.get("subject", "")[:300] or "(no subject)",
        "snippet": html.unescape(str(message.get("snippet") or ""))[:300],
        "labels": labels,
        "unread": "UNREAD" in labels,
    }


def read_message(message: dict, max_chars: int = DEFAULT_BODY_CHARS) -> dict:
    """Summary + decoded, capped body + attachment names."""
    view = summarize(message)
    headers = header_map(message.get("payload"))
    body, kind, attachments = extract_body(message.get("payload") or {})
    body, truncated = cap(body, max_chars)
    view.update({
        "cc": headers.get("cc", "")[:300],
        "body": body,
        "bodyKind": kind,
        "truncated": truncated,
        "attachments": attachments[:20],
        "messageIdHeader": headers.get("message-id", ""),
        "references": headers.get("references", ""),
    })
    return view


# -------------------------------------------------------------------- write
def _clean_header(value, field: str, limit: int = 998) -> str:
    text = str(value or "")
    if "\r" in text or "\n" in text:
        raise ConnectorError(f"{field} must be a single line")
    return text.strip()[:limit]


def clean_addresses(value, field: str) -> str:
    """Comma-separated addresses -> a validated header value ('' when empty)."""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(item) for item in value)
    text = _clean_header(value, field)
    if not text:
        return ""
    pairs = getaddresses([text])
    good = []
    for name, address in pairs:
        if not address:
            continue
        if not _ADDRESS.fullmatch(address):
            raise ConnectorError(f"{field}: {address!r} is not an email address")
        good.append(f"{name} <{address}>" if name else address)
    if not good:
        raise ConnectorError(f"{field} has no email address")
    if len(good) > MAX_RECIPIENTS:
        raise ConnectorError(f"{field} has more than {MAX_RECIPIENTS} addresses")
    return ", ".join(good)


def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()


def build_raw(
    *,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    in_reply_to: str = "",
    references: str = "",
    sender: str = "",
) -> str:
    """RFC 2822 text/plain message as Gmail's base64url ``raw``."""
    to_value = clean_addresses(to, "to")
    if not to_value:
        raise ConnectorError("to is required")
    text = str(body or "")
    if not text.strip():
        raise ConnectorError("body is required")
    if len(text) > MAX_SEND_CHARS:
        raise ConnectorError(f"body is longer than {MAX_SEND_CHARS} characters")
    message = EmailMessage(policy=email.policy.SMTP)
    if sender:
        message["From"] = clean_addresses(sender, "from")
    message["To"] = to_value
    cc_value = clean_addresses(cc, "cc")
    bcc_value = clean_addresses(bcc, "bcc")
    if cc_value:
        message["Cc"] = cc_value
    if bcc_value:
        message["Bcc"] = bcc_value
    message["Subject"] = _clean_header(subject, "subject", 300)
    reply = _clean_header(in_reply_to, "in_reply_to")
    if reply:
        message["In-Reply-To"] = reply
        refs = _clean_header(references, "references")
        message["References"] = f"{refs} {reply}".strip() if reply not in refs else refs
    message.set_content(text)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

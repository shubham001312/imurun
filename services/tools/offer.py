"""Which connector tools to put in the prompt (docs/connectors/REQUESTS.md L5).

With every service connected the catalogue holds 40+ ``<service>_<verb>``
tools. A small model given all of them picks wrong ones or copies the
example; the prompt also grows by a few thousand tokens. So connector tools
(categories ``connector-read`` / ``connector-write``) are offered **per
service**, only when the question or the recent conversation points at that
service - "my inbox" offers the Gmail tools, "open an issue" the GitHub ones.

Hiding is about the prompt only: every registered tool still runs when the
model names it (aliases in ``calls.ALIASES`` map ``send_email`` to
``gmail_send``), and the prompt tells the model which connected services have
hidden tools, so it can still reach them. Built-in tools (files, shell, web,
``run_agent``) and MCP tools are never hidden here.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

CONNECTOR_CATEGORIES = frozenset({"connector-read", "connector-write"})

# Words that point at a service. Matched on word boundaries, case-insensitive.
SERVICE_HINTS: dict[str, tuple[str, ...]] = {
    "gmail": ("gmail", "e-?mails?", "mail", "mails", "inbox", "unread", "newsletters?", "drafts?",
              "attachments?", "sender", "subject line", "reply to", "messages? from", "forward(?:ed)? to"),
    "github": ("github", "repo", "repos", "repositor(?:y|ies)", "issues?", "pull requests?", "prs?",
               "commits?", "branch(?:es)?", "merge", "release notes", "readme"),
    "slack": ("slack", "channels?", "dm", "dms", "workspace chat", "post (?:a |the )?message",
              "thread", "standup", "team chat"),
    "vercel": ("vercel", "deploy(?:s|ed|ment|ments)?", "preview url", "production build", "redeploy"),
    "render": ("render\\.com", "render service", "render", "web service logs", "redeploy",
               "deploy(?:s|ed|ment|ments)?"),
    "cloudflare": ("cloudflare", "dns", "domain", "zone", "workers?", "cache purge", "purge the cache",
                   "cname", "a record", "txt record", "nameservers?"),
    "supabase": ("supabase", "database", "db", "table", "tables", "rows?", "sql", "postgres", "query the"),
    "canva": ("canva", "designs?", "poster", "flyer", "thumbnail", "slides? deck", "brand kit"),
}

_PATTERNS = {service: re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)
             for service, words in SERVICE_HINTS.items()}

# Below this many connector tools nothing is hidden: the prompt is small anyway.
SMALL_CATALOGUE = 8
# How many recent messages count as "the conversation points at a service".
HISTORY_MESSAGES = 6
HISTORY_CHARS = 4000


def service_of(tool: Any) -> str:
    """The service a connector tool belongs to (``gmail_send`` -> ``gmail``)."""
    ctx = getattr(tool, "ctx", None)
    cid = getattr(ctx, "id", "") if ctx is not None else ""
    if cid:
        return str(cid)
    name = str(getattr(tool, "name", "") or "")
    return name.split("_", 1)[0] if "_" in name else name


def relevant_services(text: str, known: Iterable[str]) -> set[str]:
    """Services the text points at. Unknown services (no hint list) are
    matched on their own name only."""
    text = text or ""
    found: set[str] = set()
    for service in known:
        pattern = _PATTERNS.get(service)
        if pattern is None:
            pattern = re.compile(r"\b" + re.escape(service) + r"\b", re.IGNORECASE)
        if pattern.search(text):
            found.add(service)
    return found


def conversation_text(prompt: str, history: list[dict] | None) -> str:
    parts = []
    for message in list(history or [])[-HISTORY_MESSAGES:]:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content:
            parts.append(content[-HISTORY_CHARS:])
    parts.append(prompt or "")
    return "\n".join(parts)


def select_tools(tools: list[Any], prompt: str, history: list[dict] | None = None,
                 *, used_services: Iterable[str] = ()) -> tuple[list[Any], list[str]]:
    """``(offered, hidden_services)``: connector tools of services the
    conversation does not point at are left out; the hidden services are
    named so the prompt can mention them."""
    connector = [t for t in tools if getattr(t, "category", "") in CONNECTOR_CATEGORIES]
    if len(connector) <= SMALL_CATALOGUE:
        return list(tools), []
    services = {service_of(t) for t in connector}
    wanted = relevant_services(conversation_text(prompt, history), services) | set(used_services)
    hidden = sorted(s for s in services if s not in wanted)
    if not hidden:
        return list(tools), []
    kept = [t for t in tools
            if getattr(t, "category", "") not in CONNECTOR_CATEGORIES or service_of(t) in wanted]
    return kept, hidden

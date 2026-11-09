"""Fakes for the research tests: a network that never leaves the process."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from services.research.net import Response

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "research"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def ok(body, content_type="text/html; charset=utf-8", status=200, url=""):
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
        content_type = "application/json"
    data = body.encode("utf-8") if isinstance(body, str) else body
    return Response(url, status, {"content-type": content_type}, data)


def fail(error="unreachable: ConnectError", status=0, url=""):
    return Response(url, status, {}, b"", error=error)


class FakeFetcher:
    """Routes by URL prefix (longest match wins). A route is a ``Response``, a
    callable ``(url) -> Response``, or a list of either served in turn."""

    def __init__(self, routes: dict | None = None, delay: dict | None = None) -> None:
        self.routes = dict(routes or {})
        self.delay = dict(delay or {})
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def get(self, url, *, headers=None, timeout=8.0, max_bytes=2 * 1024 * 1024, allow_private=False):
        with self._lock:
            self.calls.append(url)
        match = max((p for p in self.routes if url.startswith(p)), key=len, default=None)
        for prefix, seconds in self.delay.items():
            if url.startswith(prefix):
                time.sleep(min(seconds, timeout))
                if seconds >= timeout:
                    return fail("timed out", url=url)
        if match is None:
            return fail(f"no fake route for {url}", url=url)
        route = self.routes[match]
        if isinstance(route, list):
            with self._lock:
                route = route.pop(0) if len(route) > 1 else route[0]
        response = route(url) if callable(route) else route
        return Response(response.url or url, response.status, dict(response.headers), response.body,
                        response.error, response.truncated)

    def count(self, prefix: str) -> int:
        return sum(1 for c in self.calls if c.startswith(prefix))


class FakeModel:
    """A ModelClient with scripted replies; records every call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def generate(self, messages):
        self.calls.append([dict(m) for m in messages])
        if not self.replies:
            return ""
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply


class Events:
    def __init__(self):
        self.items: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def publish(self, kind, data):
        with self._lock:
            self.items.append((kind, dict(data)))

    def kinds(self):
        return [k for k, _ in self.items]

    def of(self, kind):
        return [d for k, d in self.items if k == kind]

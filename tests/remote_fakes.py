"""A fake OpenAI-compatible provider on 127.0.0.1 for the hosted-model tests.

It records every request (method, path, headers, body), accepts one key,
and can be told to answer like a particular provider: the model list as
``{"data": [...]}`` or a bare list (Together), no ``/models`` at all, a
redirect, a rate limit, a model that is gone, or a paginated list.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_MODELS = [
    {"id": "fake/chat-large", "owned_by": "fake", "context_length": 65536},
    {"id": "fake/chat-small", "owned_by": "fake", "context_window": 8192},
    {"id": "fake/text-embed-3", "owned_by": "fake"},
]


class FakeProvider:
    def __init__(self, key: str | None = "sk-fake-provider-key-0123456789", *, models=None,
                 reply=("Hello", " from", " the fake"), shape: str = "data") -> None:
        self.key = key
        self.models = list(DEFAULT_MODELS if models is None else models)
        self.reply = list(reply)
        self.shape = shape            # "data" | "list" | "none" (no /models)
        self.chat_status = 200        # e.g. 429, 404 to simulate errors
        self.chat_error = ""
        self.redirect_to = ""         # answer every request with a 302 here
        self.next_link = ""           # links.next on the first /models page
        # Failures served (one per chat POST, in order) before the normal reply:
        #   (status, {headers})            an HTTP error, e.g. (429, {"Retry-After": "0"})
        #   ("stream_error", "message")    a 200 stream whose first event is an error
        #   ("partial_error", "message")   one text chunk, then an error event
        self.chat_failures: list = []
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _send(self, code: int, payload, headers: dict | None = None) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _record(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                entry = {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": body}
                fake.requests.append(entry)
                return entry

            def _authorized(self) -> bool:
                if fake.key is None:
                    return True
                return self.headers.get("Authorization") == f"Bearer {fake.key}"

            def do_GET(self) -> None:  # noqa: N802
                self._record()
                if fake.redirect_to:
                    self._send(302, {}, {"Location": fake.redirect_to})
                    return
                if not self._authorized():
                    # OpenAI-style: the provider echoes part of the key back.
                    sent = (self.headers.get("Authorization") or "")[7:]
                    self._send(401, {"error": {"message": f"Incorrect API key provided: {sent}"}})
                    return
                if self.path.rstrip("/").endswith("/models") or "/models?" in self.path:
                    if fake.shape == "none":
                        self._send(404, {"error": "not found"})
                        return
                    if fake.shape == "list":
                        self._send(200, fake.models)
                        return
                    page = {"object": "list", "data": fake.models}
                    if fake.next_link and "page=2" not in self.path:
                        page["links"] = {"next": fake.next_link}
                    self._send(200, page)
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                entry = self._record()
                if fake.redirect_to:
                    self._send(302, {}, {"Location": fake.redirect_to})
                    return
                if not self._authorized():
                    self._send(401, {"error": {"message": "invalid key"}})
                    return
                if not self.path.endswith("/chat/completions"):
                    self._send(404, {"error": "not found"})
                    return
                if fake.chat_failures:
                    failure = fake.chat_failures.pop(0)
                    if isinstance(failure[0], int):
                        self._send(failure[0], {"error": {"message": fake.chat_error or "error"}}, failure[1])
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    if failure[0] == "partial_error":
                        first = {"choices": [{"delta": {"content": "Half"}}]}
                        self.wfile.write(f"data: {json.dumps(first)}\n\n".encode())
                    error = {"error": {"message": failure[1]}}
                    self.wfile.write(f"data: {json.dumps(error)}\n\n".encode())
                    self.wfile.flush()
                    return
                if fake.chat_status != 200:
                    self._send(fake.chat_status, {"error": {"message": fake.chat_error or "error"}})
                    return
                body = entry["body"] or {}
                if not body.get("stream"):
                    self._send(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b": keep-alive\n\n")
                for chunk in fake.reply:
                    event = {"choices": [{"delta": {"content": chunk}}]}
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                usage = {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": len(fake.reply)}}
                self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "FakeProvider":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def chat_requests(self) -> list[dict]:
        return [r for r in self.requests if r["path"].endswith("/chat/completions")]

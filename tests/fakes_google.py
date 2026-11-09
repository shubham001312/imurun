"""A fake Google (OAuth token endpoint + Gmail REST) for connector tests.

Not a test module itself (no ``test_`` prefix). Records every call without
the Authorization header value so tests can assert on paths and bodies.
"""
from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlsplit

from services.connectors.base import ConnectorError


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def message(mid: str, subject: str, sender: str = "Alice <alice@example.com>", *,
            body: str = "Hello there", html: str | None = None, labels=("INBOX", "UNREAD"),
            thread: str = "t1", internal: int = 1_700_000_000_000) -> dict:
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": "me@example.com"},
               {"name": "Subject", "value": subject}, {"name": "Date", "value": "Tue, 14 Nov 2023 22:13:20 +0000"},
               {"name": "Message-ID", "value": f"<{mid}@mail.example.com>"},
               {"name": "References", "value": "<root@mail.example.com>"}]
    parts = [{"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=UTF-8"}],
              "body": {"data": b64(body), "size": len(body)}}]
    if html is not None:
        parts = [{"mimeType": "text/html", "headers": [], "body": {"data": b64(html)}}]
    parts.append({"mimeType": "application/pdf", "filename": "invoice.pdf", "headers": [],
                  "body": {"attachmentId": "att1", "size": 1234}})
    return {"id": mid, "threadId": thread, "labelIds": list(labels), "snippet": "Hello &amp; welcome",
            "internalDate": str(internal),
            "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}}


class FakeGoogle:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.forms: list[tuple[str, dict]] = []
        self.valid_token = "access-1"
        self.messages = {"m1": message("m1", "Quarterly report"),
                         "m2": message("m2", "Lunch?", "Bob <bob@example.com>", labels=("INBOX",))}
        self.refresh_ok = True
        self.labels = [{"id": "INBOX", "name": "INBOX", "type": "system"},
                       {"id": "Label_7", "name": "Receipts", "type": "user"}]

    # --- OAuth ------------------------------------------------------------
    def post_form(self, url: str, data: dict) -> dict:
        self.forms.append((url, dict(data)))
        if "revoke" in url:
            return {}
        if data.get("grant_type") == "authorization_code":
            return {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3599,
                    "scope": "https://www.googleapis.com/auth/gmail.readonly "
                             "https://www.googleapis.com/auth/gmail.compose",
                    "token_type": "Bearer"}
        if data.get("grant_type") == "refresh_token":
            if not self.refresh_ok:
                raise ConnectorError("POST oauth2.googleapis.com answered 400: Token has been expired or revoked.")
            self.valid_token = "access-2"
            return {"access_token": "access-2", "expires_in": 3599}
        raise ConnectorError("unexpected form post")

    # --- Gmail REST --------------------------------------------------------
    def request(self, method, url, headers=None, json_body=None, *, json=None):
        body = json_body if json_body is not None else json
        parts = urlsplit(url)
        path = parts.path.replace("/gmail/v1/users/me", "")
        query = parse_qs(parts.query)
        self.calls.append((method, path, query, body))
        token = (headers or {}).get("Authorization", "")
        if token != f"Bearer {self.valid_token}":
            raise ConnectorError(f"{method} gmail.googleapis.com answered 401: Invalid Credentials")
        if path == "/profile":
            return {"emailAddress": "me@example.com"}
        if path == "/messages" and method == "GET":
            refs = [{"id": mid, "threadId": m["threadId"]} for mid, m in self.messages.items()]
            return {"messages": refs[: int(query.get("maxResults", ["10"])[0])], "resultSizeEstimate": len(refs)}
        if path.startswith("/messages/") and method == "GET":
            mid = path.split("/")[2]
            if mid not in self.messages:
                raise ConnectorError("GET gmail.googleapis.com answered 404: Requested entity was not found.")
            return self.messages[mid]
        if path == "/threads":
            return {"threads": [{"id": "t1", "snippet": "Hello"}]}
        if path.startswith("/threads/"):
            return {"id": "t1", "messages": [self.messages["m1"], self.messages["m2"]]}
        if path == "/labels":
            return {"labels": self.labels}
        if path == "/drafts":
            return {"id": "d1", "message": {"id": "m9"}}
        if path == "/messages/send":
            return {"id": "m10", "threadId": body.get("threadId", "t9")}
        if path.endswith("/modify"):
            mid = path.split("/")[2]
            labels = [l for l in self.messages[mid]["labelIds"] if l not in body["removeLabelIds"]]
            return {"id": mid, "labelIds": labels + body["addLabelIds"]}
        if path.endswith("/trash"):
            return {"id": path.split("/")[2], "labelIds": ["TRASH"]}
        raise ConnectorError(f"{method} {path} not faked")

    def sent_raw(self) -> str:
        for method, path, _q, body in reversed(self.calls):
            if path in ("/messages/send", "/drafts"):
                raw = body.get("raw") or body["message"]["raw"]
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        raise AssertionError("nothing sent")


def jsonable(value) -> str:
    return json.dumps(value)

"""Canva Connect connector: list designs, export a design (PDF/PNG/JPG...)."""
from __future__ import annotations

import time

from ..base import READ, WRITE, Capability, ConnectorError, require_token
from ._rest import BearerConnector, clamp, query_string

FORMATS = ("pdf", "png", "jpg", "gif", "pptx", "mp4")
EXPORT_WAIT = 20.0


class CanvaConnector(BearerConnector):
    id = "canva"
    label = "Canva"
    base = "https://api.canva.com/rest/v1"
    capabilities = (
        Capability("me", READ),
        Capability("list_designs", READ),
        # An export only renders an existing design into a file; it changes
        # nothing in the user's account, so it is a read.
        Capability("export_design", READ),
        Capability("get_export", READ),
        Capability("create_design", WRITE),
    )

    def __init__(self, transport, tokens, *, sleep=time.sleep, **kwargs) -> None:
        super().__init__(transport, tokens, **kwargs)
        self._sleep = sleep

    def execute(self, action: str, params: dict) -> dict:
        params = params or {}
        if action == "me":
            return self._request("GET", "/users/me")
        if action == "list_designs":
            query = query_string({"query": str(params.get("query") or "").strip()[:200],
                                  "continuation": str(params.get("continuation") or "").strip()[:500],
                                  "limit": clamp(params.get("limit"), 20, 1, 100)})
            return self._request("GET", f"/designs{query}")
        if action == "get_export":
            job = require_token(params.get("job_id", ""), "job_id")
            return self._request("GET", f"/exports/{job}")
        if action == "export_design":
            design = require_token(params.get("design_id", ""), "design_id")
            fmt = str(params.get("format") or "pdf").strip().lower()
            if fmt not in FORMATS:
                raise ConnectorError(f"format must be one of {', '.join(FORMATS)}")
            started = self._request("POST", "/exports", {"design_id": design, "format": {"type": fmt}})
            job = (started or {}).get("job") or {}
            deadline = time.monotonic() + float(params.get("wait") or EXPORT_WAIT)
            while job.get("status") == "in_progress" and time.monotonic() < deadline:
                self._sleep(1.5)
                job = (self._request("GET", f"/exports/{require_token(job.get('id', ''), 'job_id')}") or {}).get("job") or {}
            return {"job": job}
        if action == "create_design":
            design_type = require_token(params.get("design_type", "presentation"), "design_type")
            body = {"design_type": {"type": "preset", "name": design_type}}
            title = str(params.get("title") or "").strip()
            if title:
                body["title"] = title[:255]
            return self._request("POST", "/designs", body)
        raise ConnectorError(f"canva has no action {action!r}")

"""Tuned models the channel produced, and their place in the model catalogue.

``registry.json`` is the durable list. At server start (and after each
export) every entry whose GGUF still exists is added to
``services.model_manager.RECOMMENDED_MODELS`` (and ``FISH_COMPATIBLE_IDS``)
as a local model, so the existing ``GET /api/models`` lists it and the
existing ``POST /api/models/select`` can switch to it. The catalogue module
itself is not edited: entries are added at runtime (see docs/tuning/REQUESTS.md
for the request to give model_manager a proper ``register_local`` API).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .catalogue import parent_id

_LOCK = threading.Lock()


class Registry:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def entries(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [e for e in data.get("models", []) if isinstance(e, dict) and e.get("id")]

    def add(self, entry: dict) -> dict:
        with _LOCK:
            items = [e for e in self.entries() if e.get("id") != entry["id"]]
            entry = {**entry, "created_at": entry.get("created_at") or time.time()}
            items.append(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"models": items}, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        return entry

    def latest_adapter(self, parent: str) -> str:
        """Adapter dir of the newest accepted run for a base model ('' if none)."""
        for entry in sorted(self.entries(), key=lambda e: e.get("created_at", 0), reverse=True):
            if entry.get("parent") == parent and entry.get("adapter_dir") and Path(entry["adapter_dir"]).is_dir():
                return entry["adapter_dir"]
        return ""


def register(entry: dict, catalogue=None, compatible=None) -> bool:
    """Add one tuned model to the live catalogue. False if its file is gone."""
    path = Path(entry.get("path", ""))
    if not path.is_file():
        return False
    try:
        from services import model_manager as mm
    except Exception:  # noqa: BLE001 - catalogue unavailable: nothing to register into
        return False
    catalogue = mm.RECOMMENDED_MODELS if catalogue is None else catalogue
    compatible = mm.FISH_COMPATIBLE_IDS if compatible is None else compatible
    parent = catalogue.get(parent_id(entry["id"]))
    size = path.stat().st_size
    catalogue[entry["id"]] = mm.ModelInfo(
        id=entry["id"],
        name=entry.get("name") or f"{getattr(parent, 'name', entry['id'])} - tuned {entry.get('run_id', '')}",
        filename=path.name,
        size_bytes=size,
        size_gb=round(size / 1e9, 1),
        source="local",
        repo_id=None,
        description=entry.get("description") or "Fine-tuned locally by the IMURUN tuning channel",
        tags=["tuned", "local"],
        sources=[],
    )
    if parent_id(entry["id"]) in compatible:
        compatible.add(entry["id"])
    return True


def register_all(registry: Registry, catalogue=None, compatible=None) -> int:
    return sum(1 for entry in registry.entries() if register(entry, catalogue, compatible))

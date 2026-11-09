"""Scoped API clients for external tools.

A client record holds a name, a scope list and a hash of its bearer token. The
plaintext token is returned exactly once, at creation. Revoking removes the
record, so the token stops working immediately.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

ALLOWED_SCOPES = frozenset(
    {"chat", "sessions", "skills", "models", "connectors", "read-only", "admin"}
)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ClientStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, clients: list[dict]) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(clients, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _view(client: dict) -> dict:
        return {
            "id": client["id"],
            "name": client["name"],
            "scopes": list(client["scopes"]),
            "created_at": client["created_at"],
            "last_used_at": client.get("last_used_at"),
        }

    def list(self) -> list[dict]:
        with self._lock:
            return [self._view(client) for client in self._load()]

    def create(self, name: str, scopes: list[str]) -> dict:
        cleaned = (name or "").strip()
        if not cleaned:
            raise ValueError("a client name is required")
        if len(cleaned) > 80:
            raise ValueError("the client name cannot exceed 80 characters")
        wanted = [scope for scope in (scopes or ["chat"]) if scope in ALLOWED_SCOPES]
        if not wanted:
            raise ValueError("at least one valid scope is required")
        token = "imu_" + secrets.token_urlsafe(32)
        record = {
            "id": secrets.token_hex(8),
            "name": cleaned,
            "scopes": wanted,
            "token_hash": _hash(token),
            "created_at": time.time(),
            "last_used_at": None,
        }
        with self._lock:
            clients = self._load()
            clients.append(record)
            self._save(clients)
        view = self._view(record)
        view["token"] = token
        return view

    def revoke(self, client_id: str) -> bool:
        with self._lock:
            clients = self._load()
            kept = [client for client in clients if client.get("id") != client_id]
            if len(kept) == len(clients):
                return False
            self._save(kept)
            return True

    def authenticate(self, token: str) -> dict | None:
        if not token:
            return None
        digest = _hash(token)
        with self._lock:
            clients = self._load()
            for client in clients:
                if secrets.compare_digest(client.get("token_hash", ""), digest):
                    client["last_used_at"] = time.time()
                    self._save(clients)
                    return self._view(client)
        return None

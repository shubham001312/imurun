"""Notes/Memory Tool - Store and retrieve notes, facts, and memories."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult


class NotesTool(Tool):
    """Store and retrieve notes, facts, and memories."""

    name = "notes"
    description = "Store, retrieve, and manage personal notes and memories."
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Action to perform.",
                "enum": ["save", "get", "list", "search", "delete"],
            },
            "key": {
                "type": "string",
                "description": "Note key/identifier (for save, get, delete).",
            },
            "content": {
                "type": "string",
                "description": "Note content (for save).",
            },
            "query": {
                "type": "string",
                "description": "Search query (for search).",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags for the note (for save).",
            },
        },
        "required": ["action"],
    }
    requires_network = False

    def permission_level(self, args: dict[str, Any]) -> tuple[str, str]:
        """Saving or deleting memory asks (not in Auto): saved memory is read
        into every later prompt, so injected text must not plant it (review M2)."""
        if str(args.get("action") or "").lower() in ("save", "delete"):
            return "write", "saves to IMURUN's memory of you, used in later chats"
        return "allow", "reads IMURUN's memory"

    def __init__(self, storage_dir: str | None = None):
        if storage_dir is None:
            from ..paths import data_root  # honours IMURUN_DATA_ROOT

            storage_dir = str(data_root() / "notes")
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._index_file = self.storage_dir / "index.json"
        self._index: dict = {}
        self._load_index()

    def _load_index(self):
        if self._index_file.exists():
            try:
                with open(self._index_file, "r", encoding="utf-8") as f:
                    self._index = json.load(f)
            except Exception:
                self._index = {}

    def _save_index(self):
        try:
            tmp = self._index_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._index, f, indent=2)
            os.replace(tmp, self._index_file)
        except Exception:
            pass

    def available(self) -> bool:
        return True

    def run(self, args: dict[str, Any]) -> ToolResult:
        action = args.get("action")
        if not action:
            return ToolResult(ok=False, error="action is required", content="Please specify an action: save, get, list, search, or delete.")

        action = action.lower()
        if action == "save":
            return self._save(args)
        elif action == "get":
            return self._get(args)
        elif action == "list":
            return self._list(args)
        elif action == "search":
            return self._search(args)
        elif action == "delete":
            return self._delete(args)
        else:
            return ToolResult(ok=False, error=f"unknown action: {action}", content=f"Unknown action: {action}. Use save, get, list, search, or delete.")

    def _save(self, args: dict[str, Any]) -> ToolResult:
        key = (args.get("key") or "").strip()
        content = args.get("content", "")
        tags = args.get("tags", [])

        if not key:
            return ToolResult(ok=False, error="key is required", content="Please provide a key for the note.")
        if not content:
            return ToolResult(ok=False, error="content is required", content="Please provide content for the note.")

        with self._lock:
            note = {
                "key": key,
                "content": content,
                "tags": tags if isinstance(tags, list) else [],
                "created": time.time(),
                "updated": time.time(),
            }
            self._index[key] = note
            self._save_index()

        return ToolResult(
            ok=True,
            content=f"Note '{key}' saved successfully.",
            data={"key": key, "action": "saved"},
        )

    def _get(self, args: dict[str, Any]) -> ToolResult:
        key = (args.get("key") or "").strip()
        if not key:
            return ToolResult(ok=False, error="key is required", content="Please provide a key to retrieve.")

        with self._lock:
            note = self._index.get(key)
            if not note:
                return ToolResult(ok=False, error="not found", content=f"No note found with key: {key}")

            content = f"**{note['key']}**\n\n{note['content']}"
            if note.get("tags"):
                content += f"\n\nTags: {', '.join(note['tags'])}"
            from datetime import datetime
            created = datetime.fromtimestamp(note['created']).strftime("%Y-%m-%d %H:%M")
            updated = datetime.fromtimestamp(note['updated']).strftime("%Y-%m-%d %H:%M")
            content += f"\n\nCreated: {created} | Updated: {updated}"

            return ToolResult(ok=True, content=content, data=note)

    def _list(self, args: dict[str, Any]) -> ToolResult:
        with self._lock:
            if not self._index:
                return ToolResult(ok=True, content="No notes stored yet.", data={"notes": []})

            notes = []
            for key, note in sorted(self._index.items(), key=lambda x: x[1].get("updated", 0), reverse=True):
                preview = note["content"][:80] + ("..." if len(note["content"]) > 80 else "")
                tags = ", ".join(note.get("tags", [])) if note.get("tags") else ""
                from datetime import datetime
                updated = datetime.fromtimestamp(note.get("updated", 0)).strftime("%Y-%m-%d %H:%M")
                notes.append({
                    "key": note["key"],
                    "preview": preview,
                    "tags": tags,
                    "updated": updated,
                })

            lines = [f"Found {len(notes)} note(s):"]
            for n in notes:
                tag_str = f" [{n['tags']}]" if n['tags'] else ""
                lines.append(f"  • **{n['key']}**{tag_str} - {n['preview']} (updated: {n['updated']})")

            return ToolResult(ok=True, content="\n".join(lines), data={"notes": list(self._index.keys())})

    def _search(self, args: dict[str, Any]) -> ToolResult:
        query = (args.get("query") or "").strip().lower()
        if not query:
            return ToolResult(ok=False, error="query is required", content="Please provide a search query.")

        with self._lock:
            results = []
            for key, note in self._index.items():
                content_lower = note["content"].lower()
                key_lower = key.lower()
                tags_lower = " ".join(note.get("tags", [])).lower()

                if query in content_lower or query in key_lower or query in " ".join(note.get("tags", [])).lower():
                    from datetime import datetime
                    preview = note["content"][:100] + ("..." if len(note["content"]) > 100 else "")
                    updated = datetime.fromtimestamp(note.get("updated", 0)).strftime("%Y-%m-%d %H:%M")
                    tags = ", ".join(note.get("tags", [])) if note.get("tags") else ""
                    results.append({
                        "key": key,
                        "preview": preview,
                        "tags": tags,
                        "updated": updated,
                    })

            if not results:
                return ToolResult(ok=True, content=f"No notes found matching '{query}'.", data={"results": []})

            lines = [f"Found {len(results)} note(s) matching '{query}':"]
            for r in results:
                tag_str = f" [{r['tags']}]" if r['tags'] else ""
                lines.append(f"  • **{r['key']}**{tag_str} - {r['preview']} (updated: {r['updated']})")

            return ToolResult(ok=True, content="\n".join(lines), data={"results": [r["key"] for r in results]})

    def _delete(self, args: dict[str, Any]) -> ToolResult:
        key = (args.get("key") or "").strip()
        if not key:
            return ToolResult(ok=False, error="key is required", content="Please provide a key to delete.")

        with self._lock:
            if key not in self._index:
                return ToolResult(ok=False, error="not found", content=f"No note found with key: {key}")
            del self._index[key]
            self._save_index()

        return ToolResult(ok=True, content=f"Note '{key}' deleted.", data={"key": key, "action": "deleted"})


class FactsTool(Tool):
    """Store and retrieve structured facts (key-value pairs)."""

    name = "facts"
    description = "Store and retrieve structured facts (key-value pairs with optional categories)."
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Action to perform.",
                "enum": ["set", "get", "list", "delete"],
            },
            "key": {
                "type": "string",
                "description": "Fact key.",
            },
            "value": {
                "type": "string",
                "description": "Fact value (for set).",
            },
            "category": {
                "type": "string",
                "description": "Optional category for grouping facts.",
            },
        },
        "required": ["action"],
    }
    requires_network = False

    def permission_level(self, args: dict[str, Any]) -> tuple[str, str]:
        """Saving or deleting memory asks (not in Auto): saved memory is read
        into every later prompt, so injected text must not plant it (review M2)."""
        if str(args.get("action") or "").lower() in ("set", "delete"):
            return "write", "saves to IMURUN's memory of you, used in later chats"
        return "allow", "reads IMURUN's memory"

    def __init__(self, storage_dir: str | None = None):
        if storage_dir is None:
            from ..paths import data_root  # honours IMURUN_DATA_ROOT

            storage_dir = str(data_root() / "facts")
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._file = self.storage_dir / "facts.json"
        self._data: dict = {}
        self._load()

    def _load(self):
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def _save(self):
        try:
            tmp = self._file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._file)
        except Exception:
            pass

    def available(self) -> bool:
        return True

    def run(self, args: dict[str, Any]) -> ToolResult:
        action = args.get("action")
        if not action:
            return ToolResult(ok=False, error="action is required", content="Please specify an action: set, get, list, or delete.")

        action = action.lower()
        if action == "set":
            return self._set(args)
        elif action == "get":
            return self._get(args)
        elif action == "list":
            return self._list(args)
        elif action == "delete":
            return self._delete(args)
        else:
            return ToolResult(ok=False, error=f"unknown action: {action}", content=f"Unknown action: {action}. Use set, get, list, or delete.")

    def _set(self, args: dict[str, Any]) -> ToolResult:
        key = (args.get("key") or "").strip()
        value = args.get("value", "")
        category = (args.get("category") or "general").strip()

        if not key:
            return ToolResult(ok=False, error="key is required", content="Please provide a key.")
        if value == "":
            return ToolResult(ok=False, error="value is required", content="Please provide a value.")

        with self._lock:
            if category not in self._data:
                self._data[category] = {}
            self._data[category][key] = {"value": value, "updated": time.time()}
            self._save()

        return ToolResult(ok=True, content=f"Fact '{key}' set in category '{category}'.", data={"key": key, "category": category, "action": "set"})

    def _get(self, args: dict[str, Any]) -> ToolResult:
        key = (args.get("key") or "").strip()
        category = (args.get("category") or "general").strip()

        if not key:
            return ToolResult(ok=False, error="key is required", content="Please provide a key to retrieve.")

        with self._lock:
            category_data = self._data.get(category, {})
            fact = category_data.get(key)
            if not fact:
                return ToolResult(ok=False, error="not found", content=f"No fact found with key: {key} in category: {category}")

            from datetime import datetime
            updated = datetime.fromtimestamp(fact["updated"]).strftime("%Y-%m-%d %H:%M")
            content = f"**{category}.{key}**\n\n{fact['value']}\n\nUpdated: {updated}"
            return ToolResult(ok=True, content=content, data={"key": key, "category": category, "value": fact["value"], "updated": updated})

    def _list(self, args: dict[str, Any]) -> ToolResult:
        category = (args.get("category") or "").strip()

        with self._lock:
            if category:
                if category not in self._data:
                    return ToolResult(ok=True, content=f"No facts in category '{category}'.", data={"facts": []})
                facts = self._data[category]
            else:
                facts = {}
                for cat, cat_facts in self._data.items():
                    for k, v in cat_facts.items():
                        facts[f"{cat}.{k}"] = v

            if not facts:
                return ToolResult(ok=True, content="No facts stored.", data={"facts": {}})

            lines = [f"Found {len(facts)} fact(s):"]
            for k, v in sorted(facts.items()):
                lines.append(f"  • **{k}**: {v['value'][:80]}{'...' if len(v['value']) > 80 else ''}")

            return ToolResult(ok=True, content="\n".join(lines), data={"facts": {k: v["value"] for k, v in facts.items()}})

    def _delete(self, args: dict[str, Any]) -> ToolResult:
        key = (args.get("key") or "").strip()
        category = (args.get("category") or "general").strip()

        if not key:
            return ToolResult(ok=False, error="key is required", content="Please provide a key to delete.")

        with self._lock:
            if category not in self._data or key not in self._data[category]:
                return ToolResult(ok=False, error="not found", content=f"No fact found with key: {key} in category: {category}")
            del self._data[category][key]
            if not self._data[category]:
                del self._data[category]
            self._save()

        return ToolResult(ok=True, content=f"Fact '{key}' deleted from category '{category}'.", data={"key": key, "category": category, "action": "deleted"})
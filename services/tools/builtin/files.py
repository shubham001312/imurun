"""File and folder tools, confined to the workspace.

Every path goes through ``Workspace.resolve`` (see ``workspace.py``): a path
outside the workspace, a protected file (``.env``, ``.git``...) or a system
location is refused before anything is opened. The permission policy
(``permissions.py``) decides separately whether the call needs approval.

The tools never raise: every failure is a ``ToolResult`` with ``ok=False``
and a sentence the model can repeat to the user.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from ..base import Tool, ToolResult, failed
from ..workspace import Workspace, WorkspaceError, default_workspace

MAX_READ_CHARS = 200_000
DEFAULT_READ_CHARS = 20_000
MAX_WRITE_CHARS = 1_000_000
MAX_LIST_ENTRIES = 500


class _WorkspaceTool(Tool):
    """Holds the workspace. ``ToolRegistry.bind_workspace`` replaces it with
    the one the permission policy uses, so both always agree on the root."""

    def __init__(self, workspace: Workspace | None = None) -> None:
        self.workspace = workspace or Workspace(default_workspace)

    def _path(self, value: Any, kind: str) -> Path:
        return self.workspace.resolve(str(value or "."), kind=kind)

    def _rel(self, path: Path) -> str:
        return self.workspace.relative(path)


class FileReadTool(_WorkspaceTool):
    name = "file_read"
    category = "read"
    description = (
        "Read a text file in the workspace. Returns the text, capped in size; "
        "use offset to read further into a long file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, relative to the workspace."},
            "offset": {"type": "integer", "description": "Character offset to start at (default 0).",
                       "minimum": 0, "default": 0},
            "max_chars": {"type": "integer", "description": f"Characters to read (default {DEFAULT_READ_CHARS}).",
                          "minimum": 1, "maximum": MAX_READ_CHARS, "default": DEFAULT_READ_CHARS},
        },
        "required": ["path"],
    }

    def run(self, args: dict[str, Any]) -> ToolResult:
        try:
            path = self._path(args.get("path"), "file_read")
        except WorkspaceError as exc:
            return failed(self.name, str(exc))
        if not path.exists():
            return failed(self.name, f"File not found: {self._rel(path)}")
        if path.is_dir():
            return failed(self.name, f"{self._rel(path)} is a folder; use file_list.")
        limit = max(1, min(int(args.get("max_chars") or DEFAULT_READ_CHARS), MAX_READ_CHARS))
        offset = max(0, int(args.get("offset") or 0))
        try:
            with open(path, "rb") as handle:
                head = handle.read(4096)
            if b"\x00" in head:
                return failed(self.name, f"{self._rel(path)} looks like a binary file; it was not read.")
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(offset + limit + 1)
        except OSError as exc:
            return failed(self.name, f"Could not read {self._rel(path)}: {exc.strerror or exc}")
        chunk = text[offset: offset + limit]
        truncated = len(text) > offset + limit
        note = f"\n... [more text; call again with offset {offset + limit}]" if truncated else ""
        return ToolResult(
            ok=True,
            content=f"{self._rel(path)}:\n{chunk}{note}",
            data={"path": self._rel(path), "offset": offset, "chars": len(chunk), "truncated": truncated},
            source=self.name,
        )


class FileWriteTool(_WorkspaceTool):
    name = "file_write"
    category = "write"
    description = (
        "Create or change a text file in the workspace. mode is overwrite "
        "(default), create (fail if it exists) or append. Parent folders are created."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, relative to the workspace."},
            "content": {"type": "string", "description": "The text to write."},
            "mode": {"type": "string", "enum": ["overwrite", "create", "append"],
                     "description": "overwrite (default), create or append.", "default": "overwrite"},
        },
        "required": ["path"],
    }

    def run(self, args: dict[str, Any]) -> ToolResult:
        try:
            path = self._path(args.get("path"), "file_write")
        except WorkspaceError as exc:
            return failed(self.name, str(exc))
        content = str(args.get("content") or "")
        mode = str(args.get("mode") or "overwrite")
        mode = {"w": "overwrite", "a": "append", "x": "create"}.get(mode, mode)
        if mode not in ("overwrite", "create", "append"):
            return failed(self.name, "mode must be overwrite, create or append")
        if len(content) > MAX_WRITE_CHARS:
            return failed(self.name, f"The content is larger than {MAX_WRITE_CHARS} characters.")
        if path == self.workspace.root or path.is_dir():
            return failed(self.name, f"{self._rel(path)} is a folder, not a file.")
        existed = path.exists()
        if mode == "create" and existed:
            return failed(self.name, f"{self._rel(path)} already exists; use mode overwrite or append.")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with open(path, "a", encoding="utf-8", newline="") as handle:
                    handle.write(content)
            else:
                # Write to a temporary file first so a failure never leaves a
                # half-written file where a good one was.
                temporary = path.with_name(f".{path.name}.imurun.tmp")
                with open(temporary, "w", encoding="utf-8", newline="") as handle:
                    handle.write(content)
                os.replace(temporary, path)
        except OSError as exc:
            return failed(self.name, f"Could not write {self._rel(path)}: {exc.strerror or exc}")
        verb = "Appended" if mode == "append" else ("Overwrote" if existed else "Created")
        return ToolResult(
            ok=True,
            content=f"{verb} {self._rel(path)} ({len(content)} characters).",
            data={"path": self._rel(path), "mode": mode, "existed": existed,
                  "bytes": len(content.encode("utf-8"))},
            source=self.name,
        )


class FileListTool(_WorkspaceTool):
    name = "file_list"
    category = "read"
    description = (
        "List a folder in the workspace as a tree of names with sizes. "
        "depth 1 (default) shows one level; up to 5 levels."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Folder, relative to the workspace (default: the workspace)."},
            "depth": {"type": "integer", "description": "How many levels to show (1-5, default 1).",
                      "minimum": 1, "maximum": 5, "default": 1},
            "recursive": {"type": "boolean", "description": "Same as depth 5."},
        },
        "required": [],
    }

    def run(self, args: dict[str, Any]) -> ToolResult:
        try:
            root = self._path(args.get("path") or ".", "file_read")
        except WorkspaceError as exc:
            return failed(self.name, str(exc))
        if root == self.workspace.root:
            root.mkdir(parents=True, exist_ok=True)
        if not root.exists():
            return failed(self.name, f"Folder not found: {self._rel(root)}")
        if not root.is_dir():
            return failed(self.name, f"{self._rel(root)} is a file; use file_read.")
        depth = 5 if args.get("recursive") else max(1, min(int(args.get("depth") or 1), 5))
        entries: list[dict[str, Any]] = []
        lines = [f"{self._rel(root)}/"]
        truncated = self._walk(root, 1, depth, "", entries, lines)
        if not entries:
            lines.append("  (empty)")
        if truncated:
            lines.append(f"... [stopped at {MAX_LIST_ENTRIES} entries]")
        return ToolResult(
            ok=True,
            content="\n".join(lines),
            data={"path": self._rel(root), "entries": entries, "truncated": truncated},
            source=self.name,
        )

    def _walk(self, folder: Path, level: int, depth: int, indent: str,
              entries: list[dict[str, Any]], lines: list[str]) -> bool:
        try:
            children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            lines.append(f"{indent}  [cannot list: {exc.strerror or exc}]")
            return False
        for child in children:
            if len(entries) >= MAX_LIST_ENTRIES:
                return True
            if child.name.endswith(".imurun.tmp"):
                continue
            is_dir = child.is_dir()
            try:
                size = 0 if is_dir else child.stat().st_size
            except OSError:
                size = 0
            entries.append({"path": self._rel(child), "type": "dir" if is_dir else "file", "size": size})
            lines.append(f"{indent}  {child.name}/" if is_dir else f"{indent}  {child.name} ({size} B)")
            if is_dir and level < depth and not child.is_symlink():
                if self._walk(child, level + 1, depth, indent + "  ", entries, lines):
                    return True
        return False


class MakeDirTool(_WorkspaceTool):
    name = "make_dir"
    category = "write"
    description = "Create a folder (and any missing parents) in the workspace."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Folder path, relative to the workspace."}},
        "required": ["path"],
    }

    def run(self, args: dict[str, Any]) -> ToolResult:
        try:
            path = self._path(args.get("path"), "file_write")
        except WorkspaceError as exc:
            return failed(self.name, str(exc))
        if path.exists() and not path.is_dir():
            return failed(self.name, f"{self._rel(path)} exists and is a file.")
        existed = path.is_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return failed(self.name, f"Could not create {self._rel(path)}: {exc.strerror or exc}")
        return ToolResult(ok=True, content=f"{'Folder already existed' if existed else 'Created folder'}: {self._rel(path)}",
                          data={"path": self._rel(path), "existed": existed}, source=self.name)


class _TransferTool(_WorkspaceTool):
    category = "write"
    parameters = {
        "type": "object",
        "properties": {
            "src": {"type": "string", "description": "Source path, relative to the workspace."},
            "dst": {"type": "string", "description": "Destination path, relative to the workspace."},
            "overwrite": {"type": "boolean", "description": "Replace an existing destination (default false)."},
        },
        "required": ["src", "dst"],
    }
    verb = "Moved"

    def run(self, args: dict[str, Any]) -> ToolResult:
        try:
            src = self._path(args.get("src"), "file_read")
            dst = self._path(args.get("dst"), "file_write")
        except WorkspaceError as exc:
            return failed(self.name, str(exc))
        if not src.exists():
            return failed(self.name, f"Not found: {self._rel(src)}")
        if src == self.workspace.root:
            return failed(self.name, "The workspace folder itself cannot be moved or copied.")
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            if not args.get("overwrite"):
                return failed(self.name, f"{self._rel(dst)} already exists; set overwrite to true to replace it.")
            if dst.is_dir() != src.is_dir():
                return failed(self.name, "Cannot replace a file with a folder or a folder with a file.")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            self._transfer(src, dst)
        except OSError as exc:
            return failed(self.name, f"{self.verb[:-1]} failed: {exc.strerror or exc}")
        return ToolResult(ok=True, content=f"{self.verb} {self._rel(src)} to {self._rel(dst)}.",
                          data={"src": self._rel(src), "dst": self._rel(dst)}, source=self.name)

    def _transfer(self, src: Path, dst: Path) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class FileMoveTool(_TransferTool):
    name = "file_move"
    description = "Move or rename a file or folder inside the workspace."
    verb = "Moved"

    def _transfer(self, src: Path, dst: Path) -> None:
        if dst.exists() and dst.is_dir():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))


class FileCopyTool(_TransferTool):
    name = "file_copy"
    description = "Copy a file or folder inside the workspace."
    verb = "Copied"

    def _transfer(self, src: Path, dst: Path) -> None:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(src, dst)


class FileDeleteTool(_WorkspaceTool):
    name = "file_delete"
    category = "delete"
    description = (
        "Delete a file or folder in the workspace. A non-empty folder needs "
        "recursive true. Always asks the user unless the mode is allow-all."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path, relative to the workspace."},
            "recursive": {"type": "boolean", "description": "Delete a folder and everything in it."},
        },
        "required": ["path"],
    }

    def run(self, args: dict[str, Any]) -> ToolResult:
        try:
            path = self._path(args.get("path"), "file_delete")
        except WorkspaceError as exc:
            return failed(self.name, str(exc))
        if path == self.workspace.root:
            return failed(self.name, "The workspace folder itself cannot be deleted.")
        if not path.exists() and not path.is_symlink():
            return failed(self.name, f"Not found: {self._rel(path)}")
        try:
            if path.is_dir() and not path.is_symlink():
                if any(path.iterdir()):
                    if not args.get("recursive"):
                        return failed(self.name, f"{self._rel(path)} is not empty; set recursive to true.")
                    shutil.rmtree(path)
                else:
                    path.rmdir()
            else:
                path.unlink()
        except OSError as exc:
            return failed(self.name, f"Could not delete {self._rel(path)}: {exc.strerror or exc}")
        return ToolResult(ok=True, content=f"Deleted {self._rel(path)}.",
                          data={"path": self._rel(path)}, source=self.name)

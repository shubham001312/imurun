"""G-3 check_start() and G-4 check_action(): the code decides, not the model.

Every decision is one of allow / ask / deny with a reason and the rule that
produced it (V4: enforcement in code; V9: a denial always says why).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .limits import Limits
from .profile import MachineProfile

GB = 1024**3

SYSTEM_PREFIXES = (
    # Windows
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
    # Linux
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/sys",
    "/proc",
    "/dev",
    # macOS
    "/system",
    "/library",
    "/private/etc",
    "/private/var",
)

DANGEROUS_COMMANDS = (
    re.compile(r"rm\s+-[a-z]*r[a-z]*f?[a-z]*\s+/(\s|$)"),
    re.compile(r"mkfs(\.|\s|$)"),
    re.compile(r"dd\s+.*of=/dev/"),
    re.compile(r"format\s+[a-z]:"),
    re.compile(r"del\s+/[fsq].*[a-z]:\\", re.IGNORECASE),
    re.compile(r"rd\s+/s\s+/q\s+[a-z]:\\", re.IGNORECASE),
    re.compile(r"reg\s+delete", re.IGNORECASE),
    re.compile(r"bcdedit", re.IGNORECASE),
    re.compile(r"diskpart", re.IGNORECASE),
    re.compile(r"shutdown\s+/(s|r)", re.IGNORECASE),
    re.compile(r":\(\)\s*\{.*\};\s*:"),  # fork bomb
)


@dataclass(frozen=True)
class Decision:
    kind: str  # allow | ask | deny
    rule: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelRequest:
    model_bytes: int
    context_tokens: int = 8192
    threads: int = 1


def _norm(path: str) -> str:
    resolved = os.path.realpath(os.path.expanduser(path))
    return resolved.casefold() if os.name == "nt" else resolved


def _inside(path: str, root: str) -> bool:
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root + os.sep)


def _system_path(path: str) -> bool:
    candidate = _norm(path)
    return any(candidate == prefix or candidate.startswith(prefix + os.sep) for prefix in SYSTEM_PREFIXES)


def check_start(request: ModelRequest, limits: Limits, profile: MachineProfile) -> Decision:
    reasons: list[str] = []

    if request.model_bytes > profile.disk_free - limits.min_free_disk:
        return Decision(
            "deny",
            "G4-disk",
            (
                f"model needs {request.model_bytes // GB} GB, "
                f"only {profile.disk_free // GB} GB free (keeping {limits.min_free_disk // GB} GB)",
            ),
        )

    # FISH streams weights from disk, but the resident window must fit in RAM.
    if request.model_bytes > limits.max_model_ram * 2:
        return Decision(
            "deny",
            "G1-ram",
            (
                f"model is {request.model_bytes // GB} GB, more than twice the "
                f"{limits.max_model_ram // GB} GB RAM budget even with streaming",
            ),
        )
    if request.model_bytes > limits.max_model_ram:
        reasons.append(
            f"{request.model_bytes // GB} GB model exceeds the {limits.max_model_ram // GB} GB "
            f"{limits.mode} budget; needs full-resource opt-in (G1)"
        )

    if request.threads > limits.max_threads:
        reasons.append(
            f"{request.threads} threads requested, {limits.max_threads} allowed in {limits.mode} mode"
        )

    if reasons:
        return Decision("ask", "G1-resources", tuple(reasons))
    return Decision("allow", "G1-resources", ("within the safe budget",))


def check_action(
    action: dict,
    *,
    project_root: str,
    user_data_root: str,
    allowed_hosts: tuple[str, ...] = (),
) -> Decision:
    kind = str(action.get("kind", ""))
    if kind in ("file_write", "file_delete", "file_read"):
        path = str(action.get("path", ""))
        if not path:
            return Decision("deny", "G3-path", ("no path given",))
        if _system_path(path):
            return Decision("deny", "G3-system-path", (f"{path} is a system location",))
        inside = _inside(path, project_root) or _inside(path, user_data_root)
        if not inside:
            return Decision("deny", "G3-outside-roots", (f"{path} is outside the project and user data",))
        if kind == "file_delete":
            return Decision("ask", "G3-delete", ("deleting a file needs permission",))
        return Decision("allow", "G3-file", (f"{kind} inside the approved roots",))

    if kind == "shell":
        command = str(action.get("command", ""))
        if not command:
            return Decision("deny", "G3-shell", ("no command given",))
        for pattern in DANGEROUS_COMMANDS:
            if pattern.search(command):
                return Decision("deny", "G3-destructive", (f"blocked by pattern {pattern.pattern!r}",))
        return Decision("ask", "G3-shell", ("running a shell command needs permission",))

    if kind == "network":
        host = str(action.get("host", "")).casefold()
        if not host:
            return Decision("deny", "G3-network", ("no host given",))
        if host in {h.casefold() for h in allowed_hosts}:
            return Decision("allow", "G3-network", (f"{host} is on the allowlist",))
        return Decision("ask", "G3-network", (f"{host} is not on the allowlist",))

    if kind in ("registry", "device", "disk"):
        return Decision("deny", "G3-device", (f"{kind} access is never automatic",))

    return Decision("deny", "G3-unknown", (f"unknown action kind: {kind!r}",))

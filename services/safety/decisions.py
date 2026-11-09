"""G3 action gating and G1/G2/G4 admission: the code decides, not the model.

Every decision is allow / ask / deny with a reason and the canonical rule id
from rules/guardrails.md (G1 governor, G2 hardware, G3 action, G4 disk).
Nothing here trusts the caller: protected paths, grants and the require()
helper make "ask" impossible to ignore by accident.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

from .limits import Limits
from .profile import MachineProfile

GB = 1024**3

GUARDRAIL_DIR = os.path.dirname(os.path.abspath(__file__))


def _gb(value: int) -> str:
    return f"{value / GB:.1f} GB"


def _system_prefixes() -> tuple[str, ...]:
    if os.name == "nt":
        roots: list[str] = []
        for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            value = os.environ.get(name)
            if value:
                roots.append(value)
        drive = os.environ.get("SystemDrive")
        if drive:
            roots.append(drive + "\\Windows")
        return tuple(dict.fromkeys(roots))
    if sys.platform == "darwin":
        return (
            "/etc",
            "/usr",
            "/bin",
            "/sbin",
            "/boot",
            "/sys",
            "/proc",
            "/dev",
            "/System",
            "/Library",
            "/Applications",
            "/Volumes",
            "/private/etc",
            "/private/var",
        )
    return ("/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc", "/dev", "/var", "/opt")


SYSTEM_PREFIXES = _system_prefixes()

DANGEROUS_COMMANDS = (
    re.compile(r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+/(\*)?(\s|$)", re.IGNORECASE),
    re.compile(r"--no-preserve-root", re.IGNORECASE),
    re.compile(r"\bmkfs(\.|\s|$)", re.IGNORECASE),
    re.compile(r"\bdd\b.*\bof=/dev/", re.IGNORECASE),
    re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
    re.compile(r"\bdel\s+/[fsq].*[a-z]:\\", re.IGNORECASE),
    re.compile(r"\brd\s+/s\s+/q\s+[a-z]:\\", re.IGNORECASE),
    re.compile(r"\bremove-item\b.*-recurse.*[a-z]:\\?\s*$", re.IGNORECASE),
    re.compile(r"\bformat-volume\b", re.IGNORECASE),
    re.compile(r"\bclear-disk\b", re.IGNORECASE),
    re.compile(r"\binitialize-disk\b", re.IGNORECASE),
    re.compile(r"\breg\s+delete\b", re.IGNORECASE),
    re.compile(r"\bbcdedit\b", re.IGNORECASE),
    re.compile(r"\bdiskpart\b", re.IGNORECASE),
    re.compile(r"\b(shutdown|stop-computer|restart-computer)\b", re.IGNORECASE),
    re.compile(r"\bset-executionpolicy\s+(unrestricted|bypass)", re.IGNORECASE),
    re.compile(r":\(\)\s*\{.*\};\s*:"),
    # Added with the agentic tools (the model can now propose commands):
    # wiping a home directory, piping a download into a shell, destroying
    # shadow copies, opening the whole disk to everyone, adding users.
    re.compile(r"\brm\s+-[a-z]*r[a-z]*\s+(~|\$home|/home\b|/users\b|/root\b)", re.IGNORECASE),
    re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", re.IGNORECASE),
    re.compile(r"\b(iwr|irm|invoke-webrequest|invoke-restmethod)\b.*\|\s*(iex|invoke-expression)\b", re.IGNORECASE),
    re.compile(r"\bremove-item\b.*-recurse.*(\$env:userprofile|\$home|~[\\/]?\s*$)", re.IGNORECASE),
    re.compile(r"\b(vssadmin|wbadmin)\b.*\bdelete\b", re.IGNORECASE),
    re.compile(r"\bwmic\b.*shadowcopy.*delete", re.IGNORECASE),
    re.compile(r"\bcipher\s+/w", re.IGNORECASE),
    re.compile(r"\bchmod\s+-r\s+0?777\s+/(\s|$)", re.IGNORECASE),
    re.compile(r"\bchown\s+-r\b.*\s/(\s|$)", re.IGNORECASE),
    re.compile(r">\s*/dev/(sd|nvme|hd|disk)", re.IGNORECASE),
    re.compile(r"\bnet\s+user\b.*\s/add\b", re.IGNORECASE),
    re.compile(r"\bicacls\b.*\beveryone:\(?f", re.IGNORECASE),
    re.compile(r"\btakeown\b.*\s[a-z]:\\?\s*(/r|$)", re.IGNORECASE),
)


@dataclass(frozen=True)
class Decision:
    kind: str  # allow | ask | deny
    rule: str
    reasons: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.kind == "allow"


class PermissionRequired(PermissionError):
    """Raised when a caller tries to act on an 'ask' without a grant."""


@dataclass(frozen=True)
class Grant:
    """What a session (or spawned child) is allowed to do."""

    tools: frozenset[str]
    roots: tuple[str, ...] = ()
    hosts: frozenset[str] = frozenset()

    @staticmethod
    def full(project_root: str, user_data_root: str) -> "Grant":
        return Grant(
            tools=frozenset({"file_read", "file_write", "file_delete", "shell", "network"}),
            roots=(project_root, user_data_root),
            hosts=frozenset(),
        )

    def intersect(self, other: "Grant") -> "Grant":
        roots = tuple(root for root in self.roots if any(_inside(root, keep) for keep in other.roots))
        return Grant(tools=self.tools & other.tools, roots=roots, hosts=self.hosts & other.hosts)


@dataclass(frozen=True)
class ModelRequest:
    model_bytes: int
    context_tokens: int = 8192
    threads: int = 1


def _norm(path: str) -> str:
    resolved = os.path.realpath(os.path.expanduser(path))
    return resolved.casefold() if (os.name == "nt" or sys.platform == "darwin") else resolved


def _inside(path: str, root: str) -> bool:
    candidate, base = _norm(path), _norm(root)
    return candidate == base or candidate.startswith(base + os.sep)


def _temp_prefixes() -> tuple[str, ...]:
    """Scratch locations that sit under a system prefix but are the user's own:
    the process temp dir (macOS: ``/private/var/folders/<..>/T``, which
    ``/private/var`` would otherwise swallow - the CI runner hit exactly that),
    plus the usual temp roots. A workspace there must stay writable."""
    import tempfile

    found = []
    try:
        found.append(os.path.realpath(tempfile.gettempdir()))
    except Exception:  # noqa: BLE001 - no temp dir is not an error here
        pass
    found += ["/tmp", "/var/tmp", "/var/folders", "/private/tmp", "/private/var/tmp", "/private/var/folders"]
    return tuple(dict.fromkeys(found))


TEMP_PREFIXES = _temp_prefixes()


def _system_path(path: str) -> bool:
    candidate = _norm(path)
    if any(_inside(candidate, prefix) for prefix in TEMP_PREFIXES):
        return False
    return any(candidate == _norm(prefix) or candidate.startswith(_norm(prefix) + os.sep) for prefix in SYSTEM_PREFIXES)


# Credential folders in the user's home: never read or written by a tool,
# whatever folders the user approves.
_HOME_SECRET_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".gcloud",
                     os.path.join(".config", "gh"), os.path.join(".config", "gcloud"),
                     os.path.join(".imurun", "secrets"), os.path.join(".local", "share", "keyrings"))


def _protected(path: str, project_root: str, extra: tuple[str, ...]) -> bool:
    candidate = _norm(path)
    home = os.path.expanduser("~")
    secret_roots = tuple(os.path.join(home, d) for d in _HOME_SECRET_DIRS)
    roots = (GUARDRAIL_DIR, os.path.join(project_root, ".git")) + secret_roots + tuple(extra)
    for root in roots:
        base = _norm(root)
        if candidate == base or candidate.startswith(base + os.sep):
            return True
    name = os.path.basename(candidate)
    if name == ".env" or name.startswith(".env."):
        return True
    return name in {".netrc", ".npmrc", ".pypirc", ".git-credentials", ".htpasswd"}


def require(decision: Decision, granted: bool = False) -> Decision:
    """Enforce a decision: deny always raises, ask needs an explicit grant."""
    if decision.kind == "deny" or (decision.kind == "ask" and not granted):
        raise PermissionRequired(f"{decision.rule}: {'; '.join(decision.reasons)}")
    return decision


def check_start(request: ModelRequest, limits: Limits, profile: MachineProfile) -> Decision:
    if request.model_bytes < 0 or request.context_tokens < 0 or request.threads < 1:
        return Decision(
            "deny",
            "G1-governor",
            ("sizes must be non-negative and threads must be at least 1",),
        )

    if profile.errors:
        return Decision(
            "ask",
            "G2-hardware",
            (f"hardware probe incomplete: {'; '.join(profile.errors)}",),
        )

    resident = (
        request.model_bytes
        + request.context_tokens * limits.kv_bytes_per_token
        + limits.compute_overhead
    )

    if request.model_bytes > profile.disk_free - limits.min_free_disk:
        return Decision(
            "deny",
            "G4-disk",
            (
                f"model needs {_gb(request.model_bytes)}, only {_gb(profile.disk_free)} free "
                f"(keeping {_gb(limits.min_free_disk)})",
            ),
        )

    if resident > limits.max_model_ram * 2:
        return Decision(
            "ask",
            "G1-governor",
            (
                f"{_gb(resident)} resident need is more than twice the {_gb(limits.max_model_ram)} "
                "RAM budget; it can stream from disk but will be slow - explicit opt-in required",
            ),
        )

    reasons: list[str] = []
    if resident > limits.max_model_ram:
        reasons.append(
            f"{_gb(resident)} resident need exceeds the {_gb(limits.max_model_ram)} "
            f"{limits.mode} budget; needs full-resource opt-in (G1)"
        )
    if request.threads > limits.max_threads:
        reasons.append(
            f"{request.threads} threads requested, {limits.max_threads} allowed in {limits.mode} mode"
        )
    if reasons:
        return Decision("ask", "G1-governor", tuple(reasons))
    return Decision("allow", "G1-governor", ("within the safe budget",))


def check_request(size: int, elapsed: float, limits: Limits) -> Decision:
    if size < 0 or elapsed < 0:
        return Decision(
            "deny",
            "G1-request",
            ("request size and elapsed time must be non-negative",),
        )
    if size > limits.max_request_bytes:
        return Decision(
            "deny",
            "G1-request",
            (f"request is {size} bytes, cap is {limits.max_request_bytes}",),
        )
    if elapsed > limits.request_timeout:
        return Decision(
            "deny",
            "G1-timeout",
            (f"request ran {elapsed:.1f}s, timeout is {limits.request_timeout:.0f}s",),
        )
    return Decision("allow", "G1-request", ("within the request budget",))


def check_action(
    action: dict,
    *,
    project_root: str,
    user_data_root: str,
    allowed_hosts: tuple[str, ...] = (),
    grant: Grant | None = None,
    protected_roots: tuple[str, ...] = (),
) -> Decision:
    kind = str(action.get("kind", ""))

    if grant is not None and kind not in grant.tools:
        return Decision("deny", "G3-grant", (f"the grant does not include {kind}",))

    if kind in ("file_read", "file_write", "file_delete"):
        path = str(action.get("path", ""))
        if not path:
            return Decision("deny", "G3-action", ("no path given",))
        if _protected(path, project_root, protected_roots):
            return Decision(
                "deny",
                "G3-protected",
                (f"{path} is guardrail source, version control, or a secrets file",),
            )
        if _system_path(path):
            return Decision("deny", "G3-system-path", (f"{path} is a system location",))
        roots = grant.roots if grant is not None else (project_root, user_data_root)
        if not any(_inside(path, root) for root in roots):
            return Decision(
                "deny",
                "G3-outside-roots",
                (f"{path} is outside the approved roots",),
            )
        if kind == "file_delete":
            return Decision("ask", "G3-action", ("deleting a file needs permission",))
        return Decision("allow", "G3-action", (f"{kind} inside the approved roots",))

    if kind == "shell":
        command = str(action.get("command", ""))
        if not command:
            return Decision("deny", "G3-action", ("no command given",))
        for pattern in DANGEROUS_COMMANDS:
            if pattern.search(command):
                return Decision("deny", "G3-destructive", (f"blocked by pattern {pattern.pattern!r}",))
        return Decision("ask", "G3-action", ("running a shell command needs permission",))

    if kind == "network":
        host = str(action.get("host", "")).casefold()
        if not host:
            return Decision("deny", "G5-network", ("no host given",))
        hosts = set(grant.hosts) if grant is not None else set()
        hosts.update(h.casefold() for h in allowed_hosts)
        if host in hosts:
            return Decision("allow", "G5-network", (f"{host} is on the allowlist",))
        return Decision("ask", "G5-network", (f"{host} is not on the allowlist",))

    if kind in ("registry", "device", "disk"):
        return Decision("deny", "G3-action", (f"{kind} access is never automatic",))

    return Decision("deny", "G3-action", (f"unknown action kind: {kind!r}",))

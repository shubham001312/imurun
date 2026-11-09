"""Guardrails (G1-G7): device safety, enforced in code (V4).

Nothing here throws at the caller: a failed probe, limit, action check or
mark is recorded and returned, never swallowed (V9).
"""
from .profile import GpuInfo, MachineProfile, probe
from .limits import Limits, limits
from .decisions import (
    Decision,
    Grant,
    ModelRequest,
    PermissionRequired,
    check_action,
    check_request,
    check_start,
    require,
)
from .governor import Governor, Ticket
from .killswitch import KillSwitch, StopResult
from .audit import Audit, Event

__all__ = [
    "Audit",
    "Decision",
    "Event",
    "GpuInfo",
    "Governor",
    "Grant",
    "KillSwitch",
    "Limits",
    "MachineProfile",
    "ModelRequest",
    "PermissionRequired",
    "StopResult",
    "Ticket",
    "check_action",
    "check_request",
    "check_start",
    "limits",
    "probe",
    "require",
]

"""Guardrails (G-1..G-8): device safety, enforced in code (V4).

Nothing here throws at the caller: a failed probe, limit, action check or
mark is recorded and returned, never swallowed (V9).
"""
from .profile import GpuInfo, MachineProfile, probe
from .limits import Limits, limits
from .decisions import Decision, ModelRequest, check_action, check_start
from .governor import Governor, Ticket
from .killswitch import KillSwitch
from .audit import Audit, Event

__all__ = [
    "Audit",
    "Decision",
    "Event",
    "GpuInfo",
    "Governor",
    "KillSwitch",
    "Limits",
    "MachineProfile",
    "ModelRequest",
    "Ticket",
    "check_action",
    "check_start",
    "limits",
    "probe",
]

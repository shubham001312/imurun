"""Resource governor: leave headroom for the user's own use of the machine.

See docs/resources/README.md. ``budget`` computes the numbers from the
measured machine, ``share`` splits CPU threads between chat, voice and
tuning, ``governor`` applies both (engine load knobs, per-generation thread
target, disk refusals, dynamic yielding, the ``resources`` SSE event).
"""
from .budget import (
    PRESET_NAMES,
    Headroom,
    Machine,
    ResourceBudget,
    compute_budget,
    headroom_for,
    validate_settings,
)
from .governor import DiskCheck, ResourceGovernor, get_governor, measure, set_governor
from .share import ThreadLedger

__all__ = [
    "DiskCheck",
    "Headroom",
    "Machine",
    "PRESET_NAMES",
    "ResourceBudget",
    "ResourceGovernor",
    "ThreadLedger",
    "compute_budget",
    "get_governor",
    "headroom_for",
    "measure",
    "set_governor",
    "validate_settings",
]

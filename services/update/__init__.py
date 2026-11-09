"""Update check / ask-again policy / apply (docs/setup/README.md "Updates").

Contract: services/server/doc/README.md "Update contract".
"""
from __future__ import annotations

from .manager import (  # noqa: F401
    ASK_AGAIN_AFTER,
    CACHE_SECONDS,
    EXIT_RESTART,
    RESTART,
    UpdateManager,
    apply_plan,
    cached_notice,
    detect_source,
    enabled,
    fetch_latest,
    is_newer,
    make_manager,
    parse_version,
    remove_stale_engine_copies,
    request_restart,
    should_prompt,
)

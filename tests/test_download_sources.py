"""Inspect the model catalogue and download sources.

Run from the ``imurun`` directory:

    python tests/test_download_sources.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.model_manager import (  # noqa: E402
    FISH_COMPATIBLE_IDS,
    RECOMMENDED_MODELS,
    get_model_manager,
)


def main() -> int:
    manager = get_model_manager()
    bad = 0
    print(f"catalogue entries: {len(RECOMMENDED_MODELS)}")
    print(f"fish-compatible  : {len(FISH_COMPATIBLE_IDS)}")
    for model_id, info in RECOMMENDED_MODELS.items():
        sources = manager.resolve_sources(model_id)
        if not sources:
            print(f"FAIL {model_id}: no sources")
            bad += 1
            continue
        for source in sources:
            if not isinstance(source, dict):
                print(f"FAIL {model_id}: source is {type(source).__name__}, not dict")
                bad += 1
                continue
            for key in ("repo_id", "filename", "url"):
                if not source.get(key):
                    print(f"FAIL {model_id}: source missing {key}")
                    bad += 1
        compat = model_id in FISH_COMPATIBLE_IDS
        print(f"  {model_id:38s} {info.size_gb:6.1f} GB  "
              f"{'fish' if compat else 'skip'}  {sources[0]['url']}")
    print(f"\n{'OK' if not bad else f'{bad} problems'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())

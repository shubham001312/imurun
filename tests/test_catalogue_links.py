"""Verify every catalogue download source really exists on Hugging Face.

For each source this asks the HF API for the repo tree and checks that the
exact filename is present, then records the served size. Nothing is downloaded.

Run from the ``imurun`` directory:

    python tests/test_catalogue_links.py
    python tests/test_catalogue_links.py --compatible-only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.model_manager import (  # noqa: E402
    FISH_COMPATIBLE_IDS,
    RECOMMENDED_MODELS,
    get_model_manager,
)


def check_source(repo_id: str, filename: str) -> tuple[bool, int | None, str]:
    import httpx

    url = f"https://huggingface.co/api/models/{repo_id}/tree/main"
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(url)
    except Exception as exc:  # noqa: BLE001
        return False, None, f"request failed: {exc}"
    if response.status_code != 200:
        return False, None, f"HTTP {response.status_code}"
    try:
        entries = response.json()
    except Exception:  # noqa: BLE001
        return False, None, "invalid JSON"
    if not isinstance(entries, list):
        return False, None, "unexpected API shape"
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path") == filename:
            size = entry.get("size")
            return True, int(size) if size else None, "ok"
    return False, None, "file not in repo"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compatible-only", action="store_true")
    args = ap.parse_args()

    manager = get_model_manager()
    bad = 0
    checked = 0
    for model_id, info in RECOMMENDED_MODELS.items():
        if args.compatible_only and model_id not in FISH_COMPATIBLE_IDS:
            continue
        for source in manager.resolve_sources(model_id):
            checked += 1
            ok, size, note = check_source(source["repo_id"], source["filename"])
            declared = int(info.size_bytes or 0)
            size_note = ""
            if ok and size and declared:
                drift = abs(size - declared) / size
                if drift > 0.05:
                    size_note = f"  SIZE DRIFT declared={declared} served={size} ({drift:.0%})"
            status = "OK " if ok else "BAD"
            if not ok:
                bad += 1
            print(f"{status} {model_id:38s} {note:14s} {source['repo_id']}{size_note}")

    print(f"\nchecked {checked} sources, {bad} broken")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())

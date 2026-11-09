"""Exercise the real downloader against Hugging Face, including resume.

This uses a small real file from a real repo (the 1.3 MB imatrix sidecar of
the Llama 3.2 1B repo) so the test proves three things without downloading a
multi-gigabyte model:

1. A fresh download completes and the size matches what the server serves.
2. A pre-seeded ``.part`` file resumes with an HTTP Range request instead of
   restarting from zero.
3. A source that does not exist produces a short, useful error instead of a
   ``tuple indices`` crash.

Run from the ``imurun`` directory:

    python tests/test_download_resume.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.model_manager import (  # noqa: E402
    DownloadProgress,
    ModelInfo,
    ModelManager,
    hf_source,
)

REPO = "bartowski/Llama-3.2-1B-Instruct-GGUF"
FILENAME = "Llama-3.2-1B-Instruct.imatrix"
SIZE = 1_314_426


def make_model(tmp: Path, filename: str) -> ModelInfo:
    return ModelInfo(
        id="resume-probe",
        name="Resume probe",
        filename=filename,
        size_bytes=SIZE,
        size_gb=SIZE / 1024**3,
        source="hf",
        repo_id=REPO,
        sources=[hf_source(REPO, FILENAME, SIZE)],
    )


async def run_download(manager: ModelManager, model: ModelInfo) -> list[DownloadProgress]:
    events: list[DownloadProgress] = []
    async for event in manager.download_model(model.id):
        events.append(event)
    return events


def seed_part(tmp: Path, model: ModelInfo, prefix: int) -> int:
    """Write the first ``prefix`` bytes of the remote file into the .part."""
    import httpx

    url = model.sources[0]["url"]
    part = tmp / (model.filename + ".part")
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            written = 0
            with open(part, "wb") as handle:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    take = min(len(chunk), prefix - written)
                    if take <= 0:
                        break
                    handle.write(chunk[:take])
                    written += take
                    if written >= prefix:
                        break
    return part.stat().st_size


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="imurun-resume-"))
    failures = 0
    try:
        manager = ModelManager(tmp)
        model = make_model(tmp, FILENAME)
        # The manager resolves sources from the catalogue, so register it.
        from services import model_manager as mm

        mm.RECOMMENDED_MODELS[model.id] = model

        # --- 1. fresh download -------------------------------------------
        print("fresh download ...")
        events = asyncio.run(run_download(manager, model))
        final = tmp / FILENAME
        last = events[-1]
        if last.status != "complete":
            print(f"FAIL fresh: last status {last.status!r}: {last.error}")
            failures += 1
        elif not final.exists() or final.stat().st_size != SIZE:
            got = final.stat().st_size if final.exists() else "missing"
            print(f"FAIL fresh: size {got} != {SIZE}")
            failures += 1
        else:
            print(f"OK   fresh: {final.stat().st_size} bytes, status=complete")

        # --- 2. resume from a partial file --------------------------------
        print("resume from 40% ...")
        final.unlink(missing_ok=True)
        seeded = seed_part(tmp, model, int(SIZE * 0.4))
        print(f"     seeded .part with {seeded} bytes")
        events = asyncio.run(run_download(manager, model))
        first_download = next(
            (e for e in events if e.status == "downloading"), None
        )
        last = events[-1]
        if first_download is None:
            print("FAIL resume: no downloading event")
            failures += 1
        elif first_download.downloaded_bytes < seeded:
            print(
                "FAIL resume: restarted at "
                f"{first_download.downloaded_bytes} bytes instead of resuming "
                f"from {seeded}"
            )
            failures += 1
        elif last.status != "complete" or not final.exists():
            print(f"FAIL resume: last status {last.status!r}: {last.error}")
            failures += 1
        elif final.stat().st_size != SIZE:
            print(f"FAIL resume: final size {final.stat().st_size} != {SIZE}")
            failures += 1
        else:
            print(
                f"OK   resume: continued from {seeded} bytes, "
                f"final {final.stat().st_size} bytes"
            )

        # --- 3. missing repo produces a clean error ----------------------
        print("missing repo error ...")
        broken = ModelInfo(
            id="missing-probe",
            name="Missing probe",
            filename="missing.gguf",
            size_bytes=1000,
            size_gb=0.001,
            source="hf",
            repo_id="nobody/does-not-exist-xyz",
            sources=[
                hf_source("nobody/does-not-exist-xyz", "missing.gguf", 1000)
            ],
        )
        mm.RECOMMENDED_MODELS[broken.id] = broken
        events = asyncio.run(run_download(manager, broken))
        last = events[-1]
        if last.status != "error":
            print(f"FAIL missing: status {last.status!r}, expected error")
            failures += 1
        elif "tuple indices" in (last.error or ""):
            print(f"FAIL missing: tuple crash leaked: {last.error}")
            failures += 1
        else:
            print(f"OK   missing: {last.error}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'OK' if not failures else f'{failures} failure(s)'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

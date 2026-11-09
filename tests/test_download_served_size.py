"""The downloader trusts the size the server reports over the catalogue's.

Catalogue sizes are often round approximations (on 2026-11-09 nine of the
FISH-compatible entries differed from what Hugging Face serves, e.g. Qwen2.5 7B
4.5e9 vs 4 683 074 240 bytes). The exact size came only from ``HfApi.repo_info``;
when that metadata call failed (rate limit, API outage) the approximation was
used, and a correct download could never complete: a bigger file was deleted
as "more bytes than expected" and re-downloaded up to six times, a smaller one
was "resumed" past its end. Offline: the transport is an ``httpx.MockTransport``.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from services import model_manager as mm

PAYLOAD = b"GGUF" + bytes(range(256)) * 40  # 10 244 bytes


def _transport(payload: bytes):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Range"))
        rng = request.headers.get("Range")
        if rng:
            start = int(rng.split("=", 1)[1].split("-", 1)[0])
            if start >= len(payload):
                return httpx.Response(416, headers={"Content-Range": f"bytes */{len(payload)}"})
            body = payload[start:]
            return httpx.Response(206, content=body, headers={
                "Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"})
        return httpx.Response(200, content=payload)

    return httpx.MockTransport(handler), seen


@pytest.fixture()
def offline(monkeypatch):
    class NoMetadata:
        def repo_info(self, *a, **k):
            raise RuntimeError("429 rate limited")

    monkeypatch.setattr(mm, "HfApi", NoMetadata)
    real_client = httpx.AsyncClient

    def install(payload: bytes):
        transport, seen = _transport(payload)
        monkeypatch.setattr(mm.httpx, "AsyncClient",
                            lambda *a, **k: real_client(*a, **{**k, "transport": transport}))
        return seen

    return install


def _model(catalogue_size: int) -> mm.ModelInfo:
    return mm.ModelInfo(
        id="served-size-probe", name="probe", filename="probe.gguf",
        size_bytes=catalogue_size, size_gb=catalogue_size / 1024 ** 3, source="hf",
        repo_id="example/probe", sources=[mm.hf_source("example/probe", "probe.gguf", catalogue_size)],
    )


def _download(tmp_path, monkeypatch, model):
    monkeypatch.setitem(mm.RECOMMENDED_MODELS, model.id, model)
    monkeypatch.setattr(mm.asyncio, "sleep", lambda *_a, **_k: asyncio.sleep(0))
    manager = mm.ModelManager(tmp_path)

    async def run():
        return [event async for event in manager.download_model(model.id)]

    return asyncio.run(run())


@pytest.mark.parametrize("catalogue_size", [len(PAYLOAD) - 1000, len(PAYLOAD) + 1000])
def test_download_completes_when_catalogue_size_is_approximate(tmp_path, monkeypatch, offline, catalogue_size):
    seen = offline(PAYLOAD)
    events = _download(tmp_path, monkeypatch, _model(catalogue_size))
    assert events[-1].status == "complete", events[-1]
    assert (tmp_path / "probe.gguf").read_bytes() == PAYLOAD
    assert not (tmp_path / "probe.gguf.part").exists()
    assert len(seen) == 1, f"downloaded more than once: {seen}"


def test_resume_takes_the_total_from_content_range(tmp_path, monkeypatch, offline):
    seen = offline(PAYLOAD)
    (tmp_path / "probe.gguf.part").write_bytes(PAYLOAD[:3000])
    events = _download(tmp_path, monkeypatch, _model(len(PAYLOAD) + 5000))
    assert events[-1].status == "complete", events[-1]
    assert (tmp_path / "probe.gguf").read_bytes() == PAYLOAD
    assert seen == ["bytes=3000-"]


@pytest.mark.parametrize("prefix", [6000, len(PAYLOAD)])
def test_part_bigger_than_an_approximate_catalogue_size_is_kept(tmp_path, monkeypatch, offline, prefix):
    # A .part larger than the (too small) catalogue size is a valid partial,
    # or even the whole file; it used to be deleted as "stale" before asking.
    seen = offline(PAYLOAD)
    (tmp_path / "probe.gguf.part").write_bytes(PAYLOAD[:prefix])
    events = _download(tmp_path, monkeypatch, _model(4000))
    assert events[-1].status == "complete", events[-1]
    assert (tmp_path / "probe.gguf").read_bytes() == PAYLOAD
    assert seen == [f"bytes={prefix}-"]

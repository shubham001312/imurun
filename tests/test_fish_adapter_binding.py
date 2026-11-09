"""FishNativeClient LoRA-adapter plumbing (ABI 3), against a fake engine library.

The real merge is tested in C++ (model/fish/test/test_m5.cpp); this checks the
Python side: the adapter reaches fish_set_adapter before fish_load, a runtime
switch goes through fish_set_adapter / fish_clear_adapter, adapter info is
decoded, and a failed select restores the previous model *and* adapter.
"""
from __future__ import annotations

import ctypes
from pathlib import Path

from model.fish_native import FishNativeClient


class FakeLib:
    """Just enough of the C ABI for load/select/set_adapter."""

    def __init__(self, fail_paths=()):
        self.calls = []
        self.fail_paths = {str(p) for p in fail_paths}
        self.adapter = None          # (path, scale) remembered by the "engine"
        self.loaded_adapter = None
        self.next_ctx = 100

    # lifecycle
    def fish_create(self):
        self.next_ctx += 1
        return self.next_ctx

    def fish_destroy(self, ctx):
        self.calls.append(("destroy", ctx))

    def fish_load(self, ctx, path, n_ctx):
        p = path.decode()
        self.calls.append(("load", p, self.adapter))
        if p in self.fail_paths:
            return -2
        self.loaded_adapter = self.adapter
        return 0

    def fish_last_error(self, ctx):
        return b"boom"

    def fish_abi_version(self):
        return 3

    def fish_version(self):
        return b"fish test"

    def fish_kernel_name(self):
        return b"scalar"

    def fish_model_info_of(self, ctx, ref):
        return 0                     # all-zero shape is fine for these tests

    def fish_chat_format(self, ctx):
        return b"llama3"

    # adapters
    def fish_set_adapter(self, ctx, path, scale):
        self.calls.append(("set_adapter", path.decode(), round(scale, 3)))
        self.adapter = (path.decode(), scale)
        return 0

    def fish_clear_adapter(self, ctx):
        self.calls.append(("clear_adapter",))
        self.adapter = None
        return 0

    def fish_adapter_info_of(self, ctx, ref):
        info = ref._obj
        if self.adapter:
            info.active = 1
            info.name = Path(self.adapter[0]).name.encode()
            info.rank = 16
            info.alpha = 32.0
            info.scale = self.adapter[1]
            info.effective_scale = self.adapter[1] * 2.0
            info.tensors = 112
            info.retention = 0.95
        return 0


def _client(tmp_path: Path, lib: FakeLib, model: Path) -> FishNativeClient:
    dll = tmp_path / "fish.dll"
    dll.write_bytes(b"")

    class Cfg:
        model_path = str(model)

    c = FishNativeClient(Cfg)
    c._library_path = dll
    c._lib = lib
    return c


def _files(tmp_path: Path, *names: str) -> list[Path]:
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b"GGUF")
        out.append(p)
    return out


def test_select_with_adapter_sets_it_before_load(tmp_path):
    base, other, ad = _files(tmp_path, "base.gguf", "other.gguf", "adapter.gguf")
    lib = FakeLib()
    c = _client(tmp_path, lib, base)
    assert c.load()
    assert c.select(other, adapter=ad, scale=0.5)
    i_set = lib.calls.index(("set_adapter", str(ad), 0.5))
    i_load = max(i for i, call in enumerate(lib.calls) if call[0] == "load")
    assert i_set < i_load
    assert lib.calls[i_load] == ("load", str(other), (str(ad), 0.5))
    info = c.model_info()["adapter"]
    assert info["active"] and info["name"] == "adapter.gguf"
    assert info["rank"] == 16 and info["tensors"] == 112
    assert abs(info["effective_scale"] - 1.0) < 1e-6


def test_set_adapter_at_runtime_and_clear(tmp_path):
    base, ad = _files(tmp_path, "base.gguf", "adapter.gguf")
    lib = FakeLib()
    c = _client(tmp_path, lib, base)
    assert c.load()
    assert c.model_info()["adapter"] is None
    assert c.set_adapter(ad, 1.0)
    assert ("set_adapter", str(ad), 1.0) in lib.calls
    assert c.adapter_path == ad
    assert c.set_adapter(None)
    assert ("clear_adapter",) in lib.calls
    assert c.adapter_path is None


def test_missing_adapter_file_is_refused(tmp_path):
    (base,) = _files(tmp_path, "base.gguf")
    lib = FakeLib()
    c = _client(tmp_path, lib, base)
    assert c.load()
    assert not c.set_adapter(tmp_path / "nope.gguf")
    assert "not found" in (c.error or "")
    assert not any(call[0] == "set_adapter" for call in lib.calls)


def test_failed_select_restores_previous_model_and_adapter(tmp_path):
    base, bad, ad = _files(tmp_path, "base.gguf", "bad.gguf", "adapter.gguf")
    lib = FakeLib(fail_paths=[bad])
    c = _client(tmp_path, lib, base)
    assert c.select(base, adapter=ad, scale=1.0)
    assert not c.select(bad)
    assert c.error == "boom"
    assert c.model_path == base and c.adapter_path == ad
    last_load = [call for call in lib.calls if call[0] == "load"][-1]
    assert last_load == ("load", str(base), (str(ad), 1.0))


def test_failed_first_select_leaves_no_model_configured(tmp_path):
    # With no previous model, a file that will not load used to stay as the
    # model path: configured was true, /api/status said "ready" and every turn
    # was accepted only to fail with the load error.
    (bad,) = _files(tmp_path, "bad.gguf")
    lib = FakeLib(fail_paths=[bad])
    c = _client(tmp_path, lib, tmp_path / "none.gguf")
    c._model_path = None
    assert not c.configured
    assert not c.select(bad)
    assert c.error == "boom"
    assert c.model_path is None
    assert not c.configured


def test_adapter_info_struct_layout():
    from model.fish_native import FishAdapterInfo
    # int, int, char[128], int, int, float x3, int, u64, double x3, u64 x2 (natural alignment)
    assert ctypes.sizeof(FishAdapterInfo) == 208

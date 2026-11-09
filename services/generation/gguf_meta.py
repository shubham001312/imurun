"""Header-only GGUF metadata reader (no tensor data is touched).

Reads the key/value section of a GGUF file so the server can pick a
generation profile (chat format, sampling, reasoning handling) before or
without asking the engine. Large arrays (the tokenizer vocabulary) are
skipped unless the caller asks for specific token strings, so a read of an
8B file costs a few megabytes of I/O, not gigabytes.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO, Iterable

_SCALARS = {
    0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4),
    5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1), 10: ("<Q", 8), 11: ("<q", 8),
    12: ("<d", 8),
}
_STRING, _ARRAY = 8, 9
# Arrays longer than this are not materialised (only scanned for wanted tokens).
_MAX_ARRAY = 64
MAX_KV = 100_000


class GGUFError(ValueError):
    pass


def _read(f: BinaryIO, n: int) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise GGUFError("truncated GGUF header")
    return data


def _u64(f: BinaryIO) -> int:
    return struct.unpack("<Q", _read(f, 8))[0]


def _string(f: BinaryIO) -> str:
    n = _u64(f)
    if n > 64 * 1024 * 1024:
        raise GGUFError("implausible string length in GGUF header")
    return _read(f, n).decode("utf-8", "replace")


def _value(f: BinaryIO, vtype: int, key: str, wanted: set[str], found: dict):
    if vtype in _SCALARS:
        fmt, size = _SCALARS[vtype]
        return struct.unpack(fmt, _read(f, size))[0]
    if vtype == _STRING:
        return _string(f)
    if vtype == _ARRAY:
        etype = struct.unpack("<I", _read(f, 4))[0]
        count = _u64(f)
        if count > 50_000_000:
            raise GGUFError("implausible array length in GGUF header")
        keep = count <= _MAX_ARRAY
        out = [] if keep else None
        if etype == _STRING:
            for i in range(count):
                s = _string(f)
                if keep:
                    out.append(s)
                if wanted and key == "tokenizer.ggml.tokens" and s in wanted:
                    found[s] = i
            return out if keep else {"array_of": "string", "count": count}
        if etype in _SCALARS:
            fmt, size = _SCALARS[etype]
            if keep:
                return [struct.unpack(fmt, _read(f, size))[0] for _ in range(count)]
            f.seek(size * count, 1)
            return {"array_of": fmt, "count": count}
        raise GGUFError(f"unsupported nested array type {etype} for {key}")
    raise GGUFError(f"unknown GGUF value type {vtype} for {key}")


def read_metadata(path: str | Path, tokens: Iterable[str] = ()) -> dict:
    """Return the GGUF key/values. ``tokens``: strings whose vocabulary ids are
    wanted; they come back under the pseudo-key ``"_token_ids"``."""
    wanted = set(tokens)
    found: dict[str, int] = {}
    with open(path, "rb") as f:
        if _read(f, 4) != b"GGUF":
            raise GGUFError("not a GGUF file")
        version = struct.unpack("<I", _read(f, 4))[0]
        if version < 2:
            raise GGUFError(f"GGUF version {version} is not supported")
        _n_tensors = _u64(f)
        n_kv = _u64(f)
        if n_kv > MAX_KV:
            raise GGUFError("implausible metadata count")
        meta: dict = {"_version": version}
        for _ in range(n_kv):
            key = _string(f)
            vtype = struct.unpack("<I", _read(f, 4))[0]
            meta[key] = _value(f, vtype, key, wanted, found)
    if wanted:
        meta["_token_ids"] = found
    return meta

"""Audio helpers: format sniffing, WAV in/out, decoding to 16 kHz mono.

Two halves:

* stdlib only (safe to import in the server): ``sniff``, ``wav_bytes``,
  ``wav_info``, ``parse_multipart``, ``ffmpeg_path``.
* ``decode_16k`` (used inside the voice worker): returns float32 samples at
  16 kHz mono for Whisper. WAV is decoded with the stdlib ``wave`` module +
  numpy (no ffmpeg). WebM/Opus, OGG, MP3, M4A, FLAC go through PyAV (``av``,
  which ships its own FFmpeg libraries in the wheel), and only if PyAV is
  missing through an ``ffmpeg`` executable on PATH.
"""
from __future__ import annotations

import io
import shutil
import struct
import subprocess
import wave
from dataclasses import dataclass

TARGET_RATE = 16000


class AudioError(ValueError):
    """The upload is not audio we can decode; the message is user-facing."""


def sniff(data: bytes, content_type: str = "") -> str:
    """Container name from magic bytes, falling back to the Content-Type."""
    head = data[:16]
    if head[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if data[4:8] == b"ftyp":
        return "mp4"
    kind = (content_type or "").split(";")[0].strip().lower()
    return {
        "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/vnd.wave": "wav",
        "audio/webm": "webm", "video/webm": "webm", "audio/ogg": "ogg", "audio/opus": "ogg",
        "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/flac": "flac", "audio/x-flac": "flac",
        "audio/mp4": "mp4", "audio/m4a": "mp4", "audio/x-m4a": "mp4", "audio/aac": "mp4",
    }.get(kind, "")


def wav_bytes(pcm: bytes, rate: int, channels: int = 1, width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(int(rate))
        out.writeframes(pcm)
    return buffer.getvalue()


@dataclass
class WavInfo:
    rate: int
    channels: int
    width: int
    frames: int
    fmt: int = 1          # 1 = PCM integer, 3 = IEEE float
    data_offset: int = 0
    data_size: int = 0

    @property
    def duration(self) -> float:
        return self.frames / self.rate if self.rate else 0.0


WAVE_FORMAT_PCM, WAVE_FORMAT_FLOAT, WAVE_FORMAT_EXTENSIBLE = 1, 3, 0xFFFE


def wav_info(data: bytes) -> WavInfo:
    """Parse the RIFF header ourselves: the stdlib ``wave`` module rejects
    IEEE-float WAV (format 3, what FLEURS and many Web Audio recorders write)
    and WAVE_FORMAT_EXTENSIBLE (BUGS-FIXED #7)."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError("not a readable WAV file (no RIFF/WAVE header)")
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if cid == b"fmt " and size >= 16:
            tag, channels, rate, _byte_rate, _align, bits = struct.unpack_from("<HHIIHH", data, body)
            if tag == WAVE_FORMAT_EXTENSIBLE and size >= 40:
                tag = struct.unpack_from("<H", data, body + 24)[0]   # first two bytes of the SubFormat GUID
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            if fmt is None:
                raise AudioError("not a readable WAV file (data before fmt chunk)")
            tag, channels, rate, bits = fmt
            if tag not in (WAVE_FORMAT_PCM, WAVE_FORMAT_FLOAT):
                raise AudioError(f"not a readable WAV file (unsupported format {tag})")
            if channels < 1 or rate < 1 or bits not in (8, 16, 24, 32, 64):
                raise AudioError("not a readable WAV file (bad fmt chunk)")
            width = bits // 8
            # A streaming recorder may leave size 0 / 0xFFFFFFFF: use what is there.
            available = len(data) - body
            size = available if size in (0, 0xFFFFFFFF) or size > available else size
            frames = size // (width * channels)
            return WavInfo(rate, channels, width, frames, tag, body, frames * width * channels)
        pos = body + size + (size & 1)
    raise AudioError("not a readable WAV file (no data chunk)")


def ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or ""


def has_pyav() -> bool:
    try:
        import av  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "not usable"
        return False
    return True


def parse_multipart(body: bytes, content_type: str) -> tuple[bytes, str, dict[str, str]]:
    """First file part of a multipart/form-data body -> (bytes, its content type, text fields).

    Small and strict on purpose: the shell sends one ``audio`` file plus a few
    text fields (``lang``). No temporary files are written.
    """
    boundary = ""
    for piece in content_type.split(";"):
        key, _, value = piece.strip().partition("=")
        if key.lower() == "boundary":
            boundary = value.strip().strip('"')
    if not boundary:
        raise AudioError("multipart body without a boundary")
    marker = b"--" + boundary.encode("latin-1")
    audio, audio_type, fields = b"", "", {}
    for chunk in body.split(marker)[1:]:
        if chunk.startswith(b"--"):
            break
        chunk = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        header_blob, sep, content = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        headers: dict[str, str] = {}
        for line in header_blob.decode("utf-8", "replace").split("\r\n"):
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        params = {}
        for item in disposition.split(";")[1:]:
            k, _, v = item.strip().partition("=")
            params[k.lower()] = v.strip().strip('"')
        if "filename" in params or headers.get("content-type", "").startswith(("audio/", "video/", "application/octet")):
            if not audio:
                audio, audio_type = content, headers.get("content-type", "")
        elif params.get("name"):
            fields[params["name"]] = content.decode("utf-8", "replace")
    if not audio:
        raise AudioError("multipart body has no audio file part")
    return audio, audio_type, fields


# ------------------------------------------------------------ worker side

def decode_16k(data: bytes, content_type: str = ""):
    """float32 numpy array, mono, 16 kHz. Raises AudioError with a clear reason."""
    import numpy as np

    kind = sniff(data, content_type)
    if kind == "wav":
        info = wav_info(data)
        raw = data[info.data_offset:info.data_offset + info.data_size]
        if info.fmt == WAVE_FORMAT_FLOAT and info.width in (4, 8):
            samples = np.frombuffer(raw, dtype="<f4" if info.width == 4 else "<f8").astype(np.float32)
            samples = np.nan_to_num(np.clip(samples, -1.0, 1.0))
        elif info.width == 2:
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif info.width == 4:
            samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        elif info.width == 3:
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            ints = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
            ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
            samples = ints.astype(np.float32) / 8388608.0
        elif info.width == 1:
            samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            raise AudioError(f"unsupported WAV sample width {info.width}")
        if info.channels > 1:
            samples = samples.reshape(-1, info.channels).mean(axis=1)
        return _resample(samples, info.rate)
    if not kind:
        raise AudioError("unrecognised audio format (send WAV, WebM/Opus, OGG, MP3, FLAC or M4A)")
    if has_pyav():
        # Our own PyAV loop, not faster_whisper.audio.decode_audio: that one
        # passes av.open(metadata_errors=...), which PyAV 19 removed
        # (TypeError on every WebM upload; docs/voice/BUGS-FIXED.md #1).
        try:
            return _decode_pyav(data)
        except Exception as exc:  # noqa: BLE001 - corrupt upload
            raise AudioError(f"could not decode {kind} audio: {exc}") from exc
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise AudioError(f"{kind} audio needs PyAV (pip install av) or ffmpeg on PATH; WAV works without either")
    proc = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-ac", "1", "-ar", str(TARGET_RATE), "pipe:1"],
        input=data, capture_output=True, timeout=120, check=False,
    )
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg could not decode {kind}: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    return np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0


def _decode_pyav(data: bytes):
    import av
    import numpy as np

    out = []
    with av.open(io.BytesIO(data)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
        try:
            for frame in container.decode(audio=0):
                for item in resampler.resample(frame):
                    out.append(item.to_ndarray().reshape(-1))
        except Exception:  # noqa: BLE001
            # A streamed WebM is cut mid-cluster until the next chunk arrives:
            # keep what decoded (the next pass re-decodes the whole stream).
            if not out:
                raise
        for item in resampler.resample(None):  # flush the resampler's tail
            out.append(item.to_ndarray().reshape(-1))
    if not out:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(out).astype(np.float32) / 32768.0


def _resample(samples, rate: int):
    import numpy as np

    if rate == TARGET_RATE or samples.size == 0:
        return samples.astype(np.float32)
    # Linear interpolation without a low-pass filter: some energy above 8 kHz
    # aliases down, which is small for speech and was not measurable in the
    # round-trip tests (docs/voice/README.md). PyAV paths resample properly.
    count = int(round(samples.size * TARGET_RATE / rate))
    positions = np.linspace(0, samples.size - 1, count)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.float32)

"""Audio file helpers for the podcast that NotebookLM generates."""

from __future__ import annotations

import wave
from pathlib import Path


def wav_to_mp3(wav_path: Path, kbps: int = 64) -> Path | None:
    """Encode a 16-bit PCM WAV to MP3 (about 0.5 MB per minute at 64 kbps) for email.
    Returns None if lameenc is missing or the WAV is not 16-bit PCM."""
    try:
        import lameenc
    except ImportError:
        return None
    try:
        with wave.open(str(wav_path), "rb") as w:
            if w.getsampwidth() != 2:
                return None
            enc = lameenc.Encoder()
            enc.set_bit_rate(kbps)
            enc.set_in_sample_rate(w.getframerate())
            enc.set_channels(w.getnchannels())
            enc.set_quality(5)
            data = enc.encode(w.readframes(w.getnframes())) + enc.flush()
    except wave.Error:
        return None
    mp3 = wav_path.with_suffix(".mp3")
    mp3.write_bytes(data)
    return mp3


EMAIL_AUDIO_BUDGET = 16 * 1024 * 1024  # leaves room for the digest under Gmail's 25 MB cap


def mp3_bitrate_for(seconds: float, budget_bytes: int = EMAIL_AUDIO_BUDGET) -> int:
    """Highest standard MP3 bitrate (kbps) whose file fits the budget; floor 24 kbps."""
    for kbps in (96, 80, 64, 56, 48, 40, 32, 24):
        if seconds * kbps * 1000 / 8 <= budget_bytes * 0.97:
            return kbps
    return 24


def transcode_to_mp3(src: Path, kbps: int, sample_rate: int = 24000) -> Path:
    """Decode any audio PyAV reads (NotebookLM's AAC .m4a) and re-encode as mono MP3."""
    import av
    import lameenc

    enc = lameenc.Encoder()
    enc.set_bit_rate(kbps)
    enc.set_in_sample_rate(sample_rate)
    enc.set_channels(1)
    enc.set_quality(5)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
    chunks = []
    with av.open(str(src)) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(enc.encode(bytes(out.planes[0])[: out.samples * 2]))
        for out in resampler.resample(None):
            chunks.append(enc.encode(bytes(out.planes[0])[: out.samples * 2]))
    chunks.append(enc.flush())
    dst = src.with_suffix(".mp3")
    dst.write_bytes(b"".join(chunks))
    return dst


def duration_seconds(path: Path) -> float:
    import av
    with av.open(str(path)) as c:
        return float(c.duration / 1e6) if c.duration else 0.0


def email_ready(audio: Path, budget_bytes: int = EMAIL_AUDIO_BUDGET) -> Path:
    """The episode as it will be attached. Kept as-is when it fits the email budget;
    otherwise re-encoded to an MP3 bitrate chosen to fit."""
    if audio.stat().st_size <= budget_bytes:
        return audio
    if audio.suffix.lower() == ".wav":
        mp3 = wav_to_mp3(audio, kbps=48)
        if mp3 and mp3.stat().st_size <= budget_bytes:
            return mp3
    kbps = mp3_bitrate_for(duration_seconds(audio), budget_bytes)
    return transcode_to_mp3(audio, kbps)

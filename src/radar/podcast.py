"""Text-to-speech for the two-host podcast script.

Backends:
- sapi: Windows built-in voices through System.Speech. No download, works out of the box.
- kokoro: kokoro-onnx (Apache-2.0, 82M params). Better voices; needs the model files once.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import wave
from pathlib import Path

from .config import PodcastCfg

SAPI_PS = r"""
param([string]$LinesPath, [string]$OutPath, [string]$VoiceA, [string]$VoiceB)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = @($s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo.Name })
if (-not $VoiceA) { $VoiceA = $voices[0] }
if (-not $VoiceB) { $VoiceB = if ($voices.Count -gt 1) { $voices[1] } else { $voices[0] } }
$lines = Get-Content -Raw -Encoding UTF8 $LinesPath | ConvertFrom-Json
$s.SetOutputToWaveFile($OutPath)
$s.Rate = 1
foreach ($ln in $lines) {
  if ($ln.speaker -eq 'A') { $s.SelectVoice($VoiceA) } else { $s.SelectVoice($VoiceB) }
  $s.Speak($ln.text)
}
$s.SetOutputToNull()
$s.Dispose()
Write-Output "voices: A=$VoiceA B=$VoiceB"
"""


def clean_lines(lines: list[dict]) -> list[dict]:
    out = []
    for ln in lines:
        text = " ".join(str(ln.get("text", "")).replace("*", "").split())
        if text:
            out.append({"speaker": "A" if ln.get("speaker") == "A" else "B", "text": text})
    return out


def synthesize_sapi(lines: list[dict], out_wav: Path, cfg: PodcastCfg) -> Path:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        lines_path = Path(tmp) / "lines.json"
        lines_path.write_text(json.dumps(clean_lines(lines)), encoding="utf-8")
        ps_path = Path(tmp) / "speak.ps1"
        ps_path.write_text(SAPI_PS, encoding="utf-8-sig")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_path),
             "-LinesPath", str(lines_path), "-OutPath", str(out_wav),
             "-VoiceA", cfg.host_a_voice, "-VoiceB", cfg.host_b_voice],
            capture_output=True, text=True, timeout=1800, check=False,
        )
    if proc.returncode != 0 or not out_wav.exists():
        raise RuntimeError(f"SAPI synthesis failed: {proc.stderr[-1000:]}")
    return out_wav


def synthesize_kokoro(lines: list[dict], out_wav: Path, cfg: PodcastCfg, root: Path) -> Path:
    try:
        import numpy as np
        import soundfile as sf
        from kokoro_onnx import Kokoro
    except ImportError as e:
        raise RuntimeError("kokoro backend needs `pip install -e .[kokoro]`") from e
    model, voices = root / cfg.kokoro_model, root / cfg.kokoro_voices
    if not (model.exists() and voices.exists()):
        raise RuntimeError(f"Kokoro model files missing: {model} / {voices}. See README, Podcast audio.")
    tts = Kokoro(str(model), str(voices))
    chunks, sr = [], 24000
    for ln in clean_lines(lines):
        voice = cfg.kokoro_voice_a if ln["speaker"] == "A" else cfg.kokoro_voice_b
        samples, sr = tts.create(ln["text"], voice=voice, speed=1.0, lang="en-us")
        chunks += [samples, np.zeros(int(sr * 0.35), dtype=samples.dtype)]
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), np.concatenate(chunks), sr)
    return out_wav


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def make_synthesizer(cfg: PodcastCfg, root: Path):
    def _synth(lines: list[dict], out_wav: Path) -> Path:
        if cfg.backend == "kokoro":
            return synthesize_kokoro(lines, out_wav, cfg, root)
        return synthesize_sapi(lines, out_wav, cfg)
    return _synth

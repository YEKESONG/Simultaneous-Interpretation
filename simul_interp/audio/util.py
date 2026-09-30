"""音频小工具：音量计算、WAV 读写。"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from .base import SAMPLE_RATE


def rms_dbfs(samples: np.ndarray) -> float:
    """均方根音量，单位 dBFS（0 = 满刻度，越小越安静；全零返回 -120）。"""
    if len(samples) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return 20 * np.log10(rms) if rms > 1e-6 else -120.0


def meter(dbfs: float, width: int = 30) -> str:
    filled = int(round(max(0.0, min(1.0, (dbfs + 60) / 60)) * width))
    return "█" * filled + "·" * (width - filled)


def write_wav(path: Path, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(pcm.tobytes())

"""音频源：把“电脑正在播放的声音”或音频文件统一变成 16 kHz 单声道的小块。"""

from __future__ import annotations

import sys

from ..config import Config
from .base import CHUNK_SAMPLES, SAMPLE_RATE, AudioChunk, AudioSource

__all__ = ["AudioChunk", "AudioSource", "CHUNK_SAMPLES", "SAMPLE_RATE", "create_source"]


def create_source(cfg: Config) -> AudioSource:
    if cfg.audio.source == "system":
        if sys.platform == "darwin":
            from .macos_tap import MacSystemAudioSource

            return MacSystemAudioSource()
        raise RuntimeError(f"暂不支持在 {sys.platform} 上内录系统声音")
    raise ValueError(f"未知的音频源：{cfg.audio.source}（可选 system / file）")

"""语音识别：Whisper 后端 + 流式识别策略。"""

from __future__ import annotations

import sys

from ..config import Config
from .types import AsrBackend, AsrUpdate, Segment

__all__ = ["AsrBackend", "AsrUpdate", "Segment", "create_backend"]


def create_backend(cfg: Config) -> AsrBackend:
    backend = cfg.asr.backend
    if backend == "auto":
        backend = "mlx" if sys.platform == "darwin" else "faster-whisper"
    if backend == "mlx":
        from .mlx_backend import MlxWhisperBackend

        return MlxWhisperBackend(cfg.asr.model, cfg.asr.language)
    raise ValueError(f"未知的识别后端：{backend}")

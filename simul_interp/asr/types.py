"""识别模块共用的数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


@dataclass
class Segment:
    """Whisper 输出的一段：时间相对于送进去的这段音频的开头。"""

    start: float
    end: float
    text: str
    words: list[tuple[float, float, str]] = field(default_factory=list)  # 只有要求逐词时间戳时才有


class AsrBackend(Protocol):
    def load(self) -> None:
        """加载模型并预热。必须在之后调用 transcribe 的同一个线程里调用（MLX 的计算流是按线程分的）。"""

    def transcribe(
        self, audio: np.ndarray, prompt: str = "", word_timestamps: bool = False, max_tokens: int | None = None
    ) -> list[Segment]:
        """max_tokens：最多生成多少个 token，防止陷入重复循环时一直生成、解码时间暴涨。"""


@dataclass
class AsrUpdate:
    """流式识别每跑一次产出的结果。"""

    committed: str  # 这一次新确认的文字（确认后不会再变，可以送去翻译）
    partial: str  # 目前还没确认的尾巴（下一次可能会变，只用来显示）
    final: bool  # True = 这一句话已经说完（VAD 判定），committed 是这句话最后剩下的部分
    audio_end: float  # 这次结果覆盖到的音频流时间（秒）
    compute_s: float  # 这次识别花的时间（秒）
    wall: float  # 产出结果时的 time.monotonic()

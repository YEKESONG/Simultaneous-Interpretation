"""语音检测（VAD）：判断什么时候有人在说话、一句话什么时候说完。

分两层：
- SileroVad：模型本身，每 32 毫秒（512 个采样点）给出一个“有人说话”的概率；
- VadSegmenter：纯逻辑的状态机，把概率序列变成“开始 / 音频 / 结束”事件，不依赖模型，方便单元测试。

状态机用了两个阈值（迟滞）：概率高于 threshold 才算开始说话；说话中概率低于 threshold - 0.15
才开始计静音。这样概率在阈值附近抖动时不会反复开关。
"""

from __future__ import annotations

import math
import warnings
from collections import deque
from dataclasses import dataclass
from typing import Literal

import numpy as np

from .audio.base import CHUNK_SAMPLES, SAMPLE_RATE, AudioChunk

CHUNK_MS = CHUNK_SAMPLES * 1000 / SAMPLE_RATE  # 32 ms


class SileroVad:
    def __init__(self) -> None:
        import torch
        from silero_vad import load_silero_vad

        torch.set_num_threads(1)  # 每次只算 512 个点，多线程反而更慢，也会和识别抢 CPU
        self._torch = torch
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)  # silero-vad 内部用 torch.jit.load，新版 torch 会提示已弃用
            self._model = load_silero_vad()

    def __call__(self, samples: np.ndarray) -> float:
        with self._torch.no_grad():
            return float(self._model(self._torch.from_numpy(samples), SAMPLE_RATE))

    def reset(self) -> None:
        self._model.reset_states()


@dataclass
class VadEvent:
    type: Literal["start", "audio", "end"]
    time: float  # 音频流时间（秒）
    chunk: AudioChunk | None = None  # 只有 audio 事件带音频


class VadSegmenter:
    def __init__(self, threshold: float = 0.5, min_silence_ms: int = 400, speech_pad_ms: int = 200) -> None:
        self.threshold = threshold
        self.neg_threshold = max(threshold - 0.15, 0.01)
        # 向上取整：静音至少要达到设定时长（注意 Python 的 round(12.5) == 12）
        self.min_silence_chunks = max(1, math.ceil(min_silence_ms / CHUNK_MS))
        self._preroll: deque[AudioChunk] = deque(maxlen=max(0, round(speech_pad_ms / CHUNK_MS)))
        self.in_speech = False
        self._silent_chunks = 0

    def process(self, chunk: AudioChunk, prob: float) -> list[VadEvent]:
        if not self.in_speech:
            if prob >= self.threshold:
                self.in_speech = True
                self._silent_chunks = 0
                earlier = list(self._preroll)
                self._preroll.clear()
                start = earlier[0].start if earlier else chunk.start
                return [VadEvent("start", start)] + [VadEvent("audio", c.end, c) for c in earlier + [chunk]]
            self._preroll.append(chunk)
            return []

        # 说话中：静音的块也照样送给识别，词尾常常落在这些块里
        events = [VadEvent("audio", chunk.end, chunk)]
        if prob < self.neg_threshold:
            self._silent_chunks += 1
            if self._silent_chunks >= self.min_silence_chunks:
                self.in_speech = False
                self._silent_chunks = 0
                events.append(VadEvent("end", chunk.end))
        elif prob >= self.threshold:
            self._silent_chunks = 0
        return events

    def flush(self, time: float) -> list[VadEvent]:
        """音频源结束时调用：如果还在说话，补一个结束事件。"""
        if self.in_speech:
            self.in_speech = False
            return [VadEvent("end", time)]
        return []

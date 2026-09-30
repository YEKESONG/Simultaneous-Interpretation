"""音频源的共同基类。

所有音频源（macOS 内录、Windows 内录、音频文件）都输出同一种格式：
16 kHz、单声道、float32，切成每块 512 个采样点（32 毫秒）。
512 点正好是 Silero VAD 在 16 kHz 下要求的窗口长度，后面的语音检测可以直接用。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from ..clock import now

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512


@dataclass
class AudioChunk:
    samples: np.ndarray  # float32，长度 CHUNK_SAMPLES
    start: float  # 这一块在音频流里的起始时间（秒），按采样点数计算，不受处理快慢影响
    received_at: float  # 收到这一块的时刻（clock.now），用来统计延迟

    @property
    def end(self) -> float:
        return self.start + len(self.samples) / SAMPLE_RATE


class AudioSource:
    """子类在后台线程里调用 self._feed(samples) 送入任意长度的音频，
    基类负责切成固定大小的块、打上时间戳并放进队列；结束时调用 self._finish()。"""

    name = "audio"

    def __init__(self) -> None:
        self._queue: queue.Queue[AudioChunk | None] = queue.Queue()
        self._pending = np.zeros(0, dtype=np.float32)
        self._samples_emitted = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def reconnect(self) -> None:
        """重新连接音频设备（切换输出设备之后）。默认什么也不做。"""

    def chunks(self) -> Iterator[AudioChunk]:
        """阻塞地逐块产出音频，音频源结束或 stop() 之后退出。"""
        while True:
            item = self._queue.get()
            if item is None:
                return
            yield item

    def _feed(self, samples: np.ndarray) -> None:
        received = now()
        with self._lock:
            pending = np.concatenate([self._pending, np.asarray(samples, dtype=np.float32)])
            count = len(pending) // CHUNK_SAMPLES
            for i in range(count):
                block = pending[i * CHUNK_SAMPLES : (i + 1) * CHUNK_SAMPLES].copy()
                self._queue.put(AudioChunk(block, self._samples_emitted / SAMPLE_RATE, received))
                self._samples_emitted += CHUNK_SAMPLES
            self._pending = pending[count * CHUNK_SAMPLES :]

    def _finish(self) -> None:
        self._queue.put(None)

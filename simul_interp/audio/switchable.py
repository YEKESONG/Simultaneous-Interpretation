"""运行中可以换的音源：电脑播放的声音 ⇄ 麦克风。

里面套着一个真正的音源，把它送来的音频接着往下送。换音源时停掉旧的、启动新的；音频流的时间接着往下走，
所以录音（一直是同一个文件）和会话记录里的时间不会因为换音源而错开。新的打不开（比如没有麦克风权限）时，
换回原来那一种，同传不中断。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from .base import AudioSource

logger = logging.getLogger(__name__)


class SwitchableSource(AudioSource):
    def __init__(self, source: AudioSource) -> None:
        super().__init__()
        self._inner: AudioSource | None = source
        self._pump: threading.Thread | None = None
        self._lock_switch = threading.Lock()
        self._started = False
        self._stopped = False

    @property
    def name(self) -> str:  # type: ignore[override]
        inner = self._inner
        return inner.name if inner is not None else "音源"

    @property
    def current(self) -> AudioSource | None:
        return self._inner

    def start(self) -> None:
        with self._lock_switch:
            self._started = True
            self._run_inner(self._inner)

    def stop(self) -> None:
        with self._lock_switch:
            if self._stopped:
                return
            self._stopped = True
            inner, self._inner = self._inner, None
        if inner is not None and self._started:
            inner.stop()
        self._finish()

    def reconnect(self) -> None:
        inner = self._inner
        if inner is not None:
            inner.reconnect()

    def switch(self, source: AudioSource, restore: Callable[[], AudioSource]) -> None:
        """换成 source。它打不开时，用 restore() 新建一个原来那种音源接着用，再把错误抛给调用方。"""
        with self._lock_switch:
            if self._stopped:
                raise RuntimeError("同传已经结束")
            old, self._inner = self._inner, None  # 先摘下来：旧音源停下时，它的转送线程不能把整条音频流也结束掉
            if not self._started:  # 还没开始（模型还在加载）：换掉就行，开始时启动的就是新的
                self._inner = source
                return
            if old is not None:
                old.stop()
            if self._pump is not None:
                self._pump.join(timeout=2.0)
            try:
                self._run_inner(source)
            except Exception:
                logger.exception("新的音源打不开，换回原来的")
                self._run_inner(restore())
                raise

    def _run_inner(self, source: AudioSource) -> None:
        """（持锁调用）启动 source，并开一个线程把它的音频转送出去。"""
        source.on_status = self.on_status
        source.start()
        self._inner = source
        self._pump = threading.Thread(target=self._forward, args=(source,), name="audio-forward", daemon=True)
        self._pump.start()

    def _forward(self, source: AudioSource) -> None:
        for chunk in source.chunks():
            if source is self._inner:  # 刚被换下来的音源最后送来的几块不要了
                self._feed(chunk.samples)
        if source is self._inner and not self._stopped:  # 音源自己结束了（不是被换掉、也不是退出）
            self._finish()

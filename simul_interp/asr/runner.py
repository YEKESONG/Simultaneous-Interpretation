"""把“音频源 → 语音检测 → 流式识别”串起来，跑在两个线程里：

- listen 线程：逐块读音频、跑 VAD，把“开始 / 音频 / 结束”事件放进队列（每块只要 0.1 毫秒，不会积压）；
- 调用 run() 的线程：跑流式识别。识别一次要近 1 秒，这期间新来的事件在队列里排队，
  跑完后一次性取出处理，所以不会丢音频。

MLX 的计算流是按线程分的，模型的加载和每一次识别必须在同一个线程里，所以 run() 里先加载模型。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

from ..audio.base import AudioSource
from ..config import Config
from ..vad import SileroVad, VadEvent, VadSegmenter
from .streaming import StreamingTranscriber
from .types import AsrBackend, AsrUpdate

logger = logging.getLogger(__name__)


class AsrRunner:
    def __init__(self, cfg: Config, backend: AsrBackend, on_update: Callable[[AsrUpdate], None]) -> None:
        self.cfg = cfg
        self.backend = backend
        self.on_update = on_update
        self._events: queue.Queue[VadEvent | None] = queue.Queue()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self, source: AudioSource, on_started: Callable[[], None] | None = None) -> None:
        """阻塞运行，直到音频源结束或调用了 stop()。模型加载好、开始收听时调用 on_started。"""
        self.backend.load()
        transcriber = StreamingTranscriber(self.backend, max_buffer_s=self.cfg.asr.max_buffer_s)
        if self._stop.is_set():
            return
        source.start()
        listener = threading.Thread(target=self._listen, args=(source,), name="listen", daemon=True)
        listener.start()
        if on_started is not None:
            on_started()
        try:
            self._loop(transcriber)
        finally:
            source.stop()
            listener.join(timeout=3.0)

    def _listen(self, source: AudioSource) -> None:
        vad = SileroVad()
        segmenter = VadSegmenter(self.cfg.vad.threshold, self.cfg.vad.min_silence_ms, self.cfg.vad.speech_pad_ms)
        last = 0.0
        try:
            for chunk in source.chunks():
                if self._stop.is_set():
                    break
                last = chunk.end
                for event in segmenter.process(chunk, vad(chunk.samples)):
                    self._events.put(event)
            for event in segmenter.flush(last):
                self._events.put(event)
        finally:
            self._events.put(None)

    def _loop(self, transcriber: StreamingTranscriber) -> None:
        in_speech = False
        paused = False  # 刚出现短停顿：不等固定间隔，马上识别一次
        while not self._stop.is_set():
            batch = [self._events.get()]
            while True:
                try:
                    batch.append(self._events.get_nowait())
                except queue.Empty:
                    break
            for event in batch:
                if event is None:
                    return
                if event.type == "start":
                    transcriber.start_utterance(event.time)
                    in_speech = True
                elif event.type == "audio":
                    transcriber.add_audio(event.chunk.samples, speech=event.speech)
                elif event.type == "pause":
                    paused = True
                elif event.type == "end":
                    in_speech = paused = False
                    self.on_update(transcriber.finish(event.speech_end))
            # 停顿时马上识别一次：如果真是说完了，等 VAD 判定结束时，这次结果已经覆盖整句，可以直接确认
            due = transcriber.new_audio_s >= self.cfg.asr.step_s or (paused and transcriber.new_audio_s >= 0.1)
            if in_speech and due:
                paused = False
                update = transcriber.process()
                if update is not None:
                    self.on_update(update)

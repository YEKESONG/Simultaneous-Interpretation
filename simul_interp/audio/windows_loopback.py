"""Windows 内录：WASAPI loopback（PyAudioWPatch）。

- 录的是默认扬声器声音的“副本”，不改变声音的去向，戴耳机、用扬声器都照常能听到；
- 设备的原始格式通常是 48 kHz 双声道 float32，这里混成单声道，再用 soxr 流式重采样到 16 kHz；
- **WASAPI loopback 在没有声音播放时根本不回调**。如果不管，语音检测永远等不到“静音”，
  最后一句话就一直不结束；所以用一个定时线程按时间补静音（GapFiller）；
- PortAudio 只在初始化时枚举一次设备，录音流开着时发现不了默认输出设备的变化。
  切换耳机后，用菜单里的“重新连接音频设备”重新打开（切换耳机本身不影响听原声）。

注意：这部分代码没有在 Windows 实机上运行过；补静音的逻辑有单元测试，CI 在 Windows 上检查能否导入。
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from .base import SAMPLE_RATE, AudioSource

logger = logging.getLogger(__name__)


class GapFiller:
    """记录最后一次收到真实音频的时刻；超过 max_gap_s 没有新数据，就算出应该补多少个静音采样点。"""

    def __init__(self, max_gap_s: float = 0.1, sample_rate: int = SAMPLE_RATE) -> None:
        self.max_gap_s = max_gap_s
        self.sample_rate = sample_rate
        self._last: float | None = None

    def mark(self, now: float) -> None:
        """收到了真实音频（或者刚开始录）。"""
        self._last = now

    def missing_samples(self, now: float) -> int:
        if self._last is None or now - self._last < self.max_gap_s:
            return 0
        count = int((now - self._last) * self.sample_rate)
        self._last += count / self.sample_rate
        return count


class WindowsLoopbackSource(AudioSource):
    name = "Windows 系统声音"

    def __init__(self) -> None:
        super().__init__()
        self._pa = None
        self._stream = None
        self._channels = 2
        self._resampler = None
        self._gaps = GapFiller()
        self._feed_lock = threading.Lock()
        self._stop = threading.Event()
        self._filler: threading.Thread | None = None

    def start(self) -> None:
        self._open()
        self._gaps.mark(time.monotonic())
        self._filler = threading.Thread(target=self._fill_gaps, name="wasapi-gap-filler", daemon=True)
        self._filler.start()

    def stop(self) -> None:
        self._stop.set()
        if self._filler:
            self._filler.join(timeout=1.0)
        self._close()
        self._finish()

    def reconnect(self) -> None:
        """切换输出设备后调用：重新找默认扬声器对应的 loopback 设备。"""
        with self._feed_lock:
            self._close()
            self._open()

    def _open(self) -> None:
        import pyaudiowpatch as pyaudio
        import soxr

        self._pa = pyaudio.PyAudio()
        try:
            device = self._pa.get_default_wasapi_loopback()
        except (OSError, LookupError, ValueError, KeyError) as exc:  # WASAPI 不可用或找不到对应设备
            self._pa.terminate()
            raise RuntimeError("找不到默认扬声器对应的 WASAPI loopback 设备") from exc
        self._channels = int(device["maxInputChannels"])
        rate = int(device["defaultSampleRate"])
        self._resampler = soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32")
        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self._channels,
            rate=rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=int(rate * 0.02),
            stream_callback=self._callback,
        )
        logger.info("开始内录：%s，原始 %d Hz %d 声道 → %d Hz", device["name"], rate, self._channels, SAMPLE_RATE)

    def _close(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None

    def _callback(self, in_data, frame_count, time_info, status):
        import pyaudiowpatch as pyaudio

        frames = np.frombuffer(in_data, dtype=np.float32).reshape(-1, self._channels)
        mono = frames.mean(axis=1)
        with self._feed_lock:
            self._gaps.mark(time.monotonic())
            resampled = self._resampler.resample_chunk(mono)
            if len(resampled):
                self._feed(resampled)
        return (None, pyaudio.paContinue)

    def _fill_gaps(self) -> None:
        while not self._stop.wait(0.05):
            with self._feed_lock:
                count = self._gaps.missing_samples(time.monotonic())
                if count:
                    self._feed(np.zeros(count, dtype=np.float32))

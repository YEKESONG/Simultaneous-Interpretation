"""用音频文件模拟“正在播放的声音”：按真实语速一块一块送出，调试时效果和内录一样，而且可以重复。

有 ffmpeg 时可以读任何格式（mp3 / m4a / ogg …）；没有 ffmpeg 时只支持 16 kHz 单声道 16 位的 WAV。
"""

from __future__ import annotations

import itertools
import shutil
import subprocess
import threading
import time
import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from .base import CHUNK_SAMPLES, SAMPLE_RATE, AudioSource


class FileAudioSource(AudioSource):
    def __init__(self, path: str | Path, speed: float = 1.0, tail_silence_s: float = 1.5) -> None:
        """speed：1.0 = 实时；2.0 = 两倍速；0 = 不等待、尽快送完（只适合离线测试）。
        tail_silence_s：文件结束后补一段静音，让语音检测能判断最后一句已经说完。"""
        super().__init__()
        self.path = Path(path)
        self.speed = speed
        self.tail_silence_s = tail_silence_s
        self.name = f"文件 {self.path.name}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"找不到音频文件：{self.path}")
        self._thread = threading.Thread(target=self._run, name="file-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        tail = np.zeros(int(self.tail_silence_s * SAMPLE_RATE), dtype=np.float32)
        started = time.monotonic()
        sent = 0
        try:
            for block in itertools.chain(self._decode(), _split(tail)):
                if self._stop.is_set():
                    break
                if self.speed > 0:
                    # 等到这一块“播放完”的时刻再送出，和真实内录一样：声音总是播完才拿得到
                    due = started + (sent + len(block)) / SAMPLE_RATE / self.speed
                    delay = due - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                self._feed(block)
                sent += len(block)
        finally:
            self._finish()

    def _decode(self) -> Iterator[np.ndarray]:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            yield from _decode_with_ffmpeg(ffmpeg, self.path)
        else:
            yield from _decode_wav(self.path)


def _split(samples: np.ndarray) -> Iterator[np.ndarray]:
    for i in range(0, len(samples), CHUNK_SAMPLES):
        yield samples[i : i + CHUNK_SAMPLES]


def _decode_with_ffmpeg(ffmpeg: str, path: Path) -> Iterator[np.ndarray]:
    cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    try:
        leftover = b""
        while True:
            data = proc.stdout.read(CHUNK_SAMPLES * 4)
            if not data:
                break
            data = leftover + data
            usable = len(data) // 4 * 4
            leftover = data[usable:]
            yield np.frombuffer(data[:usable], dtype="<f4")
    finally:
        proc.kill()
        proc.wait()


def _decode_wav(path: Path) -> Iterator[np.ndarray]:
    with wave.open(str(path), "rb") as f:
        if (f.getframerate(), f.getnchannels(), f.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise RuntimeError(f"没有 ffmpeg 时只支持 16 kHz 单声道 16 位 WAV：{path}")
        while frames := f.readframes(CHUNK_SAMPLES):
            yield np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0

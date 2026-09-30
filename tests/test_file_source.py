import time

import numpy as np

from simul_interp.audio.base import CHUNK_SAMPLES, SAMPLE_RATE
from simul_interp.audio.file_source import FileAudioSource
from simul_interp.audio.util import write_wav


def make_tone(path, seconds):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    write_wav(path, 0.5 * np.sin(2 * np.pi * 440 * t).astype(np.float32))


def collect(source):
    source.start()
    chunks = list(source.chunks())
    source.stop()
    return chunks


def test_file_is_split_and_tail_silence_appended(tmp_path):
    wav = tmp_path / "tone.wav"
    make_tone(wav, 1.0)
    chunks = collect(FileAudioSource(wav, speed=0, tail_silence_s=0.5))
    total = sum(len(c.samples) for c in chunks)
    assert total == (int(1.5 * SAMPLE_RATE) // CHUNK_SAMPLES) * CHUNK_SAMPLES
    audio = np.concatenate([c.samples for c in chunks])
    assert np.abs(audio[: SAMPLE_RATE // 2]).max() > 0.4  # 前面是正弦波
    assert not np.any(audio[-SAMPLE_RATE // 4 :])  # 结尾补的是静音


def test_realtime_pacing(tmp_path):
    wav = tmp_path / "tone.wav"
    make_tone(wav, 0.5)
    started = time.monotonic()
    chunks = collect(FileAudioSource(wav, speed=1.0, tail_silence_s=0.0))
    elapsed = time.monotonic() - started
    # 按实时速度送，0.5 秒的音频大约要 0.5 秒送完；每一块都在“播放完”之后才送出。
    # 容差留到 20 ms：Python 3.12 及以前在 Windows 上 time.monotonic() 的精度只有 15.6 ms
    assert 0.45 < elapsed < 0.8
    for chunk in chunks:
        assert chunk.received_at - started >= chunk.end - 0.02

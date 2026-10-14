import time

import numpy as np

from simul_interp.audio.base import CHUNK_SAMPLES, SAMPLE_RATE
from simul_interp.audio.file_source import FileAudioSource
from simul_interp.audio.util import write_wav
from simul_interp.clock import now


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
    source = FileAudioSource(wav, speed=1.0, tail_silence_s=0.0)
    started = now()
    source.start()
    arrivals = [(now() - started, chunk.end) for chunk in source.chunks()]  # (拿到这一块的时刻, 它的结束时间)
    source.stop()
    # 按实时速度送，0.5 秒的音频大约要 0.5 秒送完；每一块都在“播放完”之后才送出
    assert 0.45 < arrivals[-1][0] < 0.8
    assert all(arrived >= end - 0.01 for arrived, end in arrivals)


def test_paused_file_waits_and_then_plays_everything(tmp_path):
    """用文件模拟时，暂停是让文件停在原地，继续后接着放，一点不丢（内录才是把暂停期间的声音丢掉）。"""
    wav = tmp_path / "tone.wav"
    make_tone(wav, 0.3)
    source = FileAudioSource(wav, speed=1.0, tail_silence_s=0.0)
    source.pause()
    source.start()
    time.sleep(0.25)
    assert source.position == 0  # 暂停着：一块都没送
    source.resume()
    chunks = list(source.chunks())
    source.stop()
    assert sum(len(c.samples) for c in chunks) == (int(0.3 * SAMPLE_RATE) // CHUNK_SAMPLES) * CHUNK_SAMPLES

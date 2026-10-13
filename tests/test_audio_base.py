import numpy as np

from simul_interp.audio.base import CHUNK_SAMPLES, SAMPLE_RATE, AudioSource
from simul_interp.audio.util import rms_dbfs


def drain(source: AudioSource):
    source._finish()
    return list(source.chunks())


def test_feed_splits_into_fixed_chunks_with_stream_time():
    source = AudioSource()
    # 送入长度不规则的几段，模拟内录每次回调拿到的数据长短不一
    for n in (100, 700, 300, 1000):
        source._feed(np.ones(n, dtype=np.float32))
    chunks = drain(source)
    assert len(chunks) == 2100 // CHUNK_SAMPLES
    assert all(len(c.samples) == CHUNK_SAMPLES for c in chunks)
    assert [c.start for c in chunks] == [i * CHUNK_SAMPLES / SAMPLE_RATE for i in range(len(chunks))]
    # 不足一块的尾巴留着，等下一次送入再拼
    assert len(source._pending) == 2100 % CHUNK_SAMPLES


def test_rms_dbfs():
    assert rms_dbfs(np.zeros(100, dtype=np.float32)) == -120.0
    assert abs(rms_dbfs(np.ones(100, dtype=np.float32))) < 1e-6
    assert abs(rms_dbfs(np.full(100, 0.1, dtype=np.float32)) + 20) < 1e-3


def test_pause_drops_audio_and_freezes_stream_time():
    """暂停期间的声音丢掉，音频流的时间也停住：录音里没有空档，记录里的时间不会和录音错开。"""
    source = AudioSource()
    source.pause()  # 还什么都没送过：没有话需要收尾，不补静音
    source._feed(np.ones(CHUNK_SAMPLES, dtype=np.float32))
    assert source.paused and source.position == 0
    source.resume()
    source._feed(np.ones(CHUNK_SAMPLES * 2, dtype=np.float32))
    source.pause(flush_s=2 * CHUNK_SAMPLES / SAMPLE_RATE)  # 先补两块静音，让正说到一半的话收尾
    source._feed(np.full(CHUNK_SAMPLES * 5, 0.5, dtype=np.float32))  # 暂停期间的声音
    assert source.position == 4 * CHUNK_SAMPLES / SAMPLE_RATE
    source.resume()
    source._feed(np.full(CHUNK_SAMPLES, 0.25, dtype=np.float32))
    chunks = drain(source)
    assert [float(c.samples[0]) for c in chunks] == [1, 1, 0, 0, 0.25]
    assert [c.start for c in chunks] == [i * CHUNK_SAMPLES / SAMPLE_RATE for i in range(5)]  # 时间是连着的

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

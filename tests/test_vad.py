import numpy as np

from simul_interp.audio.base import CHUNK_SAMPLES, SAMPLE_RATE, AudioChunk
from simul_interp.vad import VadSegmenter

DT = CHUNK_SAMPLES / SAMPLE_RATE  # 0.032 秒


def run(probs, **kwargs):
    seg = VadSegmenter(**kwargs)
    events = []
    for i, p in enumerate(probs):
        chunk = AudioChunk(np.zeros(CHUNK_SAMPLES, np.float32), i * DT, 0.0)
        events += seg.process(chunk, p)
    return events, seg


def kinds(events):
    return [e.type for e in events if e.type != "audio"]


def test_start_with_preroll_and_end_after_min_silence():
    probs = [0.0] * 20 + [0.9] * 20 + [0.0] * 20
    events, _ = run(probs, min_silence_ms=400, speech_pad_ms=200)
    assert kinds(events) == ["start", "end"]
    start = events[0]
    # 句首往前多留 200 ms ≈ 6 块
    assert abs(start.time - (20 - 6) * DT) < 1e-9
    end = next(e for e in events if e.type == "end")
    # 静音 400 ms ≈ 13 块之后才判定结束；真正停止说话是在静音开始的那一刻
    assert abs(end.time - (40 + 13) * DT) < 1e-9
    assert abs(end.speech_end - 40 * DT) < 1e-9
    audio = [e for e in events if e.type == "audio"]
    assert len(audio) == 6 + 20 + 13


def test_short_pause_does_not_split_sentence():
    probs = [0.9] * 10 + [0.0] * 5 + [0.9] * 10 + [0.0] * 20  # 中间停 160 ms
    events, _ = run(probs, min_silence_ms=400)
    assert kinds(events) == ["start", "end"]


def test_hysteresis_keeps_speech_between_thresholds():
    probs = [0.9] * 5 + [0.4] * 30 + [0.0] * 20  # 0.4 介于 0.35 和 0.5 之间，仍算在说话
    events, _ = run(probs, threshold=0.5, min_silence_ms=400)
    end = next(e for e in events if e.type == "end")
    assert end.time > 35 * DT


def test_flush_closes_open_utterance():
    events, seg = run([0.9] * 10)
    assert kinds(events) == ["start"]
    assert [e.type for e in seg.flush(1.0)] == ["end"]
    assert seg.flush(1.0) == []

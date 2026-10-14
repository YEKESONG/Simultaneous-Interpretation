"""Windows 专用部分的逻辑测试。在任何系统上都能跑：不需要 PyAudioWPatch，也不需要真的 faster-whisper。"""

import sys
import threading
import time
import types

import numpy as np
import pytest

from simul_interp.asr.faster_whisper_backend import FasterWhisperBackend
from simul_interp.audio.windows_loopback import GapFiller, WindowsLoopbackSource


def test_gap_filler_adds_silence_only_after_long_gaps():
    gaps = GapFiller(max_gap_s=0.1, sample_rate=16000)
    assert gaps.missing_samples(0.0) == 0  # 还没开始
    gaps.mark(10.0)
    assert gaps.missing_samples(10.05) == 0  # 50 ms 的间隔是正常的回调抖动，不补
    assert abs(gaps.missing_samples(10.3) - 4800) <= 1  # 300 ms 没有数据（没在放声音）：补 0.3 秒静音
    assert gaps.missing_samples(10.35) == 0  # 已经补到 10.3 秒
    assert abs(gaps.missing_samples(10.45) - 2400) <= 1
    gaps.mark(10.5)  # 又有声音了
    assert gaps.missing_samples(10.55) == 0


def test_loopback_stop_closes_stream_only_once():
    """退出时主线程和识别线程会同时调用 stop()；同一个 PortAudio 流关两次会让程序崩溃（S29）。"""
    src = WindowsLoopbackSource()
    closed = []
    src._close = lambda: (time.sleep(0.05), closed.append(1))  # 拉长关流的时间，让两个线程撞上
    src._open = lambda: pytest.fail("停止以后不应该再打开")
    threads = [threading.Thread(target=src.stop) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert closed == [1]
    src.reconnect()  # 停止以后再点“重新连接”：什么都不做
    assert list(src.chunks()) == []  # 音频流正常结束


class FakeWord:
    def __init__(self, start, end, word, probability=0.9):
        self.start, self.end, self.word, self.probability = start, end, word, probability


class FakeSegment:
    def __init__(self, start, end, text, words=None):
        self.start, self.end, self.text, self.words = start, end, text, words


class FakeWhisperModel:
    """模仿 faster_whisper.WhisperModel 的接口，记下收到的参数。"""

    last = None

    def __init__(self, name, device, compute_type, local_files_only):
        if name == "not-downloaded" and local_files_only:
            raise OSError("model not found in local cache")
        self.init_args = dict(name=name, device=device, compute_type=compute_type, local_files_only=local_files_only)
        self.calls = []
        FakeWhisperModel.last = self

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        words = [FakeWord(0.0, 0.3, " Bonjour"), FakeWord(0.3, 0.5, " d"), FakeWord(0.5, 0.7, "'être"), FakeWord(0.7, 1.0, " là.")]
        segments = [
            FakeSegment(0.0, 1.0, " Bonjour d'être là.", words),
            FakeSegment(1.0, 2.0, " Sous-titrage ST' 501"),  # 典型幻觉，应被过滤
        ]
        return iter(segments), None  # 真实接口返回的是生成器


@pytest.fixture
def fake_faster_whisper(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeWhisperModel))


def test_faster_whisper_backend_options_and_conversion(fake_faster_whisper):
    backend = FasterWhisperBackend("large-v3", "fr", device="cuda", compute_type="float16")
    backend.load()
    model = FakeWhisperModel.last
    assert model.init_args["local_files_only"] is True  # 默认不自动下载
    segments = backend.transcribe(np.zeros(16000, np.float32), prompt="Salut", word_timestamps=True, max_tokens=50)
    options = model.calls[-1]
    assert options["beam_size"] == 1 and options["temperature"] == 0.0  # 和 MLX 后端一样：贪心、只解码一次
    assert options["initial_prompt"] == "Salut" and options["max_new_tokens"] == 50
    assert options["language"] == "fr" and options["vad_filter"] is False
    assert len(segments) == 1  # 幻觉分段被丢掉
    assert [w[2] for w in segments[0].words] == ["Bonjour", "d'être", "là."]  # 省音合并回一个词


def test_missing_model_gives_clear_message(fake_faster_whisper):
    with pytest.raises(RuntimeError, match="allow_download"):
        FasterWhisperBackend("not-downloaded", "fr").load()


def test_faster_whisper_word_probability(fake_faster_whisper):
    backend = FasterWhisperBackend("large-v3", "fr")
    backend.load()
    audio = np.zeros(16000, np.float32)
    assert backend.word_probability(audio, "bonjour") == 0.9  # 第一个词就是它：用逐词时间戳里自带的概率
    options = FakeWhisperModel.last.calls[-1]
    assert options["word_timestamps"] is True and options["language"] == "fr"
    assert backend.word_probability(audio, "merci") == 0.0  # 第一个词不是它

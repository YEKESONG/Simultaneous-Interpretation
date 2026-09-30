"""Windows 专用部分的逻辑测试。在任何系统上都能跑：不需要 PyAudioWPatch，也不需要真的 faster-whisper。"""

import sys
import types

import numpy as np
import pytest

from simul_interp.asr.faster_whisper_backend import FasterWhisperBackend
from simul_interp.audio.windows_loopback import GapFiller


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


class FakeWord:
    def __init__(self, start, end, word):
        self.start, self.end, self.word = start, end, word


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

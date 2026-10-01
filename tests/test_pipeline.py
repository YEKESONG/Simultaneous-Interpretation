"""整条流水线的集成测试：音频源、语音检测、识别后端都换成假的，不需要模型，CI 上也能跑。

测试音频里没有真的声音：第 k 个词被“编码”成一段幅度为 (k+1)/100 的信号，
假的识别后端靠幅度认词，所以每个词在音频里的确切位置是已知的。
"""

import wave

import numpy as np
import pytest

from simul_interp import pipeline
from simul_interp.asr import runner
from simul_interp.asr.types import Segment
from simul_interp.audio.base import CHUNK_SAMPLES, SAMPLE_RATE, AudioSource
from simul_interp.config import Config

WORDS = "Bonjour à tous. Merci beaucoup.".split()
TIMES = [(1.0, 1.3), (1.36, 1.66), (1.72, 2.02), (3.5, 3.8), (3.86, 4.3)]  # 每个词的 (开始, 结束) 秒


def make_audio():
    audio = np.zeros(6 * SAMPLE_RATE, dtype=np.float32)
    for k, (start, end) in enumerate(TIMES):
        audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)] = (k + 1) / 100
    return audio


class FakeSource(AudioSource):
    name = "测试音源"

    def start(self):
        self._feed(make_audio())
        self._finish()

    def stop(self):
        pass


class FakeVad:
    def __call__(self, samples):
        return 1.0 if np.any(samples) else 0.0


class LevelBackend:
    """假的识别后端：靠幅度认词；还没说完的词只“听”到前一半（模拟不稳定的尾巴）。"""

    def load(self):
        pass

    def transcribe(self, audio, prompt="", word_timestamps=False, max_tokens=None):
        levels = np.rint(np.asarray(audio) * 100).astype(int)
        if len(levels) == 0:
            return []
        edges = [0, *(np.flatnonzero(np.diff(levels)) + 1), len(levels)]
        segments, current = [], []
        for a, b in zip(edges, edges[1:]):
            if levels[a] == 0:
                continue
            word = WORDS[levels[a] - 1]
            if b == len(levels):
                word = word[: max(1, len(word) // 2)]
            current.append((a / SAMPLE_RATE, b / SAMPLE_RATE, word))
            if word.endswith("."):
                segments.append(self._segment(current, word_timestamps))
                current = []
        if current:
            segments.append(self._segment(current, word_timestamps))
        return segments

    @staticmethod
    def _segment(items, word_timestamps):
        return Segment(items[0][0], items[-1][1], " ".join(w for _, _, w in items), items if word_timestamps else [])


class View:
    def __init__(self):
        self.units, self.status = {}, []

    def on_live(self, pending, partial, translation=""):
        pass

    def on_unit(self, unit):
        self.units[unit.id] = unit

    def on_status(self, text):
        self.status.append(text)


def read_wav(path):
    with wave.open(str(path), "rb") as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype="<i2")


@pytest.fixture
def session(tmp_path, monkeypatch):
    """返回一个函数：按给定的选项建一个 Interpreter（内录、不翻译），输出都写到临时目录。"""
    monkeypatch.setattr(pipeline, "create_source", lambda cfg: FakeSource())
    monkeypatch.setattr(pipeline, "create_backend", lambda cfg: LevelBackend())
    monkeypatch.setattr(runner, "SileroVad", FakeVad)

    def make(cfg=None, **options):
        cfg = cfg or Config()
        cfg.transcript.dir = str(tmp_path / "记录")
        cfg.recording.dir = str(tmp_path / "录音")
        view = View()
        return pipeline.Interpreter(cfg, view, translate=False, **options), view

    return make


def test_live_session_saves_a_recording_paired_with_its_transcript(session):
    interpreter, view = session()
    interpreter.run()
    recording, transcript = interpreter.recorder.path, interpreter.transcript.path
    assert recording.parent.name == "录音" and transcript.parent.name == "记录"
    assert transcript.name == f"{recording.stem}_译文.md"  # 同一个名字，方便配对
    assert f"- 录音：{recording}" in transcript.read_text(encoding="utf-8")
    assert [view.units[i].source for i in sorted(view.units)] == ["Bonjour à tous.", "Merci beaucoup."]
    # 识别听到的音频原样存了下来（不足一块的零头不算）
    audio = make_audio()
    usable = len(audio) // CHUNK_SAMPLES * CHUNK_SAMPLES
    assert np.array_equal(read_wav(recording), (audio[:usable] * 32767.0).astype("<i2"))
    assert not interpreter.recorder.active
    # 记录里的时间是音频流（录音）里的位置，落在这段话开头之前一点，不受启动、加载模型花了多久的影响
    first, second = view.units[0].audio_start, view.units[1].audio_start
    assert 0.0 <= first <= TIMES[0][0] and TIMES[2][1] - 1.0 <= second <= TIMES[3][0]
    text = transcript.read_text(encoding="utf-8")
    assert "**[00:00]** Bonjour à tous." in text and "- 时间：" in text
    assert f"**[00:0{int(second)}]** Merci beaucoup." in text


def test_no_recording_when_switched_off_or_reading_from_a_file(session, tmp_path):
    assert session(save_recording=False)[0].recorder is None  # 命令行 --no-recording
    off = Config()
    off.recording.enabled = False
    assert session(off)[0].recorder is None  # 配置里关掉
    from_file = Config()
    from_file.audio.source, from_file.audio.file = "file", "某个目录/demo.m4a"
    interpreter, _ = session(from_file)
    assert interpreter.recorder is None  # 用音频文件做输入：文件本身就是录音
    assert interpreter.transcript.path.name == "demo_译文.md"
    assert not (tmp_path / "录音").exists()


def test_recording_failure_does_not_stop_interpretation(session):
    interpreter, view = session()

    class FullDisk:
        def write(self, data):
            raise OSError("No space left on device")

        def close(self):
            pass

    interpreter.recorder._file.close()
    interpreter.recorder._file = FullDisk()
    interpreter.run()
    assert "录音已停止：No space left on device" in view.status
    assert len(view.units) == 2  # 同传照常进行

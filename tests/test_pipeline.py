"""整条流水线的集成测试：音频源、语音检测、识别后端都换成假的，不需要模型，CI 上也能跑。

测试音频里没有真的声音：第 k 个词被“编码”成一段幅度为 (k+1)/100 的信号，
假的识别后端靠幅度认词，所以每个词在音频里的确切位置是已知的。
"""

import threading
import time
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
# 另一段：说了半句（没有句号），停一秒，再接着说完
HALTING_WORDS = "Nous allons voir la suite.".split()
HALTING_TIMES = [(1.0, 1.3), (1.36, 1.66), (1.72, 2.02), (3.0, 3.3), (3.36, 3.8)]


def make_audio(times=TIMES):
    audio = np.zeros(6 * SAMPLE_RATE, dtype=np.float32)
    for k, (start, end) in enumerate(times):
        audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)] = (k + 1) / 100
    return audio


class FakeSource(AudioSource):
    name = "测试音源"

    def __init__(self, times=TIMES):
        super().__init__()
        self.times = times

    def start(self):
        self._feed(make_audio(self.times))
        self._finish()

    def stop(self):
        pass


class ManualSource(AudioSource):
    """由测试自己一段一段地送音频。"""

    name = "测试音源"

    def start(self):
        pass

    def stop(self):
        pass


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


class FakeVad:
    def __call__(self, samples):
        return 1.0 if np.any(samples) else 0.0


class LevelBackend:
    """假的识别后端：靠幅度认词；还没说完的词只“听”到前一半（模拟不稳定的尾巴）。"""

    def __init__(self, words=WORDS, language="fr"):
        self.words = words
        self.language = language
        self.prompts = []  # 每次识别拿到的上文提示

    def load(self):
        pass

    def transcribe(self, audio, prompt="", word_timestamps=False, max_tokens=None):
        self.prompts.append(prompt)
        levels = np.rint(np.asarray(audio) * 100).astype(int)
        if len(levels) == 0:
            return []
        edges = [0, *(np.flatnonzero(np.diff(levels)) + 1), len(levels)]
        segments, current = [], []
        for a, b in zip(edges, edges[1:]):
            if levels[a] == 0:
                continue
            word = self.words[levels[a] - 1]
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
        self.live = getattr(self, "live", []) + [(pending, partial)]

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
    script = {"words": WORDS, "times": TIMES, "source": None}
    monkeypatch.setattr(pipeline, "create_source", lambda cfg: script["source"] or FakeSource(script["times"]))
    monkeypatch.setattr(pipeline, "create_backend", lambda cfg: LevelBackend(script["words"], cfg.asr.language))
    monkeypatch.setattr(runner, "SileroVad", FakeVad)

    def make(cfg=None, words=WORDS, times=TIMES, source=None, **options):
        script.update(words=words, times=times, source=source)
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


def test_unfinished_sentence_is_joined_with_its_continuation(session):
    """说了半句、停一下、接着说完：默认合成一段再翻译；把等待关掉（0 秒）就和以前一样切成两段。"""
    interpreter, view = session(words=HALTING_WORDS, times=HALTING_TIMES)
    interpreter.run()
    assert [view.units[i].source for i in sorted(view.units)] == ["Nous allons voir la suite."]
    assert view.units[0].audio_start <= HALTING_TIMES[0][0]  # 时间仍然是这段话开头的位置

    no_wait = Config()
    no_wait.translate.incomplete_wait_s = 0
    interpreter, view = session(no_wait, words=HALTING_WORDS, times=HALTING_TIMES)
    interpreter.run()
    assert [view.units[i].source for i in sorted(view.units)] == ["Nous allons voir", "la suite."]


def test_pause_ignores_what_is_said_and_resume_picks_up_again(session):
    source = ManualSource()
    # 两句话，分别在第 1~2 秒和第 3.5~5.3 秒。第二句长一些：一段话超过 1.5 秒，识别时才会带上文提示
    words = "Bonjour à tous. Merci beaucoup à vous tous.".split()
    times = [*TIMES[:3], (3.5, 3.8), (3.86, 4.16), (4.22, 4.52), (4.58, 4.88), (4.94, 5.3)]
    interpreter, view = session(words=words, source=source)
    worker = threading.Thread(target=interpreter.run, daemon=True)  # daemon：中途有断言失败时不会把测试进程挂住
    worker.start()
    audio = make_audio(times)
    first, second = audio[: 3 * SAMPLE_RATE], audio[3 * SAMPLE_RATE :]
    assert wait_for(lambda: interpreter.state == "listening")
    source._feed(first)
    assert wait_for(lambda: 0 in view.units)  # “Bonjour à tous.”

    interpreter.pause()
    assert interpreter.state == "paused" and view.status[-1] == "已暂停"
    position = interpreter.source.position  # 整条音频流（也就是录音）走到了哪里
    source._feed(second)  # 暂停期间说的第二句：不识别，也不录
    time.sleep(0.2)
    assert len(view.units) == 1 and interpreter.source.position == position

    interpreter.resume()
    assert interpreter.state == "listening" and view.status[-1] == ""
    asked = len(interpreter.backend.prompts)
    source._feed(second)  # 继续之后再说一遍：照常出来
    assert wait_for(lambda: 1 in view.units)
    # 暂停前后的声音接不上：暂停前的句子不再当上文（否则模型容易照着上文“复述”，而不是听写）
    assert len(interpreter.backend.prompts) > asked
    assert not any("Bonjour" in prompt for prompt in interpreter.backend.prompts[asked:])
    source._finish()
    worker.join(5)
    assert [view.units[i].source for i in sorted(view.units)] == ["Bonjour à tous.", "Merci beaucoup à vous tous."]
    assert interpreter.state == "ended"
    text = interpreter.transcript.path.read_text(encoding="utf-8")
    assert text.index("Bonjour à tous.") < text.index("暂停后从这里继续") < text.index("Merci beaucoup à vous tous.")
    # 录音里没有暂停的那一段，所以记录里的时间仍然对得上录音
    assert len(read_wav(interpreter.recorder.path)) == round(interpreter.source.position * SAMPLE_RATE)


def test_starting_paused_waits_for_resume(session):
    interpreter, view = session(start_paused=True)
    interpreter.run()  # 这个假音源一启动就把音频全送完了，而这时还没点“开始监听”
    assert view.units == {} and interpreter.recorder.seconds == 0
    assert any("开始监听" in text for text in view.status)


def test_languages_can_be_switched_at_run_time(session):
    interpreter, view = session()
    interpreter.set_languages("en", "English")
    assert interpreter.backend.language == "en"  # 识别换成英语（模型是多语言的，不用重新加载）
    assert interpreter.cfg.translate.target_language == "English"
    interpreter.run()
    text = interpreter.transcript.path.read_text(encoding="utf-8")
    assert "*—— 00:00 起：英语 → English ——*" in text  # 记录里留一行说明
    assert text.index("英语 → English") < text.index("Bonjour à tous.")
    assert interpreter.output_folders() == [interpreter.transcript.path.parent, interpreter.recorder.path.parent]


def test_input_can_be_switched_at_run_time(session, monkeypatch):
    first = ManualSource()
    interpreter, view = session(source=first)
    worker = threading.Thread(target=interpreter.run, daemon=True)
    worker.start()
    assert wait_for(lambda: interpreter.state == "listening")
    first._feed(make_audio()[: 3 * SAMPLE_RATE])  # “Bonjour à tous.”
    assert wait_for(lambda: 0 in view.units)

    mic = ManualSource()
    mic.name = "麦克风（测试）"
    monkeypatch.setattr(pipeline, "create_source", lambda cfg: mic)
    interpreter.set_source("mic", "测试")
    assert (interpreter.cfg.audio.source, interpreter.cfg.audio.device) == ("mic", "测试")
    assert interpreter.source.name == "麦克风（测试）"

    def broken(cfg):
        raise RuntimeError("没有找到「不存在」这个麦克风")

    monkeypatch.setattr(pipeline, "create_source", broken)
    with pytest.raises(RuntimeError, match="不存在"):
        interpreter.set_source("mic", "不存在")
    assert interpreter.cfg.audio.device == "测试"  # 找不到新的：配置和正在用的音源都不变

    mic._feed(make_audio()[3 * SAMPLE_RATE :])  # 换成麦克风以后说的 “Merci beaucoup.”
    assert wait_for(lambda: 1 in view.units)
    mic._finish()
    worker.join(5)
    text = interpreter.transcript.path.read_text(encoding="utf-8")
    assert text.index("Bonjour à tous.") < text.index("音源改为「麦克风（测试）」") < text.index("Merci beaucoup.")
    assert len(read_wav(interpreter.recorder.path)) == round(interpreter.source.position * SAMPLE_RATE)


def test_input_of_a_file_session_cannot_be_switched(session):
    from_file = Config()
    from_file.audio.source, from_file.audio.file = "file", "demo.wav"
    interpreter, _ = session(from_file)
    with pytest.raises(RuntimeError, match="音频文件"):
        interpreter.set_source("mic")


def test_japanese_is_split_into_characters_and_shown_without_spaces(session):
    """日语不用空格分词：按字确认、按“。”切句，显示和送去翻译的原文都不带空格。"""
    cfg = Config()
    cfg.asr.language = "ja"
    words = ["今日は", "晴れ", "です。", "ありがとう", "ございます。"]
    times = [(1.0, 1.3), (1.36, 1.66), (1.72, 2.02), (3.5, 3.9), (3.96, 4.3)]
    interpreter, view = session(cfg, words=words, times=times)
    interpreter.run()
    assert [view.units[i].source for i in sorted(view.units)] == ["今日は晴れです。", "ありがとうございます。"]
    assert all(" " not in pending + partial for pending, partial in view.live)  # 正在说的那一行也不带空格
    text = interpreter.transcript.path.read_text(encoding="utf-8")
    assert "今日は晴れです。" in text and "- 识别语言：日语" in text

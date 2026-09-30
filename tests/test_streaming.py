import numpy as np

from simul_interp.asr.streaming import StreamingTranscriber, align_prefix, repeated_head, strip_repetition
from simul_interp.asr.types import Segment
from simul_interp.audio.base import SAMPLE_RATE

SCRIPT = "Bonjour à tous. Nous allons parler de l'intelligence artificielle. Merci beaucoup."


def timeline(text, word_s=0.3, gap_s=0.05):
    """给剧本里的每个词排上时间：每个词 0.3 秒，词间隔 0.05 秒。"""
    words, t = [], 0.2
    for w in text.split():
        words.append((t, t + word_s, w))
        t += word_s + gap_s
    return words


class FakeBackend:
    """按剧本“识别”：只返回缓冲区里已经完整听到的词；正说到一半的词被截成半个（模拟不稳定的尾巴）。"""

    def __init__(self, words, hallucinate_on_silence=False):
        self.words = words
        self.calls = 0
        self.transcriber = None
        self.hallucinate_on_silence = hallucinate_on_silence  # 模拟 Whisper 对着句尾静音“补”一句 Merci.

    def load(self):
        pass

    def transcribe(self, audio, prompt="", word_timestamps=False, max_tokens=None):
        self.calls += 1
        t0 = self.transcriber.buffer_start
        t1 = t0 + len(audio) / SAMPLE_RATE
        items = []
        for s, e, w in self.words:
            if s < t0 - 0.01:
                continue
            if e <= t1:
                items.append((s - t0, e - t0, w))
            elif s < t1:
                items.append((s - t0, t1 - t0, w[: max(1, len(w) // 2)]))
        last_end = max((e for _, e, _ in self.words), default=0.0)
        # Whisper 对着“整段都是静音”或“词后面拖着一段静音”的音频，常会补一句 Merci.
        if self.hallucinate_on_silence and (not items or t1 - last_end > 0.3):
            items.append((max(0.0, last_end - t0) + 0.05, t1 - t0, "Merci."))
        segments, current = [], []
        for item in items:
            current.append(item)
            if item[2].endswith("."):
                segments.append(self._segment(current, word_timestamps))
                current = []
        if current:
            segments.append(self._segment(current, word_timestamps))
        return segments

    @staticmethod
    def _segment(items, word_timestamps):
        return Segment(items[0][0], items[-1][1], " ".join(w for _, _, w in items), items if word_timestamps else [])


def make(words, **kwargs):
    backend = FakeBackend(words)
    transcriber = StreamingTranscriber(backend, **kwargs)
    backend.transcriber = transcriber
    return backend, transcriber


def feed(transcriber, seconds):
    transcriber.add_audio(np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32))


def test_align_prefix_tolerates_small_changes():
    committed = "nous allons parler de".split()
    assert align_prefix(committed, "Nous allons parlé de l'intelligence".split()) == 4
    assert align_prefix(committed, "nous, allons parler de la".split()) == 4
    assert align_prefix("a b c d".split(), "a b x".split()) == 3  # 新结果比已确认的还短，也不能出错


def test_repeated_head():
    assert repeated_head("tous. Nous allons".split(), "Bonjour à tous.".split()) == 1
    assert repeated_head("Nous allons".split(), "Bonjour à tous.".split()) == 0


def test_strip_repetition_loop():
    looped = "notons que la première version de la version de la version de la version de".split()
    kept = strip_repetition(looped)
    assert looped[: len(kept)] == kept  # 只截断，不改前面的内容
    assert " ".join(kept).count("version") <= 2
    assert strip_repetition("non non non".split()) == "non non non".split()  # 单个词重复 3 次可能是真的
    normal = "nous allons faire le point sur le projet".split()
    assert strip_repetition(normal) == normal


def test_no_final_pass_on_silence_after_sentence_trim():
    words = timeline("Bonjour à tous. Merci beaucoup.")
    backend = FakeBackend(words, hallucinate_on_silence=True)
    t = StreamingTranscriber(backend)
    backend.transcriber = t
    t.start_utterance(0.0)
    committed = []
    # 两句话到 1.9 秒说完；最后两次识别在 1.92 和 1.96 秒，“beaucoup.” 两次一致被确认，缓冲区在句末切过
    for until in (0.6, 1.0, 1.3, 1.6, 1.92, 1.96):
        feed(t, until - t.buffer_end)
        update = t.process()
        if update and update.committed:
            committed += update.committed.split()
    assert committed == "Bonjour à tous. Merci beaucoup.".split()
    assert t.buffer_s < 0.2  # 切完只剩句末的一点静音
    calls = backend.calls
    # VAD 判定说完时，上一次识别（1.96 秒）没有覆盖到“说完 + 0.1 秒”，以前会对着剩下的静音再识别一次
    final = t.finish(speech_end=words[-1][1])
    assert final.committed == "" and backend.calls == calls  # 现在不再识别，也就不会“补”出 Merci.


def test_word_is_committed_only_after_two_agreeing_passes():
    words = timeline(SCRIPT)
    _, t = make(words)
    t.start_utterance(0.0)
    feed(t, 0.62)  # 听到 “Bonjour”（0.2~0.5 秒）
    first = t.process()
    assert first.committed == ""  # 第一次出现，只是暂定
    assert first.partial.startswith("Bonjour")
    feed(t, 0.6)
    second = t.process()
    assert second.committed.startswith("Bonjour")  # 两次一致才确认


def test_full_stream_has_no_duplicates_or_losses_and_trims_buffer():
    words = timeline(SCRIPT)
    backend, t = make(words)
    t.start_utterance(0.0)
    committed = []
    trimmed = False
    for _ in range(12):
        feed(t, 0.6)
        update = t.process()
        if update and update.committed:
            committed += update.committed.split()
        trimmed = trimmed or t.buffer_start > 0
    speech_end = words[-1][1]
    final = t.finish(speech_end)
    committed += final.committed.split()
    assert committed == SCRIPT.split()
    assert trimmed  # 整句确认后缓冲区被切过
    assert "tous." in t.history


def test_finish_reuses_last_pass_when_it_covers_speech_end():
    words = timeline("Bonjour à tous.")
    backend, t = make(words)
    t.start_utterance(0.0)
    feed(t, 1.6)  # 已经听完整句（结束于 1.25 秒）
    t.process()
    calls = backend.calls
    final = t.finish(speech_end=words[-1][1])
    assert backend.calls == calls  # 没有再识别一次
    assert final.final and final.committed.endswith("tous.")


def test_final_pass_ignores_trailing_silence():
    words = timeline("Bonjour à tous.")
    backend = FakeBackend(words, hallucinate_on_silence=True)
    t = StreamingTranscriber(backend)
    backend.transcriber = t
    t.start_utterance(0.0)
    feed(t, 1.0)  # 最后一个词还没说完
    t.process()
    feed(t, 0.7)  # 说完了，后面跟着 400 ms 以上的静音
    final = t.finish(speech_end=words[-1][1])
    assert "Merci" not in final.committed  # 截掉句尾静音后，最后一次识别不会“补”出 Merci.
    assert final.committed.endswith("tous.")


def test_too_short_utterance_is_ignored():
    backend, t = make(timeline("Oui."))
    t.start_utterance(0.0)
    feed(t, 0.8)  # 缓冲区有 0.8 秒（含预留和静音），但真正说话只到 0.4 秒
    final = t.finish(speech_end=0.4)
    assert final.committed == "" and backend.calls == 0


def test_cut_moves_to_the_nearest_pause():
    # 1 秒“说话”（噪声）、0.1 秒停顿（1.0~1.1 秒）、再 1 秒说话
    rng = np.random.default_rng(0)
    speech = lambda s: rng.normal(0, 0.3, int(s * SAMPLE_RATE)).astype(np.float32)  # noqa: E731
    _, t = make([])
    t.start_utterance(0.0)
    t.add_audio(np.concatenate([speech(1.0), np.zeros(int(0.1 * SAMPLE_RATE), np.float32), speech(1.0)]))
    # 时间戳说词在 0.88 秒结束（估早了），直接切会切在词中间；应该挪到真正的停顿里
    assert 1.0 <= t.quiet_point(0.88) <= 1.1


def test_force_trim_when_no_sentence_end():
    words = timeline(" ".join(f"mot{i}" for i in range(60)))  # 一直说、没有句号，约 21 秒（词各不相同，不是重复循环）
    backend, t = make(words, max_buffer_s=6.0)
    t.start_utterance(0.0)
    committed = []
    for _ in range(40):
        feed(t, 0.6)
        update = t.process()
        if update and update.committed:
            committed += update.committed.split()
        assert t.buffer_s < 6.0 + 1.0  # 缓冲区不会无限增长
    committed += t.finish(words[-1][1]).committed.split()
    assert len(committed) == 60

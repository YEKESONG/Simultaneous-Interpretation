"""流式识别：让一次只能处理整段音频的 Whisper 做到“边听边出字”。

思路（LocalAgreement，参考 ufal/whisper_streaming 的论文和实现，这里按本项目需要重写）：
1. 说话期间每隔一小段时间，把缓冲区（这句话到目前为止的音频）整段重新识别一次；
2. 本次结果和上一次结果开头一致的部分算“确认”，确认后不再改，可以送去翻译；
3. 不一致的尾巴是“暂定”，只用来显示，下一次可能会变；
4. 一整句确认完（Whisper 的分段以句号、问号等结尾），就把这段音频从缓冲区切掉，
   已确认的文字作为提示词带给下一次识别，所以缓冲区一直很短，每次识别都快；
5. VAD 判定一句说完时，把剩下的全部确认；如果上一次识别已经覆盖到说话结束，就不用再跑一次。

为了省时间，平时不用逐词时间戳（每次要多约 0.4 秒），切缓冲区用 Whisper 本来就会输出的分段时间；
一直说话、长时间没有句号时，缓冲区超过 soft_buffer_s 就让识别顺带输出逐词时间戳，在已确认的词后面切开；
万一还是超过 max_buffer_s，才额外跑一次逐词时间戳强制切开。

另外 Whisper 在音频很短时容易“编下去”（比如把提示词里的上一句接着复述一遍），
所以对识别结果加了语速上限：每秒最多 MAX_WORDS_PER_S 个词，超出的部分直接截掉。
"""

from __future__ import annotations

import re

import numpy as np

from ..audio.base import SAMPLE_RATE
from ..clock import now
from .types import AsrBackend, AsrUpdate, Segment

_NON_WORD = re.compile(r"[^\w'’-]+")
_SENTENCE_END = re.compile(r"[.?!…][\"'»”)]*$")
MIN_UTTERANCE_S = 0.45  # 从开始（含 0.2 秒句首预留）到停止说话不足这么长的，多半是杂音，不识别
HISTORY_WORDS = 200
MAX_WORDS_PER_S = 6.0  # 正常法语语速约每秒 3 个词，快的也很少超过 5 个
TOKENS_PER_S = 10  # 生成 token 的上限按音频长度算：正常语速每秒不到 5 个 token（含标点、时间戳），留足余量
MIN_NEW_SPEECH_S = 0.3  # 上一次识别之后又说了不到这么久（且之前的都已确认），就不再做最后一次识别
_CLAUSE_END = re.compile(r"[,;:.?!…][\"'»”)]*$")


def norm(word: str) -> str:
    """比较用的规范化：小写、去掉标点。纯标点的“词”保持原样。"""
    stripped = _NON_WORD.sub("", word.lower())
    return stripped or word


def align_prefix(committed: list[str], hyp: list[str], window: int = 4) -> int:
    """在新识别结果 hyp 里，找到“已确认部分”结束的位置（返回 hyp 的下标）。

    Whisper 每次重新识别时，已确认的那几个词可能写得稍有不同（大小写、标点、偶尔换个词），
    所以用编辑距离对齐，而不是要求逐字相同。"""
    n = len(committed)
    if n == 0:
        return 0
    c = [norm(w) for w in committed]
    h = [norm(w) for w in hyp[: n + window]]
    # 编辑距离动态规划：row[j] = committed 全部与 hyp 前 j 个词之间的距离
    row = list(range(len(h) + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * len(h)
        for j in range(1, len(h) + 1):
            cur[j] = min(row[j] + 1, cur[j - 1] + 1, row[j - 1] + (c[i - 1] != h[j - 1]))
        row = cur
    lo = min(max(0, n - window), len(h))
    # 取距离最小的位置；一样小时取最接近 n 的
    return min(range(lo, len(h) + 1), key=lambda j: (row[j], abs(j - n)))


def common_prefix(a: list[str], b: list[str]) -> int:
    count = 0
    for x, y in zip(a, b):
        if norm(x) != norm(y):
            break
        count += 1
    return count


def strip_repetition(words: list[str], min_repeats: int = 3) -> list[str]:
    """Whisper 偶尔陷入重复循环（实测：“de la version de la version de la version…”）。
    同一个 2~4 词的短语连续出现 min_repeats 次以上，就只保留第一次；单个词要连续 min_repeats+1 次才算，
    因为 “non non non” 这种可能是真的。"""
    normalized = [norm(w) for w in words]
    for i in range(len(words)):
        for n in range(1, 5):
            gram = normalized[i : i + n]
            if len(gram) < n:
                break
            repeats = 1
            while normalized[i + repeats * n : i + (repeats + 1) * n] == gram:
                repeats += 1
            if repeats >= min_repeats + (1 if n == 1 else 0):
                return words[: i + n]
    return words


def repeated_head(hyp: list[str], history: list[str], max_n: int = 5) -> int:
    """切缓冲区后，新结果的开头可能重复上一句最后几个词（切点比真正的词尾稍早）。返回应跳过的词数。"""
    for n in range(min(max_n, len(hyp), len(history)), 0, -1):
        if [norm(w) for w in hyp[:n]] == [norm(w) for w in history[-n:]]:
            return n
    return 0


def flatten(segments: list[Segment]) -> tuple[list[str], list[int], list[tuple[float, float]] | None]:
    """把分段展开成词列表。ends[s] = 第 s 段最后一个词之后的下标；
    有逐词时间戳时同时返回每个词的 (开始, 结束)，否则第三项为 None。"""
    timed = bool(segments) and all(segment.words for segment in segments)
    words: list[str] = []
    ends: list[int] = []
    times: list[tuple[float, float]] = []
    for segment in segments:
        if timed:
            for start, end, text in segment.words:
                words.append(text)
                times.append((start, end))
        else:
            words.extend(segment.text.split())
        ends.append(len(words))
    return words, ends, times if timed else None


class StreamingTranscriber:
    def __init__(
        self,
        backend: AsrBackend,
        max_buffer_s: float = 12.0,
        soft_buffer_s: float = 6.0,
        prompt_chars: int = 200,
        prompt_min_audio_s: float = 1.5,
        min_audio_s: float = 0.5,
    ) -> None:
        self.backend = backend
        self.max_buffer_s = max_buffer_s
        self.soft_buffer_s = soft_buffer_s
        self.prompt_chars = prompt_chars
        self.prompt_min_audio_s = prompt_min_audio_s
        self.min_audio_s = min_audio_s
        self.history: list[str] = []  # 缓冲区之前已确认的词，用作下一次识别的提示词
        self._reset(0.0)

    # ---- 状态 ----

    def _reset(self, start: float) -> None:
        self.buffer = np.zeros(0, dtype=np.float32)
        self.utterance_start = start  # 这句话（含句首预留）开始的音频流时间
        self.buffer_start = start  # 缓冲区第一个采样点的音频流时间
        self.last_speech_end = start  # VAD 认为“真的有人在说话”的最后一块的结束时间
        self.committed: list[str] = []  # 缓冲区里已确认（已发出去）的词，只追加，切缓冲区时移到 history
        self.tail: list[str] = []  # 上一次识别里还没确认的词
        self.last_pass_end = start  # 上一次识别覆盖到的音频流时间
        self.new_audio_s = 0.0  # 自上一次识别以来新增的音频时长
        self._after_trim = False

    @property
    def buffer_s(self) -> float:
        return len(self.buffer) / SAMPLE_RATE

    @property
    def buffer_end(self) -> float:
        return self.buffer_start + self.buffer_s

    def prompt(self) -> str:
        """上文提示词：帮助 Whisper 保持上下文（专有名词写法、标点风格）。
        但一句话刚开始、音频还很短时，Whisper 容易把提示词里的上一句接着“复述”出来，所以这时先不给。"""
        if self.buffer_s < self.prompt_min_audio_s and not self.committed:
            return ""
        words: list[str] = []
        size = 0
        for word in reversed(self.history):
            size += len(word) + 1
            if size > self.prompt_chars:
                break
            words.append(word)
        return " ".join(reversed(words))

    # ---- 输入 ----

    def start_utterance(self, time_s: float) -> None:
        self._reset(time_s)

    def add_audio(self, samples: np.ndarray, speech: bool = True) -> None:
        """speech：VAD 认为这一块真的有人在说话（句首预留、句中的停顿都是 False）。"""
        self.buffer = np.concatenate([self.buffer, samples])
        self.new_audio_s += len(samples) / SAMPLE_RATE
        if speech:
            self.last_speech_end = self.buffer_end

    @property
    def speech_in_buffer_s(self) -> float:
        """缓冲区（切过之后）里的说话持续到哪儿，相对缓冲区开头。整句确认、在句末切过之后，这个值接近 0。"""
        return self.last_speech_end - self.buffer_start

    # ---- 识别 ----

    def _covered(self, hyp: list[str]) -> int:
        """hyp 里有多少个开头的词已经确认过（已发出去）。"""
        if self._after_trim and not self.committed:
            return repeated_head(hyp, self.history)
        return align_prefix(self.committed, hyp)

    def _recognize(self, word_timestamps: bool = False):
        max_tokens = int(self.buffer_s * TOKENS_PER_S) + 20
        segments = self.backend.transcribe(self.buffer, self.prompt(), word_timestamps, max_tokens)
        hyp, ends, times = flatten(segments)
        limit = int(self.buffer_s * MAX_WORDS_PER_S) + 3  # 语速上限，截掉“编出来”的部分
        return segments, strip_repetition(hyp[:limit]), ends, times

    def process(self) -> AsrUpdate | None:
        """说话期间调用：重新识别整个缓冲区，确认和上一次一致的部分。"""
        if self.buffer_s < self.min_audio_s:
            return None
        if self.speech_in_buffer_s < 0.1:
            # 切过之后缓冲区里还没有新的说话（只有句间停顿）：对着静音识别只会得到幻觉（实测 “chaîne.”）
            return None
        started = now()
        segments, hyp, ends, times = self._recognize(word_timestamps=self.buffer_s > self.soft_buffer_s)
        covered = self._covered(hyp)
        self._after_trim = False
        new = hyp[covered:]
        agreed = common_prefix(new, self.tail)
        newly = new[:agreed]
        confirmed = covered + agreed
        self.committed += newly
        self.tail = new[agreed:]
        self.last_pass_end = self.buffer_end
        self.new_audio_s = 0.0
        if not self._trim_at_segment(segments, hyp, ends, confirmed):
            if times is not None:
                self._trim_at_word(hyp, times, confirmed)
            elif self.buffer_s > self.max_buffer_s:
                newly += self._force_trim()
        return AsrUpdate(
            committed=" ".join(newly),
            partial=" ".join(self.tail),
            final=False,
            audio_end=self.last_pass_end,
            compute_s=now() - started,
            wall=now(),
        )

    def finish(self, speech_end: float) -> AsrUpdate:
        """VAD 判定一句说完：把剩下的全部确认，然后清空缓冲区。"""
        started = now()
        end = self.buffer_end
        words: list[str] = []
        # 两种情况什么都不确认：整句话太短（多半是杂音）；缓冲区（切过之后）里几乎没有真的说话——
        # 最后一句已经整句确认、在句末切过，这时的暂定尾巴只可能是对静音的幻觉（实测 “chaîne.”）
        if speech_end - self.utterance_start >= MIN_UTTERANCE_S and self.speech_in_buffer_s >= MIN_NEW_SPEECH_S:
            unheard = speech_end - max(self.buffer_start, self.last_pass_end)  # 上一次识别之后又说了多久
            if self.last_pass_end >= speech_end + 0.1 and (self.tail or self.committed):
                words = self.tail  # 上一次识别已经覆盖到说话结束，直接确认，省一次识别
            elif unheard < MIN_NEW_SPEECH_S and not self.tail:
                # 上一次听到的内容已经全部确认，之后几乎没再说话：不再识别。
                # 对着剩下的静音识别，Whisper 会“补”出一句（实测 “Merci.”、“chaîne.”），而且这次不经过两次一致的检验
                words = []
            else:
                # 句尾那段静音会诱发 Whisper “补”一句，所以先把缓冲区截到停止说话后 0.1 秒
                keep = int(max(0.0, speech_end + 0.1 - self.buffer_start) * SAMPLE_RATE)
                self.buffer = self.buffer[:keep]
                _, hyp, _, _ = self._recognize()
                words = hyp[self._covered(hyp) :]
        self.history = (self.history + self.committed + words)[-HISTORY_WORDS:]
        self._reset(end)
        return AsrUpdate(
            committed=" ".join(words),
            partial="",
            final=True,
            audio_end=end,
            compute_s=now() - started,
            wall=now(),
        )

    # ---- 切缓冲区 ----

    def quiet_point(self, seconds: float, before: float = 0.25, after: float = 0.25) -> float:
        """在 seconds 前后找声音最小的 20 毫秒，返回它的中点。

        Whisper 给的分段、逐词时间戳常有 0.1~0.3 秒的误差，直接照着切可能切在词中间：
        下一段缓冲区开头只剩半个词，会被识别成别的词（实测 utilisateurs 的尾音被识别成 heures）。
        词与词之间的真实停顿比时间戳可靠，所以切在附近最安静的地方。"""
        frame = int(0.02 * SAMPLE_RATE)
        lo = max(0, int((seconds - before) * SAMPLE_RATE))
        hi = min(len(self.buffer), int((seconds + after) * SAMPLE_RATE))
        count = (hi - lo) // frame
        if count < 2:
            return seconds
        frames = self.buffer[lo : lo + count * frame].reshape(count, frame)
        energy = np.mean(np.square(frames), axis=1)
        centers = (lo + np.arange(count) * frame + frame / 2) / SAMPLE_RATE
        # 按能量从小到大排，一样安静时取离原切点最近的
        quietest = int(np.lexsort((np.abs(centers - seconds), energy))[0])
        return float(centers[quietest])

    def _cut(self, seconds: float, hyp_before_cut: list[str]) -> None:
        """把缓冲区前 seconds 秒（会微调到附近最安静的地方）切掉，对应的已确认词移到 history。"""
        seconds = self.quiet_point(seconds)
        if not 0 < seconds < self.buffer_s:
            return
        moved = align_prefix(hyp_before_cut, self.committed)
        self.history = (self.history + self.committed[:moved])[-HISTORY_WORDS:]
        self.committed = self.committed[moved:]
        self.buffer = self.buffer[int(seconds * SAMPLE_RATE) :]
        self.buffer_start += seconds
        self._after_trim = True

    def _trim_at_segment(self, segments: list[Segment], hyp: list[str], ends: list[int], confirmed: int) -> bool:
        """优先切在“整段已确认、以句末标点结尾”的分段之后；缓冲区太长时，任何整段已确认的分段都可以切。"""
        too_long = self.buffer_s > self.max_buffer_s
        cut = None
        for index, segment in enumerate(segments):
            fully_confirmed = 0 < ends[index] <= confirmed
            if fully_confirmed and (too_long or _SENTENCE_END.search(segment.text)):
                cut = index
        if cut is None:
            return False
        seconds = segments[cut].end
        if not 0 < seconds < self.buffer_s:
            return False
        self._cut(seconds, hyp[: ends[cut]])
        return True

    def _trim_at_word(self, hyp: list[str], times: list[tuple[float, float]], confirmed: int) -> bool:
        """有逐词时间戳时：切在已确认部分里最后一个逗号/句号之后；没有标点就切在最后一个已确认的词之后。"""
        confirmed = min(confirmed, len(hyp), len(times))
        if confirmed == 0:
            return False
        index = next((i for i in range(confirmed - 1, -1, -1) if _CLAUSE_END.search(hyp[i])), confirmed - 1)
        end = times[index][1]
        following = times[index + 1][0] if index + 1 < len(times) else end
        seconds = (end + max(end, following)) / 2  # 切在两个词之间的空隙中点
        if not 1.0 <= seconds < self.buffer_s:
            return False
        self._cut(seconds, hyp[: index + 1])
        return True

    def _force_trim(self) -> list[str]:
        """一直在说话、长时间没有句号，缓冲区超长又没有可切的分段时：
        用一次逐词时间戳，在倒数 2 秒处强制切开，切点之前的词直接确认。"""
        max_tokens = int(self.buffer_s * TOKENS_PER_S) + 20
        segments = self.backend.transcribe(self.buffer, self.prompt(), True, max_tokens)
        timed = [w for segment in segments for w in segment.words]
        keep_from = self.buffer_s - 2.0
        before = [i for i, (_, end, _) in enumerate(timed) if end <= keep_from]
        if not before:
            return []
        count = before[-1] + 1
        texts = [text for _, _, text in timed]
        covered = align_prefix(self.committed, texts)
        forced = texts[covered:count] if covered < count else []
        self.committed += forced
        self._cut(timed[count - 1][1], texts[:count])
        self.tail = texts[max(covered, count) :]
        return forced

"""流式翻译：把确认下来的原文按句（或较长的分句）送给大模型，译文一个字一个字地流回来。

- ChatTranslator：调用任何兼容 OpenAI Chat Completions 的接口，直接用 httpx 解析 SSE 流，不依赖 openai SDK；
- UnitBuilder：纯逻辑，决定什么时候把攒下的原文送去翻译；
- TranslationStage：把两者串起来，用小线程池并发翻译，一段慢了不会堵住后面的。
  说话人停下来想词时留下的半句话，也由它负责“先等一等”，等接上后面的话再翻译。
  正在说的这一句还没说完时，它每隔几个词就把已经听到的部分译一版“草稿”显示出来（边说边译），
  所以屏幕上的中文紧跟着法语走，不用等整句说完；整句定下来之后再换成正式译文。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx

from .asr.streaming import norm
from .clock import now
from .config import TranslateConfig

logger = logging.getLogger(__name__)

_STRONG_END = re.compile(r"[.?!][\"'»”)]*$")
_ELLIPSIS = re.compile(r"(?:\.{2,}|…)[\"'»”)]*$")
_SOFT_END = re.compile(r"[,;:][\"'»”)]*$")
_INITIAL = re.compile(r"^[A-ZÀ-ÖØ-Þ]\.$")
# 等半句话的时候，离到点还有这么久才预先送去翻译：翻译服务出第一个字约要 0.7 秒（DEVLOG S11b），
# 这样真到点时译文正好现成；说话人在这之前接着说了（多数情况），这次请求就省下了
HOLD_SPECULATE_LEAD_S = 0.8
# 等第一个字最多等多久。翻译服务繁忙时不会报错，而是让请求一直挂着（连接上不时发来一行空的“保持连接”），
# 能挂好几分钟。草稿过了几秒就没用了，而且同一时刻只请求一版，卡住一版后面的就都出不来，所以期限要短
FIRST_TOKEN_TIMEOUT_S = 12.0
DRAFT_FIRST_TOKEN_TIMEOUT_S = 4.0


def ends_sentence(word: str) -> bool:
    """这个词是不是一句话的结尾：以 . ? ! 结尾。
    省略号不算：Whisper 用它表示说话人拖着音、话还没说完；“M.” 这样的单字母缩写也不算。"""
    if _ELLIPSIS.search(word) or _INITIAL.match(word):
        return False
    return bool(_STRONG_END.search(word))


def ends_clause(word: str) -> bool:
    """逗号、分号、冒号：长句可以在这里切开，但话还没说完。"""
    return bool(_SOFT_END.search(word))


def trails_off(word: str) -> bool:
    """以省略号结尾：说话人拖着音、在想词。后面多半跟着一次停顿，所以轻易不在这里切。"""
    return bool(_ELLIPSIS.search(word))


class TranslationError(RuntimeError):
    pass


class ChatTranslator:
    def __init__(
        self,
        cfg: TranslateConfig,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        source_language: str = "法语",
    ) -> None:
        """source_language：原文是什么语言（中文名，写进提示词里）；运行中换语言时直接改这个属性。"""
        self.cfg = cfg
        self.source_language = source_language
        self._client = httpx.Client(
            base_url=cfg.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(10.0, read=30.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def warm_up(self) -> None:
        """启动时先建立 TLS 连接（之后复用），顺便检查密钥，避免第一句话多花一次握手的时间。"""
        started = now()
        try:
            response = self._client.get("/models")
        except httpx.HTTPError as exc:
            logger.warning("连接翻译服务失败：%s", exc)
            return
        if response.status_code in (401, 403):
            raise TranslationError(f"翻译 API 密钥无效（HTTP {response.status_code}），请检查 .env")
        logger.info("已连接翻译服务 %s（%.0f ms）", self.cfg.base_url, (now() - started) * 1000)

    def system_prompt(self) -> str:
        lines = [
            f"你是专业的同声传译员，把{self.source_language}实时翻译成{self.cfg.target_language}。",
            "要求：",
            "- 只输出译文，不要解释，不要加引号，不要重复原文。",
            "- 原文来自实时语音识别，可能是半句话、有识别错误或标点不准：按最合理的意思翻译，不要补充原文没有的内容。",
            "- 口语化、简洁，符合中文表达习惯；数字和专有名词要准确。",
        ]
        if self.cfg.glossary:
            lines.append("术语表（必须按此翻译）：")
            lines += [f"- {src} → {dst}" for src, dst in self.cfg.glossary.items()]
        return "\n".join(lines)

    def messages(self, source: str, context: list[str], draft: bool = False, previous: str = "") -> list[dict]:
        """draft：source 是一句还没说完的话的开头，要的是“到目前为止”的译文草稿；
        previous：上一版草稿。让模型尽量沿用它，屏幕上已经显示的字才不会每次都换个说法（DEVLOG S21）。"""
        # 系统提示词保持不变，上文放在用户消息里：固定的开头部分能被服务商缓存，更快也更便宜
        if draft:
            # 草稿不带上文：带上的话，模型常把上文也当成“这句话”的一部分译进来，有时甚至只译了上文（S21 实测）。
            # 草稿只显示一两秒就会被下一版或正式译文换掉，用不着上文；正式译文照常带
            user = "「」里是一句还没说完的话的开头，说话人还在往下说。只翻译「」里的内容：\n"
            user += f"「{source}」\n要求：\n"
            if previous:
                user += f"- 上一版译文是「{previous}」。前面已经译对的部分照抄，读的人才能接着往下看；只有译错或者读不通的地方才改；\n"
            user += "- 译文要从「」里的第一个词译到最后一个词，不要只输出新增的部分；\n"
            user += "- 不要补全还没说出来的内容，结尾不要加句号；末尾还悬着、暂时译不出的词先不译，不要用省略号占位。"
        elif context:
            user = "上文（仅供参考，不要翻译）：\n" + "\n".join(context) + "\n\n需要翻译：\n" + source
        else:
            user = source
        return [{"role": "system", "content": self.system_prompt()}, {"role": "user", "content": user}]

    def stream(
        self, source: str, context: list[str] | None = None, draft: bool = False, previous: str = ""
    ) -> Iterator[str]:
        body = {
            "model": self.cfg.model,
            "messages": self.messages(source, context or [], draft, previous),
            "stream": True,
            "temperature": self.cfg.temperature,
            "max_tokens": 400,
            **self.cfg.extra_body,
        }
        limit = DRAFT_FIRST_TOKEN_TIMEOUT_S if draft else FIRST_TOKEN_TIMEOUT_S
        timeout = httpx.Timeout(10.0, read=limit)
        started, waiting = now(), True
        with self._client.stream("POST", "/chat/completions", json=body, timeout=timeout) as response:
            if response.status_code != 200:
                response.read()
                raise TranslationError(f"翻译 API 返回 HTTP {response.status_code}：{response.text[:300]}")
            for line in response.iter_lines():
                if waiting and now() - started > limit:  # 连接没断，但一直只有“保持连接”的空行
                    raise TranslationError(f"翻译服务 {limit:.0f} 秒没有开始返回译文")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                choices = json.loads(data).get("choices") or []
                delta = (choices[0].get("delta") or {}).get("content") if choices else None
                if delta:
                    waiting = False
                    yield delta


class MockTranslator:
    """模拟翻译：不联网，逐字“流式”返回带标记的原文。用来在没有密钥时测试整条流程和界面。"""

    def __init__(self, delay_s: float = 0.02) -> None:
        self.delay_s = delay_s

    def warm_up(self) -> None:
        pass

    def stream(
        self, source: str, context: list[str] | None = None, draft: bool = False, previous: str = ""
    ) -> Iterator[str]:
        time.sleep(0.3)  # 模拟网络和首字延迟
        text = f"〔模拟译文〕{source}"
        if draft:  # 草稿很短，真实的接口几乎是一下子全出来的
            yield text
            return
        for char in text:
            time.sleep(self.delay_s)
            yield char

    def close(self) -> None:
        pass


class UnitBuilder:
    """决定什么时候把攒下的原文送去翻译：
    - 遇到句末标点（. ? !）就送；
    - 句子很长时，遇到逗号且已经攒够 clause_min_words 个词，先送前半句，降低长句的延迟；
    - 省略号（说话人拖着音想词）要攒到 trail_min_words 个词才在那里切：短的半句留着，等他接着说；
    - 攒到 max_words 个词还没切，必须切了：尽量切在最后一个逗号或省略号之后，实在没有才硬切；
    - 一段话说完（语音检测判定）时剩下的半句：hold_incomplete 为假就直接送；为真就留着，
      等说话人接着说，或者由调用方等够时间后用 flush() 取走。"""

    def __init__(
        self, clause_min_words: int = 8, max_words: int = 25, hold_incomplete: bool = False, trail_min_words: int = 14
    ) -> None:
        self.clause_min_words = clause_min_words
        self.trail_min_words = trail_min_words
        self.max_words = max_words
        self.hold_incomplete = hold_incomplete
        self.pending: list[str] = []
        self.pending_times: list[float | None] = []  # 和 pending 一一对应：每个词在音频流里的时间（秒）

    def cut(self, words: list[str]) -> int:
        """words 是从头攒到现在的词，最后一个是刚加进来的。返回应该在第几个词之后切开，0 = 还不切。"""
        count, last = len(words), words[-1]
        if ends_sentence(last):
            return count
        if (ends_clause(last) and count >= self.clause_min_words) or (trails_off(last) and count >= self.trail_min_words):
            return count
        if count >= self.max_words:
            for size in range(count - 1, self.clause_min_words - 1, -1):
                if ends_clause(words[size - 1]) or trails_off(words[size - 1]):
                    return size
            return count
        return 0

    def add(self, committed: str, final: bool) -> list[str]:
        return [source for source, _ in self.add_timed(committed, final)]

    def add_timed(
        self, committed: str, final: bool, times: list[float] | None = None
    ) -> list[tuple[str, float | None]]:
        """和 add 一样，另外带上每个词在音频流里的时间（times 和 committed 里的词一一对应）。
        返回 (翻译单元, 它第一个词的时间)；没给时间或者数量对不上时，时间为 None。"""
        words = committed.split()
        if times is None or len(times) != len(words):
            times = [None] * len(words)
        units = []
        for word, time_s in zip(words, times):
            self.pending.append(word)
            self.pending_times.append(time_s)
            size = self.cut(self.pending)
            if size:
                units.append(self._take(size))
        if final and self.pending and not self.hold_incomplete:
            units.append(self._take(len(self.pending)))
        return units

    def flush(self) -> tuple[str, float | None] | None:
        """把留着的半句当作一个翻译单元取走；没有就返回 None。"""
        return self._take(len(self.pending)) if self.pending else None

    def _take(self, size: int) -> tuple[str, float | None]:
        """取走最前面的 size 个词，剩下的接着攒。"""
        unit = (" ".join(self.pending[:size]), self.pending_times[0])
        self.pending, self.pending_times = self.pending[size:], self.pending_times[size:]
        return unit

    def first_unit(self, words: list[str]) -> int:
        """把 words 当作从头开始攒的词，返回第一个翻译单元有几个词；凑不成返回 0。
        投机翻译用它来切单元，保证和正式确认后的切法完全一样。"""
        for count in range(1, len(words) + 1):
            size = self.cut(words[:count])
            if size:
                return size
        return 0


def clean_draft(text: str) -> str:
    """译文草稿：话还没说完，结尾不该有句号（提示词里要求了，模型偶尔还是会加）。"""
    return text.strip().rstrip("。.")


def unit_key(text: str) -> tuple[str, ...]:
    """比较两段原文是否“同一句”：忽略大小写和标点。"""
    return tuple(norm(word) for word in text.split())


@dataclass
class TranslationUnit:
    id: int
    source: str
    ready_at: float  # 原文确认、送去翻译的时刻（clock.now）
    translation: str = ""
    first_token_at: float | None = None  # 投机翻译命中时可能早于 ready_at：原文还没确认，译文就已经开始出来了
    done_at: float | None = None
    error: str = ""
    speculative: bool = False  # 译文是否来自投机翻译
    audio_start: float | None = None  # 这段原文在音频流（也就是录音）里大约从第几秒开始；不知道时为 None
    placeholder: str = ""  # 这句话切出来之前屏幕上显示着的译文草稿；正式译文出来之前先接着显示它

    @property
    def done(self) -> bool:
        return self.done_at is not None

    @property
    def tentative(self) -> bool:
        """屏幕上显示的还是草稿：正式译文还没追上草稿的长度。"""
        return not self.done and len(self.translation) < len(self.placeholder)

    @property
    def shown(self) -> str:
        """屏幕上该显示的译文。正式译文追上草稿的长度之前先显示草稿：
        否则这句话一切出来，下面的中文就会先消失、过一会儿再从头出现。"""
        return self.placeholder if self.tentative else self.translation


@dataclass
class SpeculativeJob:
    """还没完全确认的一句，先送去翻译。确认后文字没变，就直接用这份译文，不再请求。"""

    source: str
    key: tuple[str, ...]
    context: list[str]
    translation: str = ""
    first_token_at: float | None = None
    done_at: float | None = None
    error: str = ""
    cancelled: bool = False
    unit: TranslationUnit | None = None  # 被哪个正式单元采用了


@dataclass
class DraftJob:
    """正在说的这句话还没说完：先把已经听到的部分译一版，显示在“正在说”那一行下面。"""

    source: str
    key: tuple[str, ...]
    context: list[str]
    previous: str  # 上一版草稿，让模型尽量沿用
    seq: int  # 第几版，显示时新的压过旧的
    translation: str = ""
    cancelled: bool = False


class TranslationStage:
    def __init__(
        self,
        translator: ChatTranslator | MockTranslator | None,
        on_update: Callable[[TranslationUnit], None],
        context_sentences: int = 3,
        max_workers: int = 6,
        speculative: bool = False,
        on_speculation: Callable[[str], None] | None = None,
        max_speculative_words: int = 12,
        incomplete_wait_s: float = 0.0,
        on_flush: Callable[[], None] | None = None,
        live_draft: bool = False,
        draft_min_words: int = 3,
        draft_step_words: int = 2,
    ) -> None:
        """speculative：开启投机翻译；on_speculation(译文) 在“正在说”那一行的译文更新时调用（投机译文或草稿）。
        live_draft：边说边译。正在说的这句话还没说完，就每隔几个词把已经听到的部分译一版草稿显示出来。
        draft_min_words：这句话至少有这么多个词才出第一版草稿；draft_step_words：比上一版多出这么多个词才出下一版。
        max_speculative_words：凑成单元还需要超过这么多个暂定词时不投机（暂定部分太长，多半不可靠）。
        incomplete_wait_s：一段话说完时剩下的半句（结尾不是句号、问号）先不送去翻译，等说话人接着说；
        安静了这么多秒还没下文才送。0 = 不等，说完就送。
        on_flush()：这样等到时间、把半句送出去之后调用（在定时器线程里），界面上“正在说”的那一行靠它清掉。"""
        self.translator = translator
        self.on_update = on_update
        self.builder = UnitBuilder(hold_incomplete=incomplete_wait_s > 0)
        self.speculative = speculative and translator is not None
        self.on_speculation = on_speculation or (lambda text: None)
        self.max_speculative_words = max_speculative_words
        self.incomplete_wait_s = incomplete_wait_s
        self.on_flush = on_flush or (lambda: None)
        self.live_draft = live_draft and translator is not None
        self.draft_min_words = draft_min_words
        self.draft_step_words = draft_step_words
        self.stats = {"speculated": 0, "adopted": 0, "drafted": 0}
        self._context: deque[str] = deque(maxlen=context_sentences)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="translate")
        self._next_id = 0
        self._spec: SpeculativeJob | None = None
        self._lock = threading.Lock()  # 攒词（builder）、编号、投机任务、等半句话的计时，都在这把锁里改
        self._utterances = 0  # 已经开口、还没处理完的话有几段；> 0 表示说话人正在说
        self._hold_timer: threading.Timer | None = None
        self._hold_token = 0  # 每次开始等、取消等都加 1，让已经在路上的定时器作废
        # “正在说”那一行下面显示的译文（投机译文或草稿）。几个任务可能同时在跑，按发起的先后编号，新的压过旧的
        self._seq = 0
        self._live_text = ""
        self._live_seq = 0  # 现在显示的这份译文是哪个任务出的
        self.live_covered = 0  # 现在显示的这份译文，是照着当前句的前几个词译的（测延迟用）
        self._live_words: list[str] = []  # 当前句目前听到的词（已确认的 + 暂定的）
        self._live_settled = False  # 这段话已经说完了（留着的半句）：有一个词没译进草稿也要再出一版
        self._draft: DraftJob | None = None  # 正在请求的草稿；同一时刻只请求一版，回来了再决定要不要下一版
        self._drafted: tuple[str, ...] = ()  # 上一版草稿是照着哪些词译的
        self._draft_text = ""  # 上一版草稿的译文

    @property
    def pending(self) -> str:
        """已经确认、但还没凑够送去翻译的原文。"""
        with self._lock:
            return " ".join(self.builder.pending)

    @property
    def next_id(self) -> int:
        """下一个翻译单元会用的编号。"""
        with self._lock:
            return self._next_id

    def reset_context(self) -> None:
        """丢掉翻译时附带的上文（换语言之后，之前的句子是另一种语言的，不能再当上文）。"""
        with self._lock:
            self._context.clear()

    @property
    def live_translation(self) -> str:
        """“正在说”那一行下面现在该显示的译文（投机译文或草稿），没有则为空。"""
        with self._lock:
            return self._live_text

    def feed(self, committed: str, final: bool, word_times: list[float] | None = None) -> list[TranslationUnit]:
        """把新确认的原文交给翻译阶段，返回因此新建的翻译单元。
        word_times：committed 里每个词在音频流里的时间（秒），用来给会话记录标上录音里的位置。"""
        with self._lock:
            if committed.strip():
                self._cancel_hold()  # 有下文了：等着的半句接着往下攒
            pieces = self.builder.add_timed(committed, final, word_times)
            if final:
                self._utterances = max(0, self._utterances - 1)
        units = [self._emit(source, start) for source, start in pieces]
        if final:
            self._hold()
            if self.live_draft:  # 留下的半句：这段话已经说完，不会再有新词来凑数了，草稿要译到最后一个词
                self._want_draft(self.pending.split(), settled=True)
        return units

    def speech_started(self) -> None:
        """语音检测发现又有人开口了（在它自己的线程里调用）：等着的半句先不送，接着往下攒。"""
        with self._lock:
            self._utterances += 1
            self._cancel_hold()

    def _emit(self, source: str, start: float | None) -> TranslationUnit:
        """新建一个翻译单元并送去翻译（投机翻译过同一句的话，直接用那份译文）。"""
        with self._lock:
            unit = TranslationUnit(self._next_id, source, now(), audio_start=start)
            self._next_id += 1
            context = list(self._context)
            self._context.append(source)
            job = self._adopt_speculation(unit)
            unit.placeholder = self._live_text  # 屏幕上正显示着的草稿：正式译文出来之前接着显示
            self._reset_live()
        if self.translator is None:  # 不翻译（没有密钥）：只有原文，直接算完成
            unit.done_at = unit.ready_at
            self.on_update(unit)
            return unit
        self.on_update(unit)
        if job is None:
            try:
                self._pool.submit(self._translate, unit, context)
            except RuntimeError:  # 正在退出，线程池已经关了
                unit.error, unit.done_at = "程序正在退出，没有翻译", now()
                self.on_update(unit)
        return unit

    # ---- 没说完的半句：先等一等 ----

    def _hold(self) -> None:
        """一段话说完了。如果还留着没说完的半句，开始计时：到点还没有下文，才把它送去翻译。"""
        with self._lock:
            self._cancel_hold()
            if not self.builder.pending or self._utterances > 0:
                return  # 没有留下半句，或者说话人已经又开口了
            lead = min(HOLD_SPECULATE_LEAD_S, self.incomplete_wait_s) if self.speculative else 0.0
            self._arm(self.incomplete_wait_s - lead, self._hold_token, lead)

    def _arm(self, delay: float, token: int, remaining: float) -> None:
        """（持锁调用）"""
        self._hold_timer = threading.Timer(max(0.0, delay), self._on_hold_timer, (token, remaining))
        self._hold_timer.daemon = True
        self._hold_timer.start()

    def _cancel_hold(self) -> None:
        """（持锁调用）"""
        self._hold_token += 1
        if self._hold_timer is not None:
            self._hold_timer.cancel()
            self._hold_timer = None

    def _on_hold_timer(self, token: int, remaining: float) -> None:
        """定时器线程。remaining > 0 表示这是“快到点了”的那一下：先投机翻译，再等完剩下的时间。"""
        piece = source = None
        with self._lock:
            if token != self._hold_token or self._utterances > 0 or not self.builder.pending:
                return  # 这次等待已经被取消（有下文了），或者说话人又开口了
            if remaining > 0:
                source = " ".join(self.builder.pending)
                self._arm(remaining, token, 0.0)
            else:
                piece = self.builder.flush()
                self._hold_token += 1
                self._hold_timer = None
        if source is not None:
            self._start_speculation(source)
        else:
            self._emit(*piece)
            self.on_flush()

    # ---- 投机翻译 ----

    def speculate(self, partial: str) -> None:
        """每次识别结果更新时调用，看“已确认但还没送出的词 + 暂定尾巴”：
        - 投机翻译：里面已经能凑成一个翻译单元（看上去说完了一句）就先送去翻译。译文不显示，
          留着等原文确认时直接用——暂定文字末尾的句号常常是假的，这时候的“整句”多半还会接着长；
        - 边说边译：不管这句话说没说完，都把目前听到的部分译一版草稿显示出来。"""
        if not (self.speculative or self.live_draft):
            return
        with self._lock:
            pending = list(self.builder.pending)
        words = pending + partial.split()
        size = self.builder.first_unit(words)
        if self.speculative and size > len(pending) and size - len(pending) <= self.max_speculative_words:
            self._start_speculation(" ".join(words[:size]))
        if self.live_draft:
            # 暂定文字最末尾那个词最不可靠（实测 28% 之后会变，往前的约 15%）：草稿先不译它，等下一次识别
            steady = words[:-1] if len(words) > len(pending) else words
            size = self.builder.first_unit(steady)
            self._want_draft(steady[:size] if size else steady)

    def _start_speculation(self, source: str) -> None:
        key = unit_key(source)
        with self._lock:
            if self._spec is not None and self._spec.key == key:
                return  # 这句已经在投机翻译了
            if self._spec is not None:
                self._spec.cancelled = True  # 暂定文字变了，之前那份投机作废
            job = SpeculativeJob(source, key, list(self._context))
            self._spec = job
            self.stats["speculated"] += 1
        try:
            self._pool.submit(self._run_speculation, job)
        except RuntimeError:  # 正在退出，线程池已经关了
            job.cancelled = True

    def _adopt_speculation(self, unit: TranslationUnit) -> SpeculativeJob | None:
        """（持锁调用）正式单元确认时：和投机的是同一句就采用它的译文，否则让投机作废。"""
        job, self._spec = self._spec, None
        if job is None:
            return None
        if job.cancelled or job.key != unit_key(unit.source):
            job.cancelled = True
            return None
        job.unit = unit
        unit.speculative = True
        unit.translation, unit.first_token_at, unit.error = job.translation, job.first_token_at, job.error
        if job.done_at is not None:
            unit.translation = unit.translation.strip()
            unit.done_at = unit.ready_at  # 原文确认时译文已经翻完了
        self.stats["adopted"] += 1
        return job

    def _run_speculation(self, job: SpeculativeJob) -> None:
        stream = self.translator.stream(job.source, job.context)
        try:
            for delta in stream:
                if job.cancelled:
                    return  # 退出前 finally 会关掉生成器，也就关掉了 HTTP 流
                with self._lock:
                    job.translation += delta
                    if job.first_token_at is None:
                        job.first_token_at = now()
                    unit = job.unit
                    if unit is not None:
                        unit.translation = job.translation
                        unit.first_token_at = unit.first_token_at or job.first_token_at
                if unit is not None:
                    self.on_update(unit)
        except Exception as exc:
            job.error = str(exc)
            logger.warning("投机翻译失败：%s", exc)
        finally:
            stream.close()
        with self._lock:
            job.done_at = now()
            unit = job.unit
            if unit is not None and not unit.done:
                unit.translation = job.translation.strip()
                unit.error = job.error
                unit.done_at = job.done_at
        if unit is not None:
            self.on_update(unit)

    # ---- 边说边译：当前句的译文草稿 ----

    def _offer_live(self, seq: int, text: str, covered: int, complete: bool) -> str | None:
        """（持锁调用）某个任务出了新的译文，决定屏幕上要不要换。要换就返回新的文字，否则返回 None。
        - 更早发起的任务不能盖掉更晚的；
        - 新任务的译文是一个字一个字来的：追上屏幕上那份的长度之前先不换，否则这一行会先缩短再变长，看着是在闪。"""
        if seq < self._live_seq or (seq == self._live_seq and text == self._live_text):
            return None
        if seq > self._live_seq and not complete and len(text) < len(self._live_text):
            return None
        self._live_text, self._live_seq, self.live_covered = text, seq, covered
        return text

    def _reset_live(self) -> None:
        """（持锁调用）当前句切成单元送走了：它下面的译文和草稿都不再属于“正在说”的那一句。"""
        if self._draft is not None:
            self._draft.cancelled = True
            self._draft = None
        self._seq += 1  # 已经发起的任务编号都比它小，再回来也不会显示
        self._live_text, self._live_seq, self.live_covered = "", self._seq, 0
        self._live_words, self._live_settled, self._drafted, self._draft_text = [], False, (), ""

    def _want_draft(self, words: list[str], settled: bool = False) -> None:
        with self._lock:
            self._live_words, self._live_settled = words, settled
            job = self._next_draft()
        if job is not None:
            self._submit_draft(job)

    def _next_draft(self) -> DraftJob | None:
        """（持锁调用）要不要出下一版草稿。同一时刻只请求一版：请求一次约 1 秒，这期间又多出来的词，
        等它回来再一起译；这样请求的节奏自然跟着翻译服务的速度走，不会越积越多。"""
        words = self._live_words
        if self._draft is not None or len(words) < (1 if self._live_settled else self.draft_min_words):
            return None
        key = unit_key(" ".join(words))
        same = 0
        for new, old in zip(key, self._drafted):
            if new != old:
                break
            same += 1
        if len(key) - same < (1 if self._live_settled else self.draft_step_words):
            return None  # 比上一版多出来（或者改了）的词还不够多
        self._seq += 1
        # 暂定文字末尾的句号、省略号多半是识别模型顺手加的，不代表话说完了：不给翻译模型看，免得它也跟着收尾
        source = " ".join(words).rstrip(".… ")
        job = DraftJob(source, key, list(self._context), self._draft_text if same else "", self._seq)
        self._draft = job
        self.stats["drafted"] += 1
        return job

    def _submit_draft(self, job: DraftJob) -> None:
        try:
            self._pool.submit(self._run_draft, job)
        except RuntimeError:  # 正在退出，线程池已经关了
            job.cancelled = True

    def _run_draft(self, job: DraftJob) -> None:
        failed = False
        stream = self.translator.stream(job.source, job.context, draft=True, previous=job.previous)
        try:
            for delta in stream:
                if job.cancelled:
                    return  # 这句话已经切走了；退出前 finally 会关掉 HTTP 流
                job.translation += delta
                with self._lock:
                    shown = None
                    if not job.cancelled:
                        shown = self._offer_live(job.seq, clean_draft(job.translation), len(job.key), False)
                if shown is not None:
                    self.on_speculation(shown)
        except Exception as exc:  # 草稿失败不要紧：这句话说完后还有正式译文
            failed = True
            logger.info("这一版译文草稿没有成功：%s", exc)
        finally:
            stream.close()
        with self._lock:
            if job.cancelled:
                return
            self._draft = None
            self._drafted = job.key  # 失败了也记下：同样的词不再重试，等有了新词再出下一版
            shown = None
            if not failed:
                self._draft_text = clean_draft(job.translation)
                shown = self._offer_live(job.seq, self._draft_text, len(job.key), True)
            following = self._next_draft()
        if shown is not None:
            self.on_speculation(shown)
        if following is not None:
            self._submit_draft(following)

    def _translate(self, unit: TranslationUnit, context: list[str]) -> None:
        for attempt in (1, 2):
            try:
                for delta in self.translator.stream(unit.source, context):
                    if unit.first_token_at is None:
                        unit.first_token_at = now()
                    unit.translation += delta
                    self.on_update(unit)
                break
            except Exception as exc:  # 翻译失败只影响这一句，不能让整个程序停下
                if attempt == 1 and not unit.translation:  # 一个字都没出来（比如服务繁忙、迟迟不回）：再试一次
                    logger.warning("翻译没有成功，重试一次：%s", exc)
                    continue
                unit.error = str(exc)
                logger.error("翻译失败：%s", exc)
                break
        unit.translation = unit.translation.strip()
        unit.done_at = now()
        self.on_update(unit)

    def close(self) -> None:
        with self._lock:
            self._cancel_hold()
            piece = self.builder.flush()  # 还留着的半句不能丢：退出前送去翻译
        if piece is not None:
            self._emit(*piece)
        with self._lock:
            if self._spec is not None:
                self._spec.cancelled = True
            self._reset_live()
        self._pool.shutdown(wait=True)
        if self.translator is not None:
            self.translator.close()

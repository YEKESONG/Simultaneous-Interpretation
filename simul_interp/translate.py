"""流式翻译：把确认下来的法语按句（或较长的分句）送给大模型，译文一个字一个字地流回来。

- ChatTranslator：调用任何兼容 OpenAI Chat Completions 的接口，直接用 httpx 解析 SSE 流，不依赖 openai SDK；
- UnitBuilder：纯逻辑，决定什么时候把攒下的法语送去翻译；
- TranslationStage：把两者串起来，用小线程池并发翻译，一段慢了不会堵住后面的。
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

_SENTENCE_END = re.compile(r"[.?!…][\"'»”)]*$")
_CLAUSE_END = re.compile(r"[,;:][\"'»”)]*$")


class TranslationError(RuntimeError):
    pass


class ChatTranslator:
    def __init__(self, cfg: TranslateConfig, api_key: str, transport: httpx.BaseTransport | None = None) -> None:
        self.cfg = cfg
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
            f"你是专业的同声传译员，把法语实时翻译成{self.cfg.target_language}。",
            "要求：",
            "- 只输出译文，不要解释，不要加引号，不要重复原文。",
            "- 原文来自实时语音识别，可能是半句话、有识别错误或标点不准：按最合理的意思翻译，不要补充原文没有的内容。",
            "- 口语化、简洁，符合中文表达习惯；数字和专有名词要准确。",
        ]
        if self.cfg.glossary:
            lines.append("术语表（必须按此翻译）：")
            lines += [f"- {src} → {dst}" for src, dst in self.cfg.glossary.items()]
        return "\n".join(lines)

    def messages(self, source: str, context: list[str]) -> list[dict]:
        # 系统提示词保持不变，上文放在用户消息里：固定的开头部分能被服务商缓存，更快也更便宜
        user = source
        if context:
            user = "上文（仅供参考，不要翻译）：\n" + "\n".join(context) + "\n\n需要翻译：\n" + source
        return [{"role": "system", "content": self.system_prompt()}, {"role": "user", "content": user}]

    def stream(self, source: str, context: list[str] | None = None) -> Iterator[str]:
        body = {
            "model": self.cfg.model,
            "messages": self.messages(source, context or []),
            "stream": True,
            "temperature": self.cfg.temperature,
            "max_tokens": 400,
            **self.cfg.extra_body,
        }
        with self._client.stream("POST", "/chat/completions", json=body) as response:
            if response.status_code != 200:
                response.read()
                raise TranslationError(f"翻译 API 返回 HTTP {response.status_code}：{response.text[:300]}")
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                choices = json.loads(data).get("choices") or []
                delta = (choices[0].get("delta") or {}).get("content") if choices else None
                if delta:
                    yield delta


class MockTranslator:
    """模拟翻译：不联网，逐字“流式”返回带标记的原文。用来在没有密钥时测试整条流程和界面。"""

    def __init__(self, delay_s: float = 0.02) -> None:
        self.delay_s = delay_s

    def warm_up(self) -> None:
        pass

    def stream(self, source: str, context: list[str] | None = None) -> Iterator[str]:
        time.sleep(0.3)  # 模拟网络和首字延迟
        for char in f"〔模拟译文〕{source}":
            time.sleep(self.delay_s)
            yield char

    def close(self) -> None:
        pass


class UnitBuilder:
    """决定什么时候把攒下的法语送去翻译：
    - 遇到句末标点（. ? !）就送；
    - 句子很长时，遇到逗号且已经攒够 clause_min_words 个词，先送前半句，降低长句的延迟；
    - 攒到 max_words 个词还没有标点，也送；
    - 一句话说完（VAD 判定），剩下的全部送。"""

    def __init__(self, clause_min_words: int = 8, max_words: int = 25) -> None:
        self.clause_min_words = clause_min_words
        self.max_words = max_words
        self.pending: list[str] = []
        self.pending_times: list[float | None] = []  # 和 pending 一一对应：每个词在音频流里的时间（秒）

    def is_boundary(self, word: str, count: int) -> bool:
        """攒到第 count 个词（就是 word）时，是否凑成了一个翻译单元。"""
        return bool(
            _SENTENCE_END.search(word)
            or (_CLAUSE_END.search(word) and count >= self.clause_min_words)
            or count >= self.max_words
        )

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
            if self.is_boundary(word, len(self.pending)):
                units.append(self._take())
        if final and self.pending:
            units.append(self._take())
        return units

    def _take(self) -> tuple[str, float | None]:
        unit = (" ".join(self.pending), self.pending_times[0])
        self.pending, self.pending_times = [], []
        return unit

    def first_unit(self, words: list[str]) -> int:
        """把 words 当作从头开始攒的词，返回第一个翻译单元有几个词；凑不成返回 0。
        投机翻译用它来切单元，保证和正式确认后的切法完全一样。"""
        for index, word in enumerate(words):
            if self.is_boundary(word, index + 1):
                return index + 1
        return 0


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

    @property
    def done(self) -> bool:
        return self.done_at is not None


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


class TranslationStage:
    def __init__(
        self,
        translator: ChatTranslator | MockTranslator | None,
        on_update: Callable[[TranslationUnit], None],
        context_sentences: int = 3,
        max_workers: int = 4,
        speculative: bool = False,
        on_speculation: Callable[[str], None] | None = None,
        max_speculative_words: int = 12,
    ) -> None:
        """speculative：开启投机翻译；on_speculation(译文) 在投机译文更新时调用，用来显示在“正在说”那一行。
        max_speculative_words：凑成单元还需要超过这么多个暂定词时不投机（暂定部分太长，多半不可靠）。"""
        self.translator = translator
        self.on_update = on_update
        self.builder = UnitBuilder()
        self.speculative = speculative and translator is not None
        self.on_speculation = on_speculation or (lambda text: None)
        self.max_speculative_words = max_speculative_words
        self.stats = {"speculated": 0, "adopted": 0}
        self._context: deque[str] = deque(maxlen=context_sentences)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="translate")
        self._next_id = 0
        self._spec: SpeculativeJob | None = None
        self._lock = threading.Lock()

    @property
    def pending(self) -> str:
        """已经确认、但还没凑够送去翻译的法语。"""
        return " ".join(self.builder.pending)

    @property
    def speculative_translation(self) -> str:
        """当前投机翻译的译文（还没被正式单元采用的），没有则为空。"""
        with self._lock:
            job = self._spec
            return job.translation.strip() if job is not None and not job.cancelled else ""

    def feed(self, committed: str, final: bool, word_times: list[float] | None = None) -> list[TranslationUnit]:
        """把新确认的法语交给翻译阶段，返回因此新建的翻译单元。
        word_times：committed 里每个词在音频流里的时间（秒），用来给会话记录标上录音里的位置。"""
        units = []
        for source, start in self.builder.add_timed(committed, final, word_times):
            with self._lock:
                unit = TranslationUnit(self._next_id, source, now(), audio_start=start)
                self._next_id += 1
                context = list(self._context)
                self._context.append(source)
                job = self._adopt_speculation(unit)
            units.append(unit)
            if self.translator is None:  # 不翻译（没有密钥）：只有原文，直接算完成
                unit.done_at = unit.ready_at
                self.on_update(unit)
                continue
            self.on_update(unit)
            if job is None:
                self._pool.submit(self._translate, unit, context)
        return units

    def speculate(self, partial: str) -> None:
        """在“已确认但还没送出的词 + 暂定尾巴”里找第一个翻译单元；找到就先送去翻译。"""
        if not self.speculative:
            return
        pending = list(self.builder.pending)
        words = pending + partial.split()
        size = self.builder.first_unit(words)
        if size <= len(pending) or size - len(pending) > self.max_speculative_words:
            return
        source = " ".join(words[:size])
        key = unit_key(source)
        with self._lock:
            if self._spec is not None and self._spec.key == key:
                return  # 这句已经在投机翻译了
            if self._spec is not None:
                self._spec.cancelled = True  # 暂定文字变了，之前那份投机作废
            job = SpeculativeJob(source, key, list(self._context))
            self._spec = job
            self.stats["speculated"] += 1
        self._pool.submit(self._run_speculation, job)

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
                    text = job.translation.strip()
                    still_live = unit is None and self._spec is job  # 还没被采用、也没被新的投机替换
                if unit is not None:
                    self.on_update(unit)
                elif still_live:
                    self.on_speculation(text)
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

    def _translate(self, unit: TranslationUnit, context: list[str]) -> None:
        try:
            for delta in self.translator.stream(unit.source, context):
                if unit.first_token_at is None:
                    unit.first_token_at = now()
                unit.translation += delta
                self.on_update(unit)
        except Exception as exc:  # 翻译失败只影响这一句，不能让整个程序停下
            unit.error = str(exc)
            logger.error("翻译失败：%s", exc)
        unit.translation = unit.translation.strip()
        unit.done_at = now()
        self.on_update(unit)

    def close(self) -> None:
        with self._lock:
            if self._spec is not None:
                self._spec.cancelled = True
        self._pool.shutdown(wait=True)
        if self.translator is not None:
            self.translator.close()

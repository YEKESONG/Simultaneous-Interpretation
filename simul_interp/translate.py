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
        started = time.monotonic()
        try:
            response = self._client.get("/models")
        except httpx.HTTPError as exc:
            logger.warning("连接翻译服务失败：%s", exc)
            return
        if response.status_code in (401, 403):
            raise TranslationError(f"翻译 API 密钥无效（HTTP {response.status_code}），请检查 .env")
        logger.info("已连接翻译服务 %s（%.0f ms）", self.cfg.base_url, (time.monotonic() - started) * 1000)

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

    def add(self, committed: str, final: bool) -> list[str]:
        units = []
        for word in committed.split():
            self.pending.append(word)
            count = len(self.pending)
            if (
                _SENTENCE_END.search(word)
                or (_CLAUSE_END.search(word) and count >= self.clause_min_words)
                or count >= self.max_words
            ):
                units.append(" ".join(self.pending))
                self.pending = []
        if final and self.pending:
            units.append(" ".join(self.pending))
            self.pending = []
        return units


@dataclass
class TranslationUnit:
    id: int
    source: str
    ready_at: float  # 原文确认、送去翻译的 time.monotonic()
    translation: str = ""
    first_token_at: float | None = None
    done_at: float | None = None
    error: str = ""

    @property
    def done(self) -> bool:
        return self.done_at is not None


class TranslationStage:
    def __init__(
        self,
        translator: ChatTranslator | MockTranslator | None,
        on_update: Callable[[TranslationUnit], None],
        context_sentences: int = 3,
        max_workers: int = 3,
    ) -> None:
        self.translator = translator
        self.on_update = on_update
        self.builder = UnitBuilder()
        self._context: deque[str] = deque(maxlen=context_sentences)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="translate")
        self._next_id = 0
        self._lock = threading.Lock()

    @property
    def pending(self) -> str:
        """已经确认、但还没凑够送去翻译的法语。"""
        return " ".join(self.builder.pending)

    def feed(self, committed: str, final: bool) -> list[TranslationUnit]:
        """把新确认的法语交给翻译阶段，返回因此新建的翻译单元（此时译文还是空的）。"""
        units = []
        for source in self.builder.add(committed, final):
            with self._lock:
                unit = TranslationUnit(self._next_id, source, time.monotonic())
                self._next_id += 1
                context = list(self._context)
                self._context.append(source)
            units.append(unit)
            if self.translator is None:  # 不翻译（没有密钥）：只有原文，直接算完成
                unit.done_at = unit.ready_at
                self.on_update(unit)
                continue
            self.on_update(unit)
            self._pool.submit(self._translate, unit, context)
        return units

    def _translate(self, unit: TranslationUnit, context: list[str]) -> None:
        try:
            for delta in self.translator.stream(unit.source, context):
                if unit.first_token_at is None:
                    unit.first_token_at = time.monotonic()
                unit.translation += delta
                self.on_update(unit)
        except Exception as exc:  # 翻译失败只影响这一句，不能让整个程序停下
            unit.error = str(exc)
            logger.error("翻译失败：%s", exc)
        unit.translation = unit.translation.strip()
        unit.done_at = time.monotonic()
        self.on_update(unit)

    def close(self) -> None:
        self._pool.shutdown(wait=True)
        if self.translator is not None:
            self.translator.close()

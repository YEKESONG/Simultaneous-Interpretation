"""整条流水线：音频源 → 语音检测 → 流式识别 → 翻译 → 界面 / 会话记录。

界面只需要实现 View 的两个回调，终端版和悬浮窗版共用同一条流水线。
回调可能来自不同的线程（识别线程、翻译线程），界面自己负责线程安全。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Protocol

from .asr import create_backend
from .asr.runner import AsrRunner
from .asr.types import AsrUpdate
from .audio import create_source
from .config import Config
from .transcript import TranscriptWriter
from .translate import ChatTranslator, MockTranslator, TranslationStage, TranslationUnit

logger = logging.getLogger(__name__)


class View(Protocol):
    def on_live(self, pending: str, partial: str) -> None:
        """正在说的话：pending = 已确认但还没送去翻译的法语，partial = 还没确认的暂定尾巴。"""

    def on_unit(self, unit: TranslationUnit) -> None:
        """一段原文送去翻译（译文为空）、译文流式更新、翻译完成时都会调用。"""

    def on_status(self, text: str) -> None:
        """运行状态，比如“正在加载识别模型”；空字符串表示正常收听中。"""


class Interpreter:
    def __init__(
        self,
        cfg: Config,
        view: View,
        translate: bool = True,
        mock_translate: bool = False,
        save_transcript: bool = True,
    ) -> None:
        self.cfg = cfg
        self.view = view
        self.source = create_source(cfg)
        self.backend = create_backend(cfg)

        self.translator: ChatTranslator | MockTranslator | None = None
        if mock_translate:
            self.translator = MockTranslator()
        elif translate:
            key = cfg.api_key()
            if key:
                self.translator = ChatTranslator(cfg.translate, key)
            else:
                logger.warning("没有设置翻译 API 密钥（%s），只显示法语原文", cfg.translate.api_key_env)
        self.translation = TranslationStage(self.translator, self._on_unit, cfg.translate.context_sentences)
        self.asr = AsrRunner(cfg, self.backend, self._on_asr)

        self.transcript: TranscriptWriter | None = None
        if save_transcript and cfg.transcript.enabled:
            stem = Path(cfg.audio.file).stem if cfg.audio.source == "file" else None
            if mock_translate:
                translation = "模拟翻译（测试用）"
            elif self.translator:
                translation = f"{cfg.translate.model}（{cfg.translate.base_url}）"
            else:
                translation = "未翻译"
            meta = {
                "音源": self.source.name,
                "识别模型": cfg.asr.model,
                "识别语言": cfg.asr.language,
                "翻译": translation,
            }
            self.transcript = TranscriptWriter(cfg.transcript_dir(), self.source.name, meta, stem)
        # 几段译文并发进行，完成的先后不一定按顺序；会话记录要按原文顺序写
        self._finished: dict[int, TranslationUnit] = {}
        self._next_to_write = 0
        self._write_lock = threading.Lock()

    def run(self) -> None:
        """阻塞运行，直到音频源结束或 stop()；Ctrl+C 也会走到 finally 里把收尾做完。"""
        try:
            if self.translator is not None:
                self.view.on_status("正在连接翻译服务……")
                self.translator.warm_up()
            self.view.on_status("正在加载识别模型……")
            self.asr.run(self.source, on_started=lambda: self.view.on_status(""))
        finally:
            self.translation.close()
            if self.transcript is not None:
                self.transcript.close()
                logger.info("会话记录已保存：%s", self.transcript.path)
            self.view.on_status("已结束")

    def stop(self) -> None:
        self.asr.stop()
        self.source.stop()

    def reconnect_audio(self) -> None:
        """切换输出设备后重新连接（Windows 需要手动；macOS 本来就会自动重连）。"""
        try:
            self.source.reconnect()
        except Exception as exc:
            logger.error("重新连接音频设备失败：%s", exc)
            self.view.on_status(f"重新连接音频设备失败：{exc}")

    def _on_asr(self, update: AsrUpdate) -> None:
        self.translation.feed(update.committed, update.final)
        self.view.on_live(self.translation.pending, update.partial)

    def _on_unit(self, unit: TranslationUnit) -> None:
        self.view.on_unit(unit)
        if unit.done and self.transcript is not None:
            with self._write_lock:
                self._finished[unit.id] = unit
                while self._next_to_write in self._finished:
                    self.transcript.add(self._finished.pop(self._next_to_write))
                    self._next_to_write += 1

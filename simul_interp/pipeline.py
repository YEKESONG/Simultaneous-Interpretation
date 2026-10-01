"""整条流水线：音频源 → 语音检测 → 流式识别 → 翻译 → 界面 / 会话记录；内录时另外把音频存一份录音。

界面只需要实现 View 的两个回调，终端版和悬浮窗版共用同一条流水线。
回调可能来自不同的线程（识别线程、翻译线程），界面自己负责线程安全。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Protocol

from .asr import create_backend
from .asr.runner import AsrRunner
from .asr.types import AsrUpdate
from .audio import AudioChunk, create_source
from .config import Config
from .recording import WavRecorder
from .transcript import TranscriptWriter
from .translate import ChatTranslator, MockTranslator, TranslationStage, TranslationUnit

logger = logging.getLogger(__name__)

# 一段话的第一个词从开始说到被确认，实测要 0.8~2.1 秒（中位数 1.4 秒，DEVLOG S14b）。
# 会话记录里的时间按“确认时刻 - 这个值”估算：取 2 秒，标出来的位置一般落在这段话开头之前 0~1 秒，从那里开始听正好
CONFIRM_LAG_S = 2.0


class View(Protocol):
    def on_live(self, pending: str, partial: str, translation: str = "") -> None:
        """正在说的话：pending = 已确认但还没送去翻译的法语，partial = 还没确认的暂定尾巴，
        translation = 这句的投机译文（还没确认，可能会变）。"""

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
        save_recording: bool = True,
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
        self.translation = TranslationStage(
            self.translator,
            self._on_unit,
            cfg.translate.context_sentences,
            speculative=cfg.translate.speculative,
            on_speculation=self._on_speculation,
            max_speculative_words=cfg.translate.speculative_max_words,
        )
        self._live = ("", "")  # 最近一次的 (pending, partial)，投机译文更新时要一起重新显示
        self.asr = AsrRunner(cfg, self.backend, self._on_asr)

        # 内录的会话按开始时间命名，录音和会话记录用同一个名字，方便配对；
        # 用音频文件做输入时，记录跟着文件名走，也不用再录一份（文件本身就是录音）
        live = cfg.audio.source != "file"
        session = f"同传_{time.strftime('%Y-%m-%d_%H%M%S')}" if live else Path(cfg.audio.file).stem

        self.recorder: WavRecorder | None = None
        if save_recording and cfg.recording.enabled and live:
            self.recorder = WavRecorder(cfg.recording_dir() / f"{session}.wav", on_error=self._on_recording_error)
            self.source.on_chunk = self._record
            logger.info("本次会话的录音保存到：%s", self.recorder.path)

        self.transcript: TranscriptWriter | None = None
        if save_transcript and cfg.transcript.enabled:
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
            if self.recorder is not None:
                meta["录音"] = str(self.recorder.path)
                meta["时间"] = "每段前面的时间是这段话在录音里的位置（一般比开头早不到一秒，可能差一两秒）"
            elif not live:
                meta["时间"] = "每段前面的时间是这段话在音频文件里的位置（一般比开头早不到一秒，可能差一两秒）"
            else:
                meta["时间"] = "每段前面的时间从开始有声音算起（电脑没有声音输出的空档不计时）"
            try:
                self.transcript = TranscriptWriter(cfg.transcript_dir(), self.source.name, meta, session)
            except OSError:
                if self.recorder is not None:
                    self.recorder.close()  # 启动失败：别留下一个空的录音文件
                raise
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
            self._close_recording()  # 先收好录音：后面等翻译收尾可能要几秒
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

    def _record(self, chunk: AudioChunk) -> None:
        self.recorder.write(chunk.samples)

    def _on_recording_error(self, message: str) -> None:
        self.view.on_status(f"录音已停止：{message}")

    def _close_recording(self) -> None:
        if self.recorder is None:
            return
        self.recorder.close()
        if not self.recorder.seconds:
            logger.info("这次没有录到任何声音，没有留下录音文件")
            return
        seconds = round(self.recorder.seconds)
        size_mb = self.recorder.seconds * self.recorder.sample_rate * 2 / 1e6
        logger.info(
            "录音已保存：%s（%d 分 %02d 秒，%.1f MB）", self.recorder.path, seconds // 60, seconds % 60, size_mb
        )

    def _on_asr(self, update: AsrUpdate) -> None:
        # 给每个词标上它在音频流（也就是录音）里的大致位置。目前只知道“确认它的那次识别听到了哪里”，
        # 这比它真正说出口的时刻晚一点，所以往前提 CONFIRM_LAG_S 秒
        heard = max(0.0, update.audio_end - CONFIRM_LAG_S)
        self.translation.feed(update.committed, update.final, [heard] * len(update.committed.split()))
        if not update.final:
            self.translation.speculate(update.partial)
        self._live = (self.translation.pending, update.partial)
        self.view.on_live(*self._live, self.translation.speculative_translation)

    def _on_speculation(self, text: str) -> None:
        self.view.on_live(*self._live, text)

    def _on_unit(self, unit: TranslationUnit) -> None:
        self.view.on_unit(unit)
        if unit.done and self.transcript is not None:
            with self._write_lock:
                self._finished[unit.id] = unit
                while self._next_to_write in self._finished:
                    self.transcript.add(self._finished.pop(self._next_to_write))
                    self._next_to_write += 1

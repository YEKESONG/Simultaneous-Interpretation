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
from .audio.switchable import SwitchableSource
from .config import Config
from .languages import language_name
from .recording import WavRecorder
from .transcript import TranscriptWriter, format_position
from .translate import ChatTranslator, MockTranslator, TranslationStage, TranslationUnit

logger = logging.getLogger(__name__)

# 一段话的第一个词从开始说到被确认，实测要 0.8~2.1 秒（中位数 1.4 秒，DEVLOG S14b）。
# 会话记录里的时间按“确认时刻 - 这个值”估算：取 2 秒，标出来的位置一般落在这段话开头之前 0~1 秒，从那里开始听正好
CONFIRM_LAG_S = 2.0


class View(Protocol):
    def on_live(self, pending: str, partial: str, translation: str = "") -> None:
        """正在说的话：pending = 已确认但还没送去翻译的法语，partial = 还没确认的暂定尾巴，
        translation = 这句话到目前为止的译文（草稿或投机译文：话还没说完、原文还没确认，可能会变）。"""

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
        start_paused: bool = False,
    ) -> None:
        """start_paused：准备好之后先不监听，等调用 resume()（控制面板上的“开始监听”）。"""
        self.cfg = cfg
        self.view = view
        self.state = "loading"  # loading（在准备）→ listening（在监听）⇄ paused（暂停）→ ended（结束）
        source = create_source(cfg)
        # 内录和外录在运行中可以互换（控制面板上的“音源”）；用音频文件模拟时不用换
        self.source = source if cfg.audio.source == "file" else SwitchableSource(source)
        self.source.on_status = view.on_status  # 比如麦克风没有权限、设备断开：直接显示在界面上
        self.backend = create_backend(cfg)
        if start_paused:
            self.source.pause()

        self.translator: ChatTranslator | MockTranslator | None = None
        if mock_translate:
            self.translator = MockTranslator()
        elif translate:
            key = cfg.api_key()
            if key:
                self.translator = ChatTranslator(
                    cfg.translate, key, source_language=language_name(cfg.asr.language)
                )
            else:
                logger.warning("没有设置翻译 API 密钥（%s），只显示原文", cfg.translate.api_key_env)
        self.translation = TranslationStage(
            self.translator,
            self._on_unit,
            cfg.translate.context_sentences,
            speculative=cfg.translate.speculative,
            on_speculation=self._on_speculation,
            max_speculative_words=cfg.translate.speculative_max_words,
            incomplete_wait_s=cfg.translate.incomplete_wait_s,
            on_flush=self._on_flush,
            live_draft=cfg.translate.live_draft,
            draft_min_words=cfg.translate.draft_min_words,
            draft_step_words=cfg.translate.draft_step_words,
        )
        self._live = ("", "")  # 最近一次的 (pending, partial)，投机译文更新时要一起重新显示
        self.asr = AsrRunner(cfg, self.backend, self._on_asr, on_speech_start=self.translation.speech_started)

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
                "识别语言": language_name(cfg.asr.language),
                "译成": cfg.translate.target_language,
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
        self._notes: dict[int, list[str]] = {}  # 要插在某个翻译单元之前的说明（换语言、暂停后继续）
        self._write_lock = threading.Lock()

    def run(self) -> None:
        """阻塞运行，直到音频源结束或 stop()；Ctrl+C 也会走到 finally 里把收尾做完。"""
        try:
            if self.translator is not None:
                self.view.on_status("正在连接翻译服务……")
                self.translator.warm_up()
            self.view.on_status("正在加载识别模型……")
            self.asr.run(self.source, on_started=self._on_started)
        finally:
            self.state = "ended"
            self._close_recording()  # 先收好录音：后面等翻译收尾可能要几秒
            self.translation.close()
            if self.transcript is not None:
                self.transcript.close()
                logger.info("会话记录已保存：%s", self.transcript.path)
            self.view.on_status("已结束")

    def stop(self) -> None:
        self.asr.stop()
        self.source.stop()

    def _on_started(self) -> None:
        """模型加载好、音频源已经启动。"""
        self.state = "paused" if self.source.paused else "listening"
        self.view.on_status("已暂停，点“开始监听”开始" if self.source.paused else "")

    # ---- 运行中的控制（控制面板在界面线程里调用） ----

    def pause(self) -> None:
        """暂停监听：不识别、不翻译，录音也跟着停。正说到一半的话会先收尾。"""
        if self.state != "listening":
            return
        self.source.pause(self.cfg.vad.min_silence_ms / 1000 + 0.3)
        self.state = "paused"
        self.view.on_status("已暂停")

    def resume(self) -> None:
        if self.state != "paused":
            return
        if self.source.position > 0:  # 录音里没有暂停的那段时间，在记录里留个记号
            self._note("暂停后从这里继续")
        self.asr.reset_context()  # 暂停前后的声音接不上，之前的句子不能再给识别当上文
        self.source.resume()
        self.state = "listening"
        self.view.on_status("")

    def set_languages(self, source: str, target: str) -> None:
        """运行中换语言，从下一句话开始生效。source 是识别语言的代码（如 "en"），target 是译文语言的名字。"""
        if (source, target) == (self.cfg.asr.language, self.cfg.translate.target_language):
            return
        self.cfg.asr.language = source
        self.cfg.translate.target_language = target  # 翻译的提示词每次请求时现读配置，改了就生效
        self.asr.set_language(source)
        if isinstance(self.translator, ChatTranslator):
            self.translator.source_language = language_name(source)
        self.translation.reset_context()  # 之前的句子是另一种语言的，不能再当上文
        self._note(f"{language_name(source)} → {target}")
        logger.info("语言改为：%s → %s", language_name(source), target)

    def set_source(self, kind: str, device: str = "") -> None:
        """运行中换音源：kind 是 "system"（电脑播放的声音）或 "mic"（麦克风；device 是设备名的一部分或编号，
        空 = 系统默认）。新的打不开时换回原来的，并把错误抛出来（界面上显示原因）。可以在任何线程调用。"""
        if not isinstance(self.source, SwitchableSource):
            raise RuntimeError("用音频文件模拟时不能换音源")
        old = (self.cfg.audio.source, self.cfg.audio.device)
        if (kind, device) == old:
            return
        self.cfg.audio.source, self.cfg.audio.device = kind, device
        try:
            new = create_source(self.cfg)  # 麦克风找不到时在这一步就报错，原来的音源不受影响
        except Exception:
            self.cfg.audio.source, self.cfg.audio.device = old
            raise

        def restore():
            self.cfg.audio.source, self.cfg.audio.device = old
            return create_source(self.cfg)

        self.source.switch(new, restore)
        self.asr.reset_context()  # 声音换了来源，之前的句子不能再给识别当上文
        self._note(f"音源改为「{self.source.name}」")
        logger.info("音源改为：%s", self.source.name)

    def output_folders(self) -> list[Path]:
        """这次会话的记录和录音所在的文件夹（去掉重复的）。"""
        paths = [item.path.parent for item in (self.transcript, self.recorder) if item is not None]
        return list(dict.fromkeys(paths))

    def _note(self, text: str) -> None:
        """在会话记录里插一行说明，位置在下一个翻译单元之前，并标上录音里的时间。"""
        if self.transcript is None:
            return
        stamp = format_position(int(self.source.position))
        with self._write_lock:
            self._notes.setdefault(self.translation.next_id, []).append(f"{stamp} 起：{text}")

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
        self.view.on_live(*self._live, self.translation.live_translation)

    def _on_speculation(self, text: str) -> None:
        self.view.on_live(*self._live, text)

    def _on_flush(self) -> None:
        """等了一阵没有下文，留着的半句已经送去翻译：界面上“正在说”的那一行要清掉。"""
        self._live = (self.translation.pending, "")
        self.view.on_live(*self._live, "")

    def _on_unit(self, unit: TranslationUnit) -> None:
        self.view.on_unit(unit)
        if unit.done and self.transcript is not None:
            with self._write_lock:
                self._finished[unit.id] = unit
                while self._next_to_write in self._finished:
                    for text in self._notes.pop(self._next_to_write, []):
                        self.transcript.note(text)
                    self.transcript.add(self._finished.pop(self._next_to_write))
                    self._next_to_write += 1

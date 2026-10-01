"""录音保存：同传时把听到的声音同步存成 WAV，方便事后对照录音校对识别和译文。

存的就是送给识别模型的那一路音频（16 kHz 单声道），所以录音里的位置和音频流时间完全一致。
16 位 WAV 每小时约 115 MB。
"""

from __future__ import annotations

import logging
import struct
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .audio.base import SAMPLE_RATE

logger = logging.getLogger(__name__)

HEADER_BYTES = 44
MAX_DATA_BYTES = 0xFFFFFFFF - HEADER_BYTES  # WAV 用 32 位整数记长度，最多约 4 GB（16 kHz 单声道约 37 小时）


def wav_header(data_bytes: int, sample_rate: int) -> bytes:
    """16 位单声道 PCM 的 WAV 文件头（44 字节）。"""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,  # fmt 块的长度
        1,  # 1 = 不压缩的 PCM
        1,  # 单声道
        sample_rate,
        sample_rate * 2,  # 每秒字节数
        2,  # 每个采样点的字节数
        16,  # 位深
        b"data",
        data_bytes,
    )


class WavRecorder:
    """边录边写的 WAV 文件。

    WAV 的文件头里记着数据有多长，一般要到结束时才知道。如果只在结束时填，程序一旦被强制退出，
    留下的文件头是错的，播放器打不开。所以这里每写 sync_every_s 秒的音频就把文件头更新一次：
    任何时候中断，已经写下的部分都是一个能正常播放的文件，最多少最后几秒。

    写盘出错（磁盘满了、移动硬盘被拔掉）时只停止录音并通知一次，不抛异常：录音不能连累同传本身。
    """

    def __init__(
        self,
        path: Path,
        sample_rate: int = SAMPLE_RATE,
        sync_every_s: float = 2.0,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.sample_rate = sample_rate
        self.error = ""
        self._on_error = on_error
        self._sync_frames = max(1, int(sync_every_s * sample_rate))
        self._frames = 0  # 已经写了多少个采样点
        self._synced = 0  # 文件头里记到了第几个采样点
        self._lock = threading.Lock()
        # 用 "x" 模式创建：同名文件已经存在就失败，换个带序号的名字再试，绝不覆盖已有的录音
        candidate, number = path, 2
        while True:
            try:
                self._file = candidate.open("xb")
                break
            except FileExistsError:
                candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
                number += 1
        self.path = candidate
        self._file.write(wav_header(0, sample_rate))
        self._file.flush()

    @property
    def active(self) -> bool:
        """是否还在录（正常结束或写盘出错之后为 False）。"""
        return self._file is not None

    @property
    def seconds(self) -> float:
        """已经录了多长。"""
        return self._frames / self.sample_rate

    def write(self, samples: np.ndarray) -> None:
        """追加一段 float32 音频（-1 ~ 1）。"""
        with self._lock:
            if self._file is None:
                return
            try:
                if (self._frames + len(samples)) * 2 > MAX_DATA_BYTES:
                    raise OSError("录音已达到 WAV 文件 4 GB 的上限")
                pcm = np.nan_to_num(np.clip(samples, -1.0, 1.0)) * 32767.0
                self._file.write(pcm.astype("<i2").tobytes())
                self._frames += len(samples)
                if self._frames - self._synced >= self._sync_frames:
                    self._sync()
            except OSError as exc:
                self._abort(exc)

    def close(self) -> None:
        """结束录音：把文件头写对并关闭文件。一点声音都没录到就不留空文件。"""
        with self._lock:
            if self._file is None:
                return
            try:
                self._sync()
            except OSError as exc:
                self.error = str(exc)
                logger.error("录音收尾失败：%s", exc)
            self._release()

    def _sync(self) -> None:
        """把文件头里的长度更新成已经写下的数据量，并把缓冲区里的内容交给操作系统。"""
        self._file.seek(0)  # 移动位置之前，Python 会先把还在缓冲区里的音频写出去
        self._file.write(wav_header(self._frames * 2, self.sample_rate))
        self._file.seek(0, 2)  # 回到文件末尾，接着追加
        self._file.flush()
        self._synced = self._frames

    def _abort(self, exc: OSError) -> None:
        self.error = str(exc)
        logger.error("录音写入失败，已停止录音（同传不受影响）：%s", exc)
        self._release()
        if self._on_error is not None:
            self._on_error(self.error)

    def _release(self) -> None:
        file, self._file = self._file, None
        try:
            file.close()
            if self._frames == 0:
                self.path.unlink(missing_ok=True)
        except OSError:
            pass

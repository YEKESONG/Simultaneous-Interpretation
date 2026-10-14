"""Windows（以及 Linux）识别后端：faster-whisper（CTranslate2）。有 NVIDIA 显卡时用 CUDA。

和 macOS 的 MLX 后端用同一个模型（Whisper large-v3 turbo），只是格式不同。默认不自动下载模型（约 1.6 GB），
需要在 config.toml 里设 [asr] allow_download = true 才会下载。

在 Windows 实机（RTX 4060 Laptop，CUDA float16）上测过，延迟和准确率见 DEVLOG S30。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import numpy as np

from .common import HALLUCINATIONS, merge_word_pieces
from .types import Segment

logger = logging.getLogger(__name__)


def _add_nvidia_dll_dirs() -> None:
    """Windows：让 ctranslate2 找到 pip 装的 CUDA 12 库（nvidia-cublas-cu12、nvidia-cudnn-cu12）。

    ctranslate2 要 cublas64_12.dll 和 cuDNN 9，第一次用显卡时才按 PATH 去找（os.add_dll_directory 不管用，
    实测过）。这两个包把 DLL 放在 site-packages/nvidia/*/bin，不在 PATH 里，所以加进去。
    没装这两个包时什么都不做，用系统里装的 CUDA Toolkit 12 和 cuDNN 9。"""
    if sys.platform != "win32":
        return
    try:
        import nvidia  # 命名空间包，只有装了上面的包才有
    except ImportError:
        return
    dirs = [str(d) for root in nvidia.__path__ for d in sorted(Path(root).glob("*/bin"))]
    os.environ["PATH"] = os.pathsep.join([*dirs, os.environ.get("PATH", "")])


class FasterWhisperBackend:
    def __init__(
        self,
        model: str,
        language: str,
        device: str = "auto",
        compute_type: str = "default",
        allow_download: bool = False,
    ) -> None:
        self.model_id = model
        self.language = language
        self.device = device
        self.compute_type = compute_type
        self.allow_download = allow_download
        self._model = None

    def load(self) -> None:
        if self._model is not None:
            return
        _add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel

        logger.info("加载识别模型 %s（device=%s）……", self.model_id, self.device)
        try:
            self._model = WhisperModel(
                self.model_id,
                device=self.device,
                compute_type=self.compute_type,
                local_files_only=not self.allow_download,
            )
        except Exception as exc:
            if self.allow_download:
                raise
            raise RuntimeError(
                f"本地没有找到模型 {self.model_id}。为避免意外下载几 GB 的文件，默认不自动下载；"
                "确认要下载的话，在 config.toml 的 [asr] 里设 allow_download = true。"
            ) from exc
        self.transcribe(np.zeros(16000, dtype=np.float32))  # 预热

    def set_language(self, language: str) -> None:
        """运行中换识别语言：模型是多语言的，不用重新加载，下一次识别就按新语言来。"""
        self.language = language

    def transcribe(
        self, audio: np.ndarray, prompt: str = "", word_timestamps: bool = False, max_tokens: int | None = None
    ) -> list[Segment]:
        segments, _ = self._model.transcribe(
            audio,
            language=self.language,
            task="transcribe",
            beam_size=1,  # 默认 5：束搜索更准一点，但慢得多；和 MLX 后端一样用贪心解码
            temperature=0.0,  # 默认是 6 个温度的重试序列，结果不理想时会重复解码
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
            word_timestamps=word_timestamps,
            max_new_tokens=max_tokens,
            vad_filter=False,  # 语音检测已经在前面做过了
        )
        result = []
        for s in segments:  # segments 是生成器：遍历时才真正解码
            text = s.text.strip()
            if HALLUCINATIONS.search(text):
                continue
            raw = [(w.start, w.end, w.word) for w in (s.words or [])]
            result.append(Segment(float(s.start), float(s.end), text, merge_word_pieces(raw)))
        return result

    def word_probability(self, audio: np.ndarray, word: str) -> float:
        """模型认为这段音频的第一个词就是 word 的概率（含义见 MlxWhisperBackend.word_probability）。

        faster-whisper 的逐词时间戳里自带每个词的概率，算法相同，所以带着逐词时间戳重新识别一次、
        取第一个词的；第一个词不是 word 就返回 0。比 MLX 那边的做法慢（要完整解码一遍，实测每次 0.4～0.6 秒）。
        实测：真的 “Merci” 0.80～0.94，杂音上编出来的都不超过 0.43（DEVLOG S30）。"""
        segments, _ = self._model.transcribe(
            audio,
            language=self.language,
            task="transcribe",
            beam_size=1,
            temperature=0.0,
            condition_on_previous_text=False,
            word_timestamps=True,
            vad_filter=False,
        )
        for s in segments:
            for w in s.words or []:
                same = "".join(c for c in w.word.lower() if c.isalnum()) == word.lower()
                return float(w.probability) if same else 0.0
        return 0.0

"""Windows（以及 Linux）识别后端：faster-whisper（CTranslate2）。有 NVIDIA 显卡时用 CUDA。

和 macOS 的 MLX 后端用同一个模型（Whisper large-v3），只是格式不同。默认不自动下载模型（约 3 GB），
需要在 config.toml 里设 [asr] allow_download = true 才会下载。

注意：这部分代码没有在 Windows 实机上运行过；转换逻辑有单元测试（用假的 WhisperModel）。
"""

from __future__ import annotations

import logging

import numpy as np

from .common import HALLUCINATIONS, merge_word_pieces
from .types import Segment

logger = logging.getLogger(__name__)


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

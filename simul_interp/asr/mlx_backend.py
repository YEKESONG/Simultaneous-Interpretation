"""macOS（Apple Silicon）识别后端：用 MLX 跑 Whisper。"""

from __future__ import annotations

import logging

import numpy as np

from .common import HALLUCINATIONS, merge_word_pieces
from .types import Segment

logger = logging.getLogger(__name__)


def resolve_local_model(repo_id: str) -> str:
    """只在本地缓存里找模型，找不到就报错，绝不自动下载（语音识别模型动辄几 GB，下载前要用户同意）。"""
    from pathlib import Path

    if Path(repo_id).expanduser().exists():
        return str(Path(repo_id).expanduser())
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        return snapshot_download(repo_id=repo_id, local_files_only=True)
    except LocalEntryNotFoundError as exc:
        raise RuntimeError(
            f"本地没有找到模型 {repo_id}。为避免意外下载几 GB 的文件，程序不会自动下载；"
            f"确认要用这个模型的话，请先手动下载到 Hugging Face 缓存。"
        ) from exc


class MlxWhisperBackend:
    def __init__(self, model: str, language: str, quantize_bits: int = 0) -> None:
        self.model_id = model
        self.language = language
        self.quantize_bits = quantize_bits
        self._path: str | None = None
        self._transcribe = None

    def load(self) -> None:
        if self._transcribe is not None:
            return
        import mlx_whisper

        self._path = resolve_local_model(self.model_id)  # 传本地路径给 mlx_whisper，它就不会再联网检查
        self._transcribe = mlx_whisper.transcribe
        logger.info("加载识别模型 %s ……", self.model_id)
        if self.quantize_bits:
            self._quantize()
        self.transcribe(np.zeros(16000, dtype=np.float32))  # 预热：加载权重、编译 GPU 内核

    def _quantize(self) -> None:
        """在内存里把权重量化成 8 位或 4 位：还是同一个模型，不下载任何东西。
        解码器生成每个 token 都要把全部权重读一遍，速度卡在内存带宽上，权重变小就更快。"""
        import mlx.core as mx
        import mlx.nn as nn
        from mlx_whisper.load_models import load_model
        from mlx_whisper.transcribe import ModelHolder

        model = load_model(self._path, dtype=mx.float16)
        nn.quantize(model, group_size=64, bits=self.quantize_bits)
        # mlx_whisper 按路径缓存模型（ModelHolder）：把量化后的模型放进缓存，transcribe 就会直接用它
        ModelHolder.model, ModelHolder.model_path = model, self._path
        logger.info("已在内存中把识别模型量化为 %d 位", self.quantize_bits)

    def transcribe(
        self, audio: np.ndarray, prompt: str = "", word_timestamps: bool = False, max_tokens: int | None = None
    ) -> list[Segment]:
        result = self._transcribe(
            audio,
            path_or_hf_repo=self._path,
            language=self.language,
            task="transcribe",
            temperature=0.0,  # 只解码一次：多个温度会在结果不理想时重试，延迟翻倍
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
            word_timestamps=word_timestamps,
            verbose=None,
            sample_len=max_tokens,  # 默认最多 224 个 token；陷入重复循环时会一直生成到上限
        )
        segments = []
        for s in result["segments"]:
            text = s["text"].strip()
            if HALLUCINATIONS.search(text):
                continue
            raw = [(w["start"], w["end"], w["word"]) for w in s.get("words", [])]
            segments.append(Segment(float(s["start"]), float(s["end"]), text, merge_word_pieces(raw)))
        return segments


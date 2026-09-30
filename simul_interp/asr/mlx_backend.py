"""macOS（Apple Silicon）识别后端：用 MLX 跑 Whisper。"""

from __future__ import annotations

import logging
import re

import numpy as np

from .types import Segment

logger = logging.getLogger(__name__)

# Whisper 在静音、音乐上常见的“幻觉”：训练数据里的字幕署名，真人几乎不会说出口，出现就丢掉。
# 注意不要过滤“abonnez-vous”“merci d'avoir regardé”这类视频里真的会说的话。
HALLUCINATIONS = re.compile(
    r"sous-titrage|sous-titres réalisés|sous-titres par|amara\.org|^\W*$",
    re.IGNORECASE,
)


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
    def __init__(self, model: str, language: str) -> None:
        self.model_id = model
        self.language = language
        self._path: str | None = None
        self._transcribe = None

    def load(self) -> None:
        if self._transcribe is not None:
            return
        import mlx_whisper

        self._path = resolve_local_model(self.model_id)  # 传本地路径给 mlx_whisper，它就不会再联网检查
        self._transcribe = mlx_whisper.transcribe
        logger.info("加载识别模型 %s ……", self.model_id)
        self.transcribe(np.zeros(16000, dtype=np.float32))  # 预热：加载权重、编译 GPU 内核

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
            segments.append(Segment(float(s["start"]), float(s["end"]), text, _merge_word_pieces(s.get("words", []))))
        return segments


def _merge_word_pieces(raw_words: list[dict]) -> list[tuple[float, float, str]]:
    """Whisper 的逐词输出会把法语的省音和连字符拆开：d'être → “d” + “'être”，Est-ce → “Est” + “-ce”。
    真正的新词都以空格开头，所以没有前导空格的片段并回前一个词，和 text.split() 的分词保持一致。"""
    words: list[tuple[float, float, str]] = []
    for w in raw_words:
        raw = w["word"]
        if words and not raw.startswith(" "):
            start, _, text = words[-1]
            words[-1] = (start, float(w["end"]), text + raw.strip())
        else:
            words.append((float(w["start"]), float(w["end"]), raw.strip()))
    return words

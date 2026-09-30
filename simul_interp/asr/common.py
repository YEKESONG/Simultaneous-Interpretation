"""各识别后端共用的小工具。"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Whisper 在静音、音乐上常见的“幻觉”：训练数据里的字幕署名，真人几乎不会说出口，出现就丢掉。
# 注意不要过滤 “abonnez-vous”“merci d'avoir regardé” 这类视频里真的会说的话。
HALLUCINATIONS = re.compile(
    r"sous-titrage|sous-titres réalisés|sous-titres par|amara\.org|^\W*$",
    re.IGNORECASE,
)


def merge_word_pieces(raw_words: Iterable[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Whisper 的逐词输出会把法语的省音和连字符拆开：d'être → “d” + “'être”，Est-ce → “Est” + “-ce”。
    真正的新词都以空格开头，所以没有前导空格的片段并回前一个词，和 text.split() 的分词保持一致。
    输入是 (开始, 结束, 带空格的原始词)。"""
    words: list[tuple[float, float, str]] = []
    for start, end, raw in raw_words:
        if words and not raw.startswith(" "):
            first_start, _, text = words[-1]
            words[-1] = (first_start, float(end), text + raw.strip())
        else:
            words.append((float(start), float(end), raw.strip()))
    return words

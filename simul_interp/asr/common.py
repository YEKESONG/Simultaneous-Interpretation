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


# 另一类幻觉不能直接丢：Whisper 在“有声音但听不出内容”的时候（键盘声、提示音、远处含糊的人声），
# 最常编出来的是一句 “Merci.”——这句话真人也常说（下课时学生就会说）。所以只在整个结果除了它
# 什么都没有的时候，回头向识别模型核实一次（见 StreamingTranscriber._is_made_up）。
_FILLER = re.compile(r"merci(?: merci)*")
_NOT_LETTER = re.compile(r"[^\w\s]+")


def is_filler(words: list[str]) -> bool:
    """这几个词是不是只有 “Merci.”（可以连着重复几遍）。“Merci beaucoup.”“Merci à tous.” 不算：
    实测被编出来的只有单独的 “Merci.”，后面还跟着别的词时，那些词本身就是佐证。"""
    text = " ".join(_NOT_LETTER.sub(" ", " ".join(words)).lower().split())
    return bool(_FILLER.fullmatch(text))


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

"""可以选的语言，以及不同语言怎么切成“词”、怎么拼回句子。

流式识别是把结果按“词”逐个确认的（asr/streaming.py），切句、出草稿也按词数算（translate.py）。
法语、英语这些语言用空格分词；日语不用空格，这里把它按字切开：一个字算一个“词”，
连在一起的字母和数字（AI、2026 这种）算一个词，标点粘在前一个字上（这样“す。”能认出是句末）。
拼回去时不加空格。按词数设的门槛（多少个词就切句之类）对这类语言按字数放大（chars_per_word）。
用空格的语言不受影响：照旧 split() 和 " ".join()。

Whisper 的模型本身是多语言的，换源语言不用换模型，也不用下载任何东西。

目标语言只是写进翻译提示词里的一个名字，大模型能翻的都可以；这里列常用的几个，
config.toml 里也可以写表外的。
"""

from __future__ import annotations

import re

# Whisper 的语言代码 → 中文名
SOURCE_LANGUAGES = {
    "fr": "法语",
    "en": "英语",
    "es": "西班牙语",
    "de": "德语",
    "it": "意大利语",
    "pt": "葡萄牙语",
    "ja": "日语",
}

TARGET_LANGUAGES = ["简体中文", "繁體中文", "English", "Français", "Español", "Deutsch", "日本語"]

# 不用空格分词的语言 → 一个“词”平均相当于几个字（用来换算按词数设的门槛）
NO_SPACE_LANGUAGES = {"ja": 2.0}

# 一个“字”：连在一起的半角 / 全角字母数字（可以带小数点、百分号）算一个，其他每个字符一个
_CHAR_TOKEN = re.compile(r"[0-9A-Za-z０-９Ａ-Ｚａ-ｚ]+(?:[.,'’%][0-9A-Za-z０-９Ａ-Ｚａ-ｚ]+)*%?|\S")
_PUNCT = re.compile(r"^[^\w]+$")
_ALNUM_END = re.compile(r"[0-9A-Za-z０-９Ａ-Ｚａ-ｚ]$")
_ALNUM_START = re.compile(r"^[0-9A-Za-z０-９Ａ-Ｚａ-ｚ]")


def language_name(code: str) -> str:
    """语言代码的中文名；不在表里的原样返回（比如 config.toml 里写了别的语言）。"""
    return SOURCE_LANGUAGES.get(code, code)


def uses_spaces(language: str) -> bool:
    return language not in NO_SPACE_LANGUAGES


def chars_per_word(language: str) -> float:
    """按词数设的门槛要乘的倍数：用空格的语言是 1。"""
    return NO_SPACE_LANGUAGES.get(language, 1.0)


def split_words(text: str, language: str) -> list[str]:
    """把识别结果切成“词”。用空格的语言就是 text.split()。"""
    if uses_spaces(language):
        return text.split()
    words: list[str] = []
    for token in _CHAR_TOKEN.findall(text):
        if words and _PUNCT.match(token):
            words[-1] += token  # 标点粘在前一个字上
        else:
            words.append(token)
    return words


def join_words(words: list[str], language: str) -> str:
    """把“词”拼回句子。不用空格的语言里，只有两个字母数字词挨着时（“Deep Learning”）中间才加空格。"""
    if uses_spaces(language):
        return " ".join(words)
    text = ""
    for word in words:
        if text and _ALNUM_END.search(text) and _ALNUM_START.match(word):
            text += " "
        text += word
    return text

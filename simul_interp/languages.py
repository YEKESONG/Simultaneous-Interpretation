"""可以选的语言。

源语言只列按空格分词的语言：流式识别是把结果按“词”逐个确认的（asr/streaming.py），
中文、日语这类不分词的语言要换一套做法，目前不支持。Whisper 的模型本身是多语言的，
换源语言不用换模型，也不用下载任何东西。

目标语言只是写进翻译提示词里的一个名字，大模型能翻的都可以；这里列常用的几个，
config.toml 里也可以写表外的。
"""

from __future__ import annotations

# Whisper 的语言代码 → 中文名
SOURCE_LANGUAGES = {
    "fr": "法语",
    "en": "英语",
    "es": "西班牙语",
    "de": "德语",
    "it": "意大利语",
    "pt": "葡萄牙语",
}

TARGET_LANGUAGES = ["简体中文", "繁體中文", "English", "Français", "Español", "Deutsch", "日本語"]


def language_name(code: str) -> str:
    """语言代码的中文名；不在表里的原样返回（比如 config.toml 里写了别的语言）。"""
    return SOURCE_LANGUAGES.get(code, code)

import json
import threading
import time

import httpx
import pytest

from simul_interp.config import TranslateConfig
from simul_interp.translate import ChatTranslator, TranslationError, TranslationStage, UnitBuilder


def sse(*deltas):
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": d}}]}, ensure_ascii=False) for d in deltas
    ]
    lines.append('data: {"choices": [], "usage": {"total_tokens": 10}}')  # 有的服务最后会发一条只有用量的消息
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def make_translator(handler, **cfg):
    return ChatTranslator(TranslateConfig(**cfg), api_key="test-key", transport=httpx.MockTransport(handler))


def test_stream_parses_sse_and_sends_expected_body():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse("你好", "，", "各位。"), headers={"content-type": "text/event-stream"})

    translator = make_translator(handler, glossary={"IA": "人工智能"})
    assert "".join(translator.stream("Bonjour à tous.", ["Phrase précédente."])) == "你好，各位。"
    body = seen["body"]
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert body["model"] == "deepseek-flash" and body["stream"] is True
    assert body["thinking"] == {"type": "disabled"}  # DeepSeek 默认开思考模式，同传必须关掉
    assert "IA → 人工智能" in body["messages"][0]["content"]
    assert "Phrase précédente." in body["messages"][1]["content"]
    assert body["messages"][1]["content"].endswith("Bonjour à tous.")


def test_http_error_is_reported():
    translator = make_translator(lambda request: httpx.Response(401, json={"error": "invalid key"}))
    with pytest.raises(TranslationError, match="401"):
        list(translator.stream("Bonjour"))


def test_unit_builder_splits_at_sentences_and_long_clauses():
    b = UnitBuilder(clause_min_words=8, max_words=25)
    assert b.add("Bonjour à toutes et à tous,", False) == []  # 逗号前只有 6 个词：先不送
    assert b.add("et merci d'être venus. Aujourd'hui", False) == [
        "Bonjour à toutes et à tous, et merci d'être venus."
    ]
    long_clause = "je propose que nous organisions une démonstration devant le comité de direction,"
    assert b.add(long_clause, False) == ["Aujourd'hui " + long_clause]  # 长句在逗号处先送前半句
    assert b.add("afin de présenter", True) == ["afin de présenter"]  # 说完了，剩下的全送


class ScriptedTranslator:
    """记下每次请求的原文；gate 打开之前不返回任何字，用来控制时序。"""

    def __init__(self, open_gate=True):
        self.calls = []
        self.gate = threading.Event()
        if open_gate:
            self.gate.set()

    def stream(self, source, context=None):
        self.calls.append(source)
        self.gate.wait(2)
        yield "译"
        yield "：" + source[:8]

    def close(self):
        pass


def wait_for(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def test_speculation_hit_is_adopted_without_second_request():
    translator = ScriptedTranslator()
    shown = []
    stage = TranslationStage(translator, lambda unit: None, speculative=True, on_speculation=shown.append)
    stage.feed("Bonjour à", final=False)  # 已确认的两个词，还没凑成单元
    stage.speculate("tous. Nous")  # 加上暂定的 “tous.” 就是一整句：先送去翻译
    assert wait_for(lambda: stage.speculative_translation)  # 原文还没确认，投机译文已经在显示
    units = stage.feed("tous.", final=False)  # 正式确认，文字和投机的一样
    stage.close()
    assert translator.calls == ["Bonjour à tous."]  # 只请求了一次
    unit = units[0]
    assert unit.speculative and unit.done and unit.translation.startswith("译")
    assert unit.first_token_at < unit.ready_at  # 原文确认之前，译文就已经开始出现
    assert stage.stats == {"speculated": 1, "adopted": 1}
    assert shown


def test_speculation_miss_is_cancelled_and_retranslated():
    translator = ScriptedTranslator(open_gate=False)
    stage = TranslationStage(translator, lambda unit: None, speculative=True)
    stage.speculate("Les résultats sont encouragés.")
    assert wait_for(lambda: translator.calls)
    units = stage.feed("Les résultats sont encourageants.", final=False)  # 确认后的文字变了
    translator.gate.set()
    stage.close()
    assert translator.calls == ["Les résultats sont encouragés.", "Les résultats sont encourageants."]
    assert not units[0].speculative and units[0].translation.startswith("译")
    assert stage.stats == {"speculated": 1, "adopted": 0}


def test_no_speculation_without_a_complete_unit_or_on_long_guesses():
    translator = ScriptedTranslator()
    stage = TranslationStage(translator, lambda unit: None, speculative=True)
    stage.speculate("nous allons faire le point")  # 还凑不成单元
    stage.speculate(" ".join(f"mot{i}" for i in range(13)) + ".")  # 要靠 14 个暂定词才凑成：太不可靠
    stage.close()
    assert translator.calls == []


def test_stage_translates_in_background_and_reports_progress():
    translator = make_translator(
        lambda request: httpx.Response(200, content=sse("大家好", "。"), headers={"content-type": "text/event-stream"})
    )
    updates = []
    stage = TranslationStage(translator, updates.append)
    units = stage.feed("Bonjour à tous.", final=False)
    stage.close()
    assert len(units) == 1
    unit = units[0]
    assert unit.done and unit.translation == "大家好。" and unit.error == ""
    assert unit.first_token_at is not None and unit.first_token_at >= unit.ready_at
    assert updates[0] is unit  # 送去翻译时先通知一次（此时只有原文）

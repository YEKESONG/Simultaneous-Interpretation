import json

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

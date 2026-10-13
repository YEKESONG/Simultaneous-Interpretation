import json
import threading
import time

import httpx
import pytest

from simul_interp.config import TranslateConfig
from simul_interp import translate
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
    assert "把法语实时翻译成简体中文" in body["messages"][0]["content"]
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


def test_unit_builder_keeps_the_time_of_each_units_first_word():
    b = UnitBuilder()
    assert b.add_timed("Bonjour à", False, [1.0, 1.4]) == []
    # 一个单元可以跨好几次确认：时间取它第一个词的
    assert b.add_timed("tous. Merci", False, [1.8, 3.5]) == [("Bonjour à tous.", 1.0)]
    assert b.add_timed("beaucoup.", True, [3.9]) == [("Merci beaucoup.", 3.5)]
    assert b.add_timed("Oui.", False, [1.0, 2.0]) == [("Oui.", None)]  # 时间和词数对不上：当作不知道
    assert b.add("Non.", False) == ["Non."] and b.pending_times == []


def test_stage_passes_word_times_to_units():
    stage = TranslationStage(None, lambda unit: None)
    units = stage.feed("Bonjour à tous. Merci", final=False, word_times=[1.0, 1.4, 1.8, 3.5])
    units += stage.feed("beaucoup.", final=True, word_times=[3.9])
    stage.close()
    assert [(u.source, u.audio_start) for u in units] == [("Bonjour à tous.", 1.0), ("Merci beaucoup.", 3.5)]
    assert stage.feed("Oui.", final=True)[0].audio_start is None  # 不给时间也照常工作


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


def test_ellipsis_and_initials_do_not_end_a_sentence():
    b = UnitBuilder()
    assert b.add("Alors je voudrais vous présenter aujourd'hui le...", False) == []  # 省略号：说话人拖着音，话还没说完
    assert b.add("le plan du projet.", False) == ["Alors je voudrais vous présenter aujourd'hui le... le plan du projet."]
    assert b.add("M. Dupont est là.", False) == ["M. Dupont est là."]  # “M.” 是缩写，不是句子的结尾
    # 省略号后面多半是一次想词的停顿：11 个词的半句不在这里切（逗号的话 8 个词就切了），等他接着说
    assert b.add("le train de neuf heures part toujours du quai numéro trois...", False) == []
    assert b.add("sauf le dimanche.", False) == [
        "le train de neuf heures part toujours du quai numéro trois... sauf le dimanche."
    ]
    rambling = "et puis le marché du samedi matin est toujours plein de gens qui viennent pour..."
    assert b.add(rambling, False) == [rambling]  # 已经 15 个词了：够长，可以先送去翻译


def test_overlong_stretch_is_cut_at_the_last_pause_not_mid_phrase():
    """一口气说了 25 个词还没有句号，必须切了：切在最后一个省略号或逗号之后，而不是正好第 25 个词那里。"""
    b = UnitBuilder()
    head = "le train de neuf heures part toujours du quai trois..."  # 10 个词，结尾拖着音
    rest = " ".join(f"mot{i}" for i in range(15))
    assert b.add(f"{head} {rest}", False) == [head]
    assert b.pending == rest.split()  # 后半截接着攒
    assert b.first_unit(f"{head} {rest}".split()) == 10  # 投机翻译用同一套切法
    plain = " ".join(f"mot{i}" for i in range(30))
    assert UnitBuilder().add(plain, False) == [" ".join(plain.split()[:25])]  # 一个标点都没有：只能硬切


def collector():
    """记下每个翻译单元（同一个单元会通知好几次，只留一份）。"""
    seen = {}
    return seen, lambda unit: seen.setdefault(unit.id, unit)


def test_unfinished_sentence_waits_for_the_speaker_to_continue():
    """说话人说了半句、停下来想词：这半句先不翻译，等接上后面的话再一起翻。"""
    translator = ScriptedTranslator()
    stage = TranslationStage(translator, lambda unit: None, incomplete_wait_s=30)
    assert stage.feed("Alors je voudrais vous présenter aujourd'hui le...", final=True) == []  # 这段话停了，但句子没说完
    assert stage.pending == "Alors je voudrais vous présenter aujourd'hui le..."  # 留着，界面上仍然显示在“正在说”那一行
    stage.speech_started()
    units = stage.feed("le plan du projet.", final=True)
    stage.close()
    whole = "Alors je voudrais vous présenter aujourd'hui le... le plan du projet."
    assert [u.source for u in units] == [whole]
    assert translator.calls == [whole]  # 只翻译了一次，翻的是完整的一句


def test_unfinished_sentence_is_sent_after_waiting_long_enough():
    seen, on_update = collector()
    flushed = []
    stage = TranslationStage(ScriptedTranslator(), on_update, incomplete_wait_s=0.2, on_flush=lambda: flushed.append(1))
    stage.feed("et ensuite nous", final=True)
    assert not seen and stage.pending  # 还在等
    assert wait_for(lambda: flushed)  # 等够了还没有下文：送去翻译，并通知界面清掉“正在说”那一行
    stage.close()
    assert seen[0].source == "et ensuite nous" and seen[0].translation.startswith("译")
    assert stage.pending == ""


def test_speaker_resuming_cancels_the_wait():
    seen, on_update = collector()
    stage = TranslationStage(ScriptedTranslator(), on_update, incomplete_wait_s=0.15)
    stage.feed("et ensuite nous", final=True)
    stage.speech_started()  # 很快又开口了：语音检测比识别结果早知道近一秒
    time.sleep(0.4)
    assert not seen  # 只要还在说就一直等，不会说到一半把前半句送走
    stage.feed("allons au marché.", final=True)
    stage.close()
    assert [u.source for u in seen.values()] == ["et ensuite nous allons au marché."]


def test_no_wait_starts_if_the_speaker_has_already_resumed():
    """实际的先后顺序可能是：语音检测已经发现下一段话开始了，上一段话的识别结果才处理完。"""
    seen, on_update = collector()
    stage = TranslationStage(ScriptedTranslator(), on_update, incomplete_wait_s=0.1)
    stage.speech_started()  # 第一段话
    stage.speech_started()  # 第二段话已经开始了
    stage.feed("et ensuite nous", final=True)  # 第一段话的结果这时才到
    time.sleep(0.3)
    assert not seen
    stage.feed("allons au marché.", final=True)
    stage.close()
    assert len(seen) == 1


def test_closing_sends_what_is_still_waiting():
    seen, on_update = collector()
    stage = TranslationStage(ScriptedTranslator(), on_update, incomplete_wait_s=30)
    stage.feed("et ensuite nous", final=True)
    stage.close()  # 退出时还在等的半句不能丢
    assert seen[0].source == "et ensuite nous" and seen[0].done


def test_waiting_fragment_is_translated_ahead_of_time(monkeypatch):
    """快到点时先投机翻译：真到点、说话人没有接着说，译文已经现成，不用再多等一次请求。"""
    monkeypatch.setattr(translate, "HOLD_SPECULATE_LEAD_S", 0.15)
    translator = ScriptedTranslator()
    seen, on_update = collector()
    stage = TranslationStage(translator, on_update, speculative=True, incomplete_wait_s=0.3)
    stage.feed("et ensuite nous", final=True)
    time.sleep(0.05)
    assert translator.calls == []  # 刚开始等的时候不请求：多数情况下说话人马上就接着说了
    assert wait_for(lambda: seen)
    stage.close()
    assert translator.calls == ["et ensuite nous"] and seen[0].speculative  # 只请求了一次
    assert stage.stats == {"speculated": 1, "adopted": 1}


def test_prompt_follows_the_languages_chosen_at_run_time():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(200, content=sse("ok"), headers={"content-type": "text/event-stream"})

    translator = make_translator(handler)
    translator.source_language = "英语"  # 运行中换语言：改这两处，下一次请求就生效
    translator.cfg.target_language = "Français"
    list(translator.stream("Good morning."))
    assert "把英语实时翻译成Français" in seen[0]


def test_context_can_be_dropped_when_the_language_changes():
    translator = ScriptedTranslator()
    contexts = []
    original = translator.stream
    translator.stream = lambda source, context=None: (contexts.append(list(context)), original(source, context))[1]
    stage = TranslationStage(translator, lambda unit: None)
    stage.feed("Bonjour à tous.", final=True)
    stage.reset_context()  # 之前的句子是另一种语言的，不能再当上文
    assert stage.next_id == 1
    stage.feed("Good morning.", final=True)
    stage.close()
    assert contexts == [[], []]

"""不同语言怎么切成“词”、怎么拼回句子。"""

from simul_interp.languages import SOURCE_LANGUAGES, chars_per_word, join_words, split_words, uses_spaces


def test_spaced_languages_are_unchanged():
    text = "Bonjour à tous, merci d'être venus."
    assert split_words(text, "fr") == text.split()
    assert join_words(text.split(), "fr") == text
    assert uses_spaces("fr") and chars_per_word("fr") == 1.0


def test_japanese_is_split_into_characters():
    assert split_words("今日はAIの話をします。", "ja") == ["今", "日", "は", "AI", "の", "話", "を", "し", "ま", "す。"]
    assert split_words("2026年10月、60%です", "ja") == ["2026", "年", "10", "月、", "60%", "で", "す"]  # 数字连着算一个
    assert split_words("「はい」と言った。", "ja") == ["「", "は", "い」", "と", "言", "っ", "た。"]  # 标点粘在前一个字上
    assert split_words("今日 は 晴れ", "ja") == ["今", "日", "は", "晴", "れ"]  # 识别结果里偶尔有空格，不影响


def test_japanese_is_joined_back_without_spaces():
    for text in ["今日はAIの話をします。", "2026年10月、60%です", "「はい」と言った。"]:
        assert join_words(split_words(text, "ja"), "ja") == text
    assert join_words(["これは", "Deep", "Learning", "です"], "ja") == "これはDeep Learningです"  # 两个英文词之间留空格


def test_japanese_is_listed_and_counted_by_characters():
    assert SOURCE_LANGUAGES["ja"] == "日语"
    assert not uses_spaces("ja") and chars_per_word("ja") == 2.0

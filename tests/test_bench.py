"""测量工具里纯计算的部分。"""

from simul_interp.bench import RecordingView, display_lag
from simul_interp.translate import TranslationUnit


def test_display_lag_is_measured_per_word():
    # (时刻, 屏幕上的法语到第几个词, 译文覆盖到第几个词)
    timeline = [
        (10.0, 3, 0),  # 第 1~3 个词出现
        (11.0, 6, 0),  # 第 4~6 个词出现
        (11.5, 6, 3),  # 前 3 个词有了译文：等了 1.5 秒
        (15.0, 8, 6),  # 第 4~6 个词：等了 4 秒；第 7、8 个词刚出现
        (16.0, 8, 7),  # 第 7 个词：等了 1 秒；第 8 个词一直没有译文，不计
    ]
    assert display_lag(timeline) == [1.5, 1.5, 1.5, 4.0, 4.0, 4.0, 1.0]


def test_recording_view_counts_words_across_units_and_the_live_line():
    class Stage:
        live_covered = 0

    view = RecordingView()
    view.stage = Stage()
    view.on_live("Bonjour à", "tous")  # 3 个词出现，没有译文
    view.stage.live_covered = 2
    view.on_live("Bonjour à", "tous", "大家")  # 当前句下面的译文是照着前 2 个词译的
    assert view.timeline[-1][1:] == (3, 2)
    unit = TranslationUnit(0, "Bonjour à tous.", 0.0)
    view.on_unit(unit)  # 切成了一个单元，正式译文还没来
    view.stage.live_covered = 0
    view.on_live("", "Merci")  # 下一句的第一个词：整场的第 4 个词
    assert view.timeline[-1][1:] == (4, 2)
    unit.translation = "大家好。"
    view.on_unit(unit)  # 正式译文开始出现：这个单元的 3 个词都算有译文了
    assert view.timeline[-1][1:] == (4, 3)

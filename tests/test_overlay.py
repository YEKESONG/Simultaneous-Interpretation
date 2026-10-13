"""悬浮窗的逻辑测试：用 Qt 的 offscreen 平台，不需要屏幕，CI 上也能跑。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QAbstractAnimation, QPoint, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from simul_interp.config import UiConfig  # noqa: E402
from simul_interp.ui.overlay import STREAM_FALLBACK_S, WAIT_FALLBACK_S, Overlay, UnitSnapshot  # noqa: E402


def make_overlay(tmp_path, **ui):
    QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "overlay.ini"), QSettings.Format.IniFormat)
    widget = Overlay(UiConfig(font_size=22, **ui), settings=settings)
    widget.resize(700, 260)
    # 先在 offscreen 平台上把窗口显示出来、处理完事件，保证尺寸已经生效：
    # 没显示的窗口什么时候收到调整大小的事件，各平台不一样（Windows 上字幕区的宽度会在测试中途才变）
    widget.show()
    QApplication.processEvents()
    widget.set_status("")
    return widget


@pytest.fixture
def overlay(tmp_path):
    widget = make_overlay(tmp_path)
    yield widget
    widget.close()
    QApplication.processEvents()


def unit(i, translation="", done=False, error=""):
    return UnitSnapshot(i, f"phrase {i}", translation, done, error)


def settle(overlay):
    """刷新一次并让滑动动画走完。"""
    overlay.refresh()
    overlay.captions._anim.stop()
    overlay.captions._on_anim_done()


def fill(overlay, count):
    """放进 count 对翻好的历史（下一句已经开始，所以它们都在历史里）。"""
    for i in range(count):
        overlay.set_unit(unit(i, f"句子 {i}", done=True))
    overlay.set_live("", "suite")
    settle(overlay)


def test_live_text_goes_to_the_slot_with_blank_translation(overlay):
    overlay.set_live("Bonjour à", "tous")
    history, slot = overlay.compose()
    assert history == [] and slot == ("live", "Bonjour à", "tous")
    overlay.refresh()
    source, translation = overlay.captions._slot_html
    assert "Bonjour à" in source and "tous" in source
    assert translation == ""  # 译文的位置先留白
    assert overlay.hearing


def test_translation_fills_the_blank_then_pair_waits_for_next_sentence(overlay):
    overlay.set_live("Bonjour à tous.", "")
    overlay.set_unit(unit(0))  # 这一句确认了，送去翻译
    overlay.set_live("", "")
    assert overlay.compose()[1] == ("unit", unit(0))  # 当前句区域里是这一句，译文留白
    overlay.set_unit(unit(0, "大家好。", done=True))
    history, slot = overlay.compose()
    assert history == [] and slot[1].translation == "大家好。"  # 译文填进留白，这一对留在原位
    overlay.set_live("", "Aujourd'hui")  # 下一句开始了
    history, slot = overlay.compose()
    assert [u.id for u in history] == [0] and slot[0] == "live"  # 这时上一对才滑进历史


def test_pair_never_moves_back_down_when_live_text_flickers(overlay):
    """实测问题：暂定文字有时会短暂变空，最后一对就在历史和当前句区域之间来回跳。"""
    overlay.set_unit(unit(0, "句子 0", done=True))
    assert overlay.compose()[1] == ("unit", unit(0, "句子 0", done=True))  # 翻好了，先留在当前句区域
    for pending, partial in [("", "Aujourd'hui"), ("", ""), ("", "Aujourd'hui nous"), ("", "")]:
        overlay.set_live(pending, partial)
        history, slot = overlay.compose()
        assert [u.id for u in history] == [0]  # 滑上去之后就一直在历史里，不会再退回来
        assert slot[0] in ("live", "empty")


def test_history_text_does_not_depend_on_the_slot(overlay):
    """历史的文字和排版不随当前句区域里放的是译文还是暂定文字而变（否则会整片重排、闪一下）。"""
    overlay.set_unit(unit(0, "句子 0", done=True))
    overlay.set_live("", "suite")
    overlay.refresh()
    before = [(item.html, item.doc) for item in overlay.captions._items]
    overlay.set_unit(unit(1))  # 下一句确认了、正在翻译：当前句区域从暂定文字变成这一句
    overlay.refresh()
    assert [(item.html, item.doc) for item in overlay.captions._items] == before


def test_slot_changes_never_move_the_history(overlay):
    overlay.set_unit(unit(0, "句子 0", done=True))
    overlay.set_live("", "Aujourd'hui")
    settle(overlay)
    top = overlay.captions.history_top(0)
    height = overlay.captions.slot_height
    for pending, partial in [("Aujourd'hui nous", "allons"), ("Aujourd'hui nous allons faire", "le"), ("", "x")]:
        overlay.set_live(pending, partial)  # 当前句里的暂定文字怎么变
        overlay.refresh()
        assert overlay.captions.history_top(0) == top  # 上面的历史一动不动
        assert overlay.captions.slot_height == height  # 当前句区域的高度也不变


def test_pairs_keep_order_and_wait_for_earlier_ones(overlay):
    overlay.set_unit(unit(0, "句子 0", done=True))
    overlay.set_unit(unit(1))  # 第 1 句还在翻
    overlay.set_unit(unit(2, "句子 2", done=True))  # 第 2 句先翻完了
    overlay.set_live("", "et")
    history, slot = overlay.compose()
    assert [u.id for u in history] == [0] and slot == ("unit", unit(1))  # 第 2 句不插队，也不显示下一句的暂定文字
    overlay.set_unit(unit(1, "句子 1", done=True))
    history, slot = overlay.compose()
    assert [u.id for u in history] == [0, 1, 2] and slot[0] == "live"


def test_units_arriving_out_of_order_are_put_back_in_order(overlay):
    """两个单元几乎同时发出时（一个是等够了时间送出的半句），通知到达的先后可能反过来。"""
    overlay.set_unit(unit(1, "句子 1", done=True))
    overlay.set_unit(unit(0, "句子 0", done=True))
    assert list(overlay.units) == [0, 1]


def test_slow_translation_falls_back_after_waiting(overlay):
    overlay.set_unit(unit(0, "半句", done=False))  # 译文开始出来了，但迟迟没翻完
    overlay.set_live("", "suite")
    start = overlay._first_text_at[0]
    assert overlay.compose(start + STREAM_FALLBACK_S / 2)[1][0] == "unit"  # 先等着
    assert overlay.compose(start + STREAM_FALLBACK_S)[1][0] == "live"  # 等太久就往下走
    overlay.set_unit(unit(1))  # 一个字都没出来
    seen = overlay._seen_at[1]
    assert [u.id for u in overlay.compose(seen + WAIT_FALLBACK_S)[0]] == [0, 1]


def test_escaping_error_and_dimming(overlay):
    overlay.set_unit(UnitSnapshot(0, "x < y & z", "甲<乙", True, ""))
    overlay.set_unit(UnitSnapshot(1, "Bonjour", "", True, "HTTP 401"))
    overlay.set_live("", "suite")  # 下一句开始了，两对都进了历史
    overlay.refresh()
    older, newest = [item.html for item in overlay.captions._items]
    assert "x &lt; y &amp; z" in older and "甲&lt;乙" in older  # 原文译文里的 < & 不能被当成 HTML
    assert "翻译失败：HTTP 401" in newest
    # 越旧越暗：当前句最亮，历史里最新的一对次一级，更旧的再暗一级
    opacity = overlay.captions.pair_opacity
    assert 1.0 > opacity(0) > opacity(1) == opacity(7)


def test_window_size_never_follows_content(overlay):
    size = overlay.size()
    for i in range(12):
        overlay.set_unit(UnitSnapshot(i, "très long " * 40, "很长的译文" * 30, True, ""))
        overlay.set_live("", "très " * 50)
        overlay.refresh()
        QApplication.processEvents()
    assert overlay.size() == size  # 文字再多，窗口也不会被撑大


def test_nothing_is_recomputed_while_idle(overlay, monkeypatch):
    """字幕窗每 50 毫秒检查一次；没有变化、也没有正在等的译文时，这次检查什么都不该做。"""
    fill(overlay, 5)
    monkeypatch.setattr(overlay, "compose", lambda *args: pytest.fail("没有变化却重新安排了显示"))
    overlay.refresh()
    overlay.set_live("", "suite")  # 和现在显示的一样，也不算变化
    overlay.refresh()


def test_history_can_be_scrolled_back(overlay):
    fill(overlay, 12)
    captions = overlay.captions
    assert captions.max_scroll() > 0 and not captions.reviewing  # 历史比窗口放得下的多，平时停在最新
    captions.scroll_by(150)
    assert captions.reviewing and captions.pair_opacity(5) == 1.0  # 回看时全部用亮色
    captions.scroll_by(10**6)
    assert captions.scroll == captions.max_scroll()
    assert abs(captions.history_top(0)) < 1  # 翻到头：最旧的一对正好顶着字幕区的上边
    captions.scroll_by(-(10**6))
    assert not captions.reviewing


def test_reading_position_stays_put_while_new_pairs_arrive(overlay):
    """回看的时候，新的句子照常加在下面，但眼前这几对不能动。"""
    fill(overlay, 12)
    captions = overlay.captions
    captions.scroll_by(150)
    top, scroll = captions.history_top(6), captions.scroll
    overlay.set_unit(unit(12, "句子 12", done=True))
    overlay.set_live("", "encore")
    overlay.refresh()
    assert abs(captions.history_top(6) - top) < 0.01  # 位置没变
    assert captions.scroll > scroll  # 靠多翻一点来抵消
    assert captions._anim.state() == QAbstractAnimation.State.Stopped and captions._shift == 0  # 不做滑动动画
    assert 12 in captions.history_keys()  # 新的一对已经在下面了
    captions.scroll_to_latest()
    assert captions.history_top(6) < top  # 回到最新：旧的被顶上去


def test_wheel_scrollbar_and_hint(overlay):
    fill(overlay, 12)
    captions = overlay.captions
    assert overlay._scroll_thumb().bottom() == overlay._scroll_track().bottom()  # 滑块在最下面 = 停在最新
    center = QPointF(overlay.rect().center())
    wheel = QWheelEvent(center, center, QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(overlay, wheel)  # 滚轮往上滚一格
    assert captions.reviewing
    position = overlay.pos()
    track = overlay._scroll_track()
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(track.center().x(), track.top() + 1))
    assert captions.scroll == captions.max_scroll()  # 点轨道最上面：翻到最旧
    assert overlay._scroll_thumb().top() == track.top()
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=overlay._review_hint().center())
    assert not captions.reviewing  # 点顶部的提示：回到最新
    assert overlay.pos() == position  # 这些点击都不会拖动窗口


def test_scrollback_keeps_only_recent_pairs(tmp_path):
    overlay = make_overlay(tmp_path, scrollback=10)
    fill(overlay, 25)
    assert list(overlay.units) == list(range(15, 25))  # 更早的不留在字幕窗里（会话记录里有）
    assert overlay.captions.history_keys() == list(range(15, 25))
    assert set(overlay._pairs) <= set(overlay.units)  # 缓存也跟着清掉
    overlay.close()


def test_resizing_rewraps_the_history(overlay):
    for i in range(8):
        overlay.set_unit(UnitSnapshot(i, "une phrase assez longue pour passer à la ligne " * 2, "一句比较长的译文" * 6, True, ""))
    overlay.set_live("", "suite")
    settle(overlay)
    before = overlay.captions.max_scroll()
    overlay.resize(420, 260)  # 窗口变窄：每一对要折成更多行
    QApplication.processEvents()
    assert overlay.captions.max_scroll() > before


def test_font_limits_and_click_through(overlay):
    for _ in range(40):
        overlay.change_font(+2)
    assert overlay.font_size == 64
    for _ in range(40):
        overlay.change_opacity(-0.1)
    assert overlay.opacity == 0.0
    overlay.set_click_through(True)
    assert overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput
    overlay.set_click_through(False)
    assert not overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput


def test_old_font_setting_is_reset_once(tmp_path):
    QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "old.ini"), QSettings.Format.IniFormat)
    settings.setValue("font_size", 26)  # 旧版本保存的大字号
    assert Overlay(UiConfig(font_size=22), settings=settings).font_size == 22
    settings.setValue("font_size", 30)  # 新版本里用户自己调的字号要保留
    assert Overlay(UiConfig(font_size=22), settings=settings).font_size == 30

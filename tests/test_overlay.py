"""悬浮窗的逻辑测试：用 Qt 的 offscreen 平台，不需要屏幕，CI 上也能跑。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from simul_interp.config import UiConfig  # noqa: E402
from simul_interp.ui.overlay import STREAM_FALLBACK_S, WAIT_FALLBACK_S, Overlay, UnitSnapshot  # noqa: E402


@pytest.fixture
def overlay(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "overlay.ini"), QSettings.Format.IniFormat)
    widget = Overlay(UiConfig(font_size=22, max_lines=2), settings=settings)
    widget.set_status("")
    yield widget
    widget.close()
    app.processEvents()


def unit(i, translation="", done=False, error=""):
    return UnitSnapshot(i, f"phrase {i}", translation, done, error)


def texts(overlay, moment=None):
    return "".join(html for _, html in overlay.blocks(moment))


def test_pair_appears_only_when_translation_is_ready(overlay):
    overlay.set_unit(unit(0))  # 原文确认了，但译文还没出来
    assert overlay.visible_units() == []  # 不先显示原文，免得原文一段段冒出来、译文迟迟不到
    overlay.set_unit(unit(0, "句子 0", done=True))
    shown = texts(overlay)
    assert "phrase 0" in shown and "句子 0" in shown  # 原文和译文一起出现


def test_pairs_keep_order_and_wait_for_earlier_ones(overlay):
    overlay.set_unit(unit(0, "句子 0", done=True))
    overlay.set_unit(unit(1))  # 第 1 句还在翻
    overlay.set_unit(unit(2, "句子 2", done=True))  # 第 2 句先翻完了
    assert [u.id for u in overlay.visible_units()] == [0]  # 第 2 句先等着，不插到第 1 句前面
    overlay.set_unit(unit(1, "句子 1", done=True))
    assert [u.id for u in overlay.visible_units()] == [1, 2]  # max_lines = 2，只保留最后两对


def test_slow_translation_falls_back_after_waiting(overlay):
    overlay.set_unit(unit(0, "半句", done=False))  # 译文开始出来了，但迟迟没翻完
    start = overlay._first_text_at[0]
    assert overlay.visible_units(start + STREAM_FALLBACK_S / 2) == []
    assert [u.id for u in overlay.visible_units(start + STREAM_FALLBACK_S)] == [0]
    overlay.set_unit(unit(1))  # 一个字都没出来
    seen = overlay._seen_at[1]
    assert [u.id for u in overlay.visible_units(seen + WAIT_FALLBACK_S)] == [0, 1]


def test_escaping_error_and_dimming(overlay):
    overlay.set_unit(UnitSnapshot(0, "x < y & z", "甲<乙", True, ""))
    overlay.set_unit(UnitSnapshot(1, "Bonjour", "", True, "HTTP 401"))
    newest, older = overlay.blocks()[-1][1], overlay.blocks()[0][1]
    assert "x &lt; y &amp; z" in older and "甲&lt;乙" in older  # 原文里的 < & 不能被当成 HTML
    assert "翻译失败：HTTP 401" in newest
    assert "#ffffff" not in older  # 旧的一对颜色变暗，最新的一对最亮


def test_live_recognition_text_is_not_shown(overlay):
    overlay.set_unit(unit(0, "句子 0", done=True))
    before = overlay.blocks()
    overlay.set_live("déjà confirmé", "encore incertain", "投机译文")
    assert overlay.blocks() == before  # 正在识别的文字只点亮小圆点，不改变字幕内容
    assert overlay.hearing


def test_window_size_never_follows_content(overlay):
    overlay.resize(600, 180)
    size = overlay.size()
    for i in range(12):
        overlay.set_unit(UnitSnapshot(i, "très long " * 40, "很长的译文" * 30, True, ""))
        overlay.refresh()
        QApplication.processEvents()
    assert overlay.size() == size  # 文字再多，窗口也不会被撑大


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

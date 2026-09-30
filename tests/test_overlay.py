"""悬浮窗的逻辑测试：用 Qt 的 offscreen 平台，不需要屏幕，CI 上也能跑。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from simul_interp.config import UiConfig  # noqa: E402
from simul_interp.ui.overlay import Overlay, UnitSnapshot  # noqa: E402


@pytest.fixture
def overlay(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "overlay.ini"), QSettings.Format.IniFormat)
    widget = Overlay(UiConfig(font_size=26, max_lines=2), settings=settings)
    yield widget
    widget.close()
    app.processEvents()


def test_render_escapes_html_and_marks_streaming(overlay):
    overlay.set_unit(UnitSnapshot(0, "x < y & z", "甲<乙", False, ""))
    html = overlay.render_html()
    assert "x &lt; y &amp; z" in html and "甲&lt;乙" in html  # 原文里的 < & 不能被当成 HTML
    assert "…" in html  # 还没翻完，末尾有省略号


def test_keeps_only_latest_units_and_live_line(overlay):
    for i in range(4):
        overlay.set_unit(UnitSnapshot(i, f"phrase {i}", f"句子 {i}", True, ""))
    overlay.set_live("déjà confirmé", "encore incertain")
    overlay.set_status("")
    html = overlay.render_html()
    assert "phrase 0" not in html and "phrase 1" not in html  # max_lines = 2
    assert "phrase 2" in html and "phrase 3" in html
    assert "déjà confirmé" in html and "encore incertain" in html


def test_error_and_font_limits(overlay):
    overlay.set_unit(UnitSnapshot(0, "Bonjour", "", True, "HTTP 401"))
    assert "翻译失败：HTTP 401" in overlay.render_html()
    for _ in range(40):
        overlay.change_font(+2)
    assert overlay.font_size == 64
    for _ in range(40):
        overlay.change_opacity(-0.1)
    assert overlay.opacity == 0.0


def test_click_through_toggles_window_flag(overlay):
    overlay.set_click_through(True)
    assert overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput
    overlay.set_click_through(False)
    assert not overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput
